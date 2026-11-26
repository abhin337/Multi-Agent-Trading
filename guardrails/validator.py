"""Deterministic security & risk validation at the execution tool boundary."""

from __future__ import annotations

from config import AppSettings
from guardrails.allowlist import validate_ticker
from schemas.models import (
    NSEQuote,
    OrderRequest,
    ScanResult,
    SentimentReport,
    TechnicalIndicators,
    TradeAction,
    TradeProposal,
)


class GuardrailViolation(RuntimeError):
    """Raised when a proposal or agent violates deterministic boundary guardrails."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class BoundaryValidator:
    """Enforces least-privilege, ticker allowlist, cross-agent consistency, and risk caps."""

    READ_ONLY_ROLES = frozenset({"scanner", "sentiment", "chart_analyst"})
    ALLOWED_EXECUTION_CALLER = "orchestrator_validated"

    @classmethod
    def assert_caller_authorized(cls, caller_role: str) -> None:
        """Block read-only agents from directly invoking write-enabled brokerage tools."""
        if caller_role != cls.ALLOWED_EXECUTION_CALLER:
            raise GuardrailViolation(
                [
                    f"least_privilege_violation: role '{caller_role}' is read-only "
                    "and cannot invoke Zerodha execution tools."
                ]
            )

    @classmethod
    def validate_proposal(
        cls,
        trace_id: str,
        proposal: TradeProposal,
        scan_result: ScanResult,
        sentiment: SentimentReport,
        indicators: TechnicalIndicators,
        live_quote: NSEQuote,
        settings: AppSettings,
        caller_role: str = ALLOWED_EXECUTION_CALLER,
    ) -> OrderRequest:
        """Validate a TradeProposal deterministically and return an OrderRequest."""
        cls.assert_caller_authorized(caller_role)
        reasons: list[str] = []

        if proposal.action == TradeAction.NO_TRADE:
            reasons.append("no_trade_proposal: proposal action is NO_TRADE.")

        # 1. Canonical NSE ticker allowlist check
        ticker_check = validate_ticker(proposal.symbol)
        if not ticker_check.is_valid or ticker_check.canonical_symbol is None:
            reasons.append(f"ticker_allowlist_rejected: {ticker_check.reason}")
            canonical_symbol = proposal.symbol.strip().upper()
        else:
            canonical_symbol = ticker_check.canonical_symbol

        # 2. Cross-agent ticker consistency check (blocks hidden ticker substitution)
        scanned_symbols = {c.symbol.upper() for c in scan_result.candidates}
        if canonical_symbol != sentiment.symbol.upper():
            reasons.append(
                f"cross_agent_ticker_mismatch: proposal '{canonical_symbol}' != "
                f"sentiment '{sentiment.symbol}'."
            )
        if scan_result.selected_symbol and canonical_symbol != scan_result.selected_symbol.upper():
            reasons.append(
                f"cross_agent_ticker_mismatch: proposal '{canonical_symbol}' != "
                f"scanner selected '{scan_result.selected_symbol}'."
            )
        if scanned_symbols and canonical_symbol not in scanned_symbols:
            reasons.append(
                f"cross_agent_ticker_mismatch: '{canonical_symbol}' not in scanner candidates."
            )

        # 3. Quantity & position exposure caps
        if proposal.quantity <= 0:
            reasons.append("invalid_quantity: quantity must be > 0.")
        if proposal.quantity > settings.max_order_quantity:
            reasons.append(
                f"max_order_quantity_exceeded: {proposal.quantity} > {settings.max_order_quantity}."
            )

        effective_price = proposal.limit_price or live_quote.ltp
        notional_inr = proposal.quantity * effective_price
        if notional_inr > settings.max_position_value_inr:
            reasons.append(
                f"max_position_value_exceeded: INR {notional_inr:,.2f} > "
                f"cap INR {settings.max_position_value_inr:,.2f}."
            )

        # 4. Price deviation & stop-loss sanity checks
        if proposal.order_type == "LIMIT":
            if proposal.limit_price is None:
                reasons.append("missing_limit_price: LIMIT order requires limit_price.")
            else:
                dev_pct = abs(proposal.limit_price - live_quote.ltp) / live_quote.ltp * 100.0
                if dev_pct > settings.max_price_deviation_pct:
                    reasons.append(
                        f"fat_finger_price_deviation: limit {proposal.limit_price:.2f} deviates "
                        f"{dev_pct:.2f}% from LTP {live_quote.ltp:.2f} "
                        f"(max {settings.max_price_deviation_pct:.1f}%)."
                    )

        if proposal.stop_loss is None:
            reasons.append("missing_stop_loss: deterministic risk policy requires stop_loss.")
        else:
            if proposal.action == TradeAction.BUY:
                if proposal.stop_loss >= effective_price:
                    reasons.append("invalid_stop_loss: BUY stop_loss must be below entry price.")
                else:
                    sl_pct = (effective_price - proposal.stop_loss) / effective_price * 100.0
                    if sl_pct > settings.max_stop_loss_pct:
                        reasons.append(
                            f"stop_loss_cap_exceeded: {sl_pct:.2f}% > {settings.max_stop_loss_pct}%."
                        )
            elif proposal.action == TradeAction.SELL:
                if proposal.stop_loss <= effective_price:
                    reasons.append("invalid_stop_loss: SELL stop_loss must be above entry price.")
                else:
                    sl_pct = (proposal.stop_loss - effective_price) / effective_price * 100.0
                    if sl_pct > settings.max_stop_loss_pct:
                        reasons.append(
                            f"stop_loss_cap_exceeded: {sl_pct:.2f}% > {settings.max_stop_loss_pct}%."
                        )

        # 5. Deterministic technical & sentiment alignment (guards against handoff drift)
        if proposal.action == TradeAction.BUY:
            if not indicators.bullish_breakout:
                reasons.append(
                    "technical_validation_failed: raw OHLCV indicators do not confirm "
                    "bullish breakout."
                )
            if sentiment.conflict_detected or sentiment.score <= -0.35:
                reasons.append(
                    "sentiment_conflict_blocked: negative/conflicting sentiment forbids BUY."
                )

        if reasons:
            raise GuardrailViolation(reasons)

        return OrderRequest(
            trace_id=trace_id,
            symbol=canonical_symbol,
            exchange=settings.allowed_exchange,
            transaction_type=proposal.action.value,  # type: ignore[arg-type]
            quantity=proposal.quantity,
            order_type=proposal.order_type,
            price=round(effective_price, 2),
            stop_loss=round(proposal.stop_loss or effective_price * 0.97, 2),
            product="CNC",
            order_tag="AGY_VALIDATED",
        )
