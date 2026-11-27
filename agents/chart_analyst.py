"""Agent 3: Read-only Chart Analyst Agent computing deterministic technicals from raw OHLCV."""

from __future__ import annotations

import json
import time

from agents.llm_client import StructuredLLMClient
from mcp_client.nse_client import NSEClient
from observability.logger import AuditLogger
from schemas.models import (
    CorporateActionEvent,
    MarketMood,
    NSEQuote,
    OHLCVBar,
    SentimentReport,
    TechnicalIndicators,
    TradeProposal,
)

_SYSTEM_PROMPT = (
    "You are a read-only technical chart analyst for NSE equities. "
    "Base your proposal strictly on the deterministic TechnicalIndicators, live quote, "
    "MarketMood regime, and SentimentReport. If sentiment conflicts with the chart, "
    "market_regime is HIGH_VIX_RISK_OFF, or technicals do not confirm a breakout, emit "
    "action=NO_TRADE. Never invent tickers or request bypassing HITL."
)


def _compute_ema(values: list[float], period: int) -> float:
    """Compute exponential moving average over closing prices."""
    if not values:
        raise ValueError("Cannot compute EMA on empty price series.")
    k = 2.0 / (period + 1)
    ema = values[0]
    for price in values[1:]:
        ema = price * k + ema * (1.0 - k)
    return round(ema, 2)


def _compute_rsi(values: list[float], period: int = 14) -> float:
    """Compute 14-period Relative Strength Index."""
    if len(values) < 2:
        return 50.0
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    window = deltas[-period:] if len(deltas) >= period else deltas
    gains = [d for d in window if d > 0]
    losses = [-d for d in window if d < 0]
    avg_gain = sum(gains) / len(window)
    avg_loss = sum(losses) / len(window)
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def adjust_ohlcv_for_corporate_actions(
    bars: list[OHLCVBar],
    actions: list[CorporateActionEvent],
) -> tuple[list[OHLCVBar], list[CorporateActionEvent]]:
    """Adjust pre-ex-date OHLCV bars for splits/bonuses so EMAs & S/R stay continuous."""
    relevant = [
        a
        for a in actions
        if a.action_type in {"SPLIT", "BONUS"}
        and a.ex_date
        and abs(a.adjustment_factor - 1.0) > 1e-6
    ]
    if not relevant:
        return list(bars), []

    adjusted: list[OHLCVBar] = []
    for bar in bars:
        factor = 1.0
        for act in relevant:
            if bar.date < act.ex_date:
                factor *= act.adjustment_factor
        if abs(factor - 1.0) <= 1e-6:
            adjusted.append(bar)
        else:
            adjusted.append(
                OHLCVBar(
                    date=bar.date,
                    open=round(bar.open * factor, 2),
                    high=round(bar.high * factor, 2),
                    low=round(bar.low * factor, 2),
                    close=round(bar.close * factor, 2),
                    volume=max(0, int(round(bar.volume / factor))),
                )
            )
    return adjusted, relevant


def compute_technical_indicators(
    symbol: str,
    quote: NSEQuote,
    bars: list[OHLCVBar],
    market_mood: MarketMood | None = None,
) -> TechnicalIndicators:
    """Deterministically compute RSI, EMAs, support/resistance, and breakout status."""
    closes = [b.close for b in bars]
    ema_20 = _compute_ema(closes, 20)
    ema_50 = _compute_ema(closes, 50)
    rsi_14 = _compute_rsi(closes, 14)

    lookback = bars[-15:-1] if len(bars) > 2 else bars
    support = round(min(b.low for b in lookback), 2)
    resistance = round(max(b.high for b in lookback), 2)

    prev_volumes = [b.volume for b in bars[:-1]]
    avg_vol = (sum(prev_volumes) / len(prev_volumes)) if prev_volumes else max(quote.volume, 1)
    volume_ratio = round(quote.volume / max(avg_vol, 1.0), 2)

    regime = market_mood.regime if market_mood is not None else "NEUTRAL"
    bullish_breakout = (
        regime != "HIGH_VIX_RISK_OFF"
        and quote.ltp > resistance
        and quote.ltp > ema_20
        and ema_20 >= ema_50
        and volume_ratio >= 1.2
        and 45.0 <= rsi_14 <= 80.0
    )

    return TechnicalIndicators(
        symbol=symbol,
        ltp=quote.ltp,
        rsi_14=rsi_14,
        ema_20=ema_20,
        ema_50=ema_50,
        support=support,
        resistance=resistance,
        volume_ratio=volume_ratio,
        bullish_breakout=bullish_breakout,
        market_regime=regime,
    )


class ChartAnalystAgent:
    """Evaluates corporate-action-adjusted OHLCV + market regime + sentiment."""

    def __init__(
        self,
        nse_client: NSEClient,
        llm_client: StructuredLLMClient,
        audit_logger: AuditLogger,
    ) -> None:
        self._nse = nse_client
        self._llm = llm_client
        self._audit = audit_logger

    def run(
        self,
        symbol: str,
        sentiment: SentimentReport,
        high_vix_threshold: float = 22.0,
        min_ad_ratio: float = 0.65,
    ) -> tuple[
        TechnicalIndicators,
        NSEQuote,
        TradeProposal,
        MarketMood,
        list[CorporateActionEvent],
    ]:
        """Fetch raw OHLCV, quote, corporate actions, and market mood, then propose trade."""
        t0 = time.perf_counter()
        quote = self._nse.get_quote(symbol)
        raw_bars = self._nse.get_historical_ohlcv(symbol, days=35)
        actions = self._nse.get_corporate_actions(symbol)
        adjusted_bars, applied_actions = adjust_ohlcv_for_corporate_actions(raw_bars, actions)
        mood = self._nse.get_market_mood(
            high_vix_threshold=high_vix_threshold,
            min_ad_ratio=min_ad_ratio,
        )
        indicators = compute_technical_indicators(symbol, quote, adjusted_bars, market_mood=mood)

        user_prompt = (
            f"TechnicalIndicators:\n{json.dumps(indicators.model_dump(), indent=2)}\n\n"
            f"MarketMood:\n{json.dumps(mood.model_dump(), indent=2)}\n\n"
            f"SentimentReport:\n{json.dumps(sentiment.model_dump(), indent=2)}"
        )

        proposal = self._llm.generate_structured(
            agent_name="ChartAnalystAgent",
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_schema=TradeProposal,
            metadata={
                "symbol": symbol,
                "ltp": quote.ltp,
                "bullish_breakout": indicators.bullish_breakout,
                "sentiment_score": sentiment.score,
                "sentiment_conflict": sentiment.conflict_detected,
                "market_regime": mood.regime,
            },
        )
        latency = (time.perf_counter() - t0) * 1000.0
        self._audit.record_handoff(
            agent="ChartAnalystAgent",
            tool_or_action=f"propose:{proposal.action.value}:{proposal.symbol}",
            status="ok",
            latency_ms=latency,
            payload={
                "action": proposal.action.value,
                "symbol": proposal.symbol,
                "quantity": proposal.quantity,
                "bullish_breakout": indicators.bullish_breakout,
                "market_regime": mood.regime,
                "india_vix": mood.india_vix,
                "corp_actions_adjusted": len(applied_actions),
            },
        )
        return indicators, quote, proposal, mood, applied_actions
