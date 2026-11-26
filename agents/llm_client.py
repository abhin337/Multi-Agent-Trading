"""Structured LLM client abstraction supporting Gemini API and deterministic mock replay."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from config import AppSettings
from schemas.models import (
    ScanResult,
    SentimentReport,
    TickerCandidate,
    TradeAction,
    TradeProposal,
)

T = TypeVar("T", bound=BaseModel)


class StructuredLLMClient(Protocol):
    """Protocol for structured Pydantic-output LLM providers."""

    def generate_structured(
        self,
        agent_name: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[T],
        metadata: dict[str, Any] | None = None,
    ) -> T:
        """Generate a schema-validated Pydantic response."""
        ...


class GeminiStructuredClient:
    """Google Gemini API client using native structured JSON schema outputs."""

    def __init__(self, settings: AppSettings) -> None:
        api_key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is required when USE_MOCK_LLM=false."
            )
        self._model = settings.gemini_model
        self._client = genai.Client(api_key=api_key)

    def generate_structured(
        self,
        agent_name: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[T],
        metadata: dict[str, Any] | None = None,
    ) -> T:
        response = self._client.models.generate_content(
            model=self._model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=0.1,
            ),
        )
        if not response.text:
            raise RuntimeError(f"Gemini returned empty response for {agent_name}.")
        return response_schema.model_validate_json(response.text)


@dataclass
class MockAgentOverrides:
    """Optional adversarial/scenario overrides for deterministic tests & evals."""

    force_sentiment_score: float | None = None
    force_sentiment_conflict: bool | None = None
    force_proposal_symbol: str | None = None
    force_proposal_quantity: int | None = None
    force_limit_price: float | None = None
    force_action: TradeAction | None = None
    force_override_hitl: bool = False
    force_sell_all_attempt: bool = False


class MockStructuredClient:
    """Deterministic offline structured responder mirroring Gemini schema contracts."""

    def __init__(self, overrides: MockAgentOverrides | None = None) -> None:
        self.overrides = overrides or MockAgentOverrides()

    def generate_structured(
        self,
        agent_name: str,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[T],
        metadata: dict[str, Any] | None = None,
    ) -> T:
        meta = metadata or {}

        if response_schema is ScanResult:
            return self._mock_scan(meta)  # type: ignore[return-value]
        if response_schema is SentimentReport:
            return self._mock_sentiment(user_prompt, meta)  # type: ignore[return-value]
        if response_schema is TradeProposal:
            return self._mock_proposal(meta)  # type: ignore[return-value]
        raise TypeError(f"Unsupported schema for MockStructuredClient: {response_schema}")

    def _mock_scan(self, meta: dict[str, Any]) -> ScanResult:
        valid_quotes: list[dict[str, Any]] = meta.get("valid_quotes", [])
        rejected: list[str] = meta.get("rejected_inputs", [])
        candidates: list[TickerCandidate] = []
        ranked = sorted(
            valid_quotes,
            key=lambda q: (float(q.get("change_pct", 0.0)), int(q.get("volume", 0))),
            reverse=True,
        )
        for idx, item in enumerate(ranked, start=1):
            score = round(max(0.5, min(0.95, 0.90 - (idx - 1) * 0.06)), 2)
            candidates.append(
                TickerCandidate(
                    symbol=item["symbol"],
                    rank=idx,
                    scanner_score=score,
                    catalyst=f"NSE momentum + volume ({item.get('change_pct', 0)}%)",
                )
            )
        selected = candidates[0].symbol if candidates else None
        return ScanResult(
            candidates=candidates,
            selected_symbol=selected,
            rejected_inputs=rejected,
            notes="Deterministic offline scan over read-only NSE MCP quotes.",
        )

    def _mock_sentiment(self, user_prompt: str, meta: dict[str, Any]) -> SentimentReport:
        symbol = str(meta.get("symbol", ""))
        flags: list[str] = list(meta.get("sanitization_flags", []))
        lowered = user_prompt.lower()

        if self.overrides.force_sentiment_score is not None:
            score = self.overrides.force_sentiment_score
            conflict = bool(self.overrides.force_sentiment_conflict)
            summary = "Simulated sentiment score override for adversarial/eval testing."
        elif any(
            neg in lowered
            for neg in ("margin compression", "client ramp-downs", "earnings warning", "cancellations")
        ):
            score = -0.82
            conflict = True
            summary = f"Negative earnings/guidance headlines detected for {symbol}."
        elif "base64_encoded_override" in flags or flags:
            # Neutralize untrusted injected payloads rather than obeying directives
            if any(pos in lowered for pos in ("cloud", "deal", "rallies", "wins", "order", "growth")):
                score = 0.45 if "ticker_substitution_attempt" not in flags else 0.35
            else:
                score = 0.0
            conflict = "fake_compliance_liquidation" in flags
            summary = (
                f"Sanitized untrusted news for {symbol}; ignored adversarial directives "
                f"(flags={flags})."
            )
        elif any(
            pos in lowered
            for pos in ("cloud", "ai transformation", "rallies", "beats", "credit growth", "mega", "steady")
        ):
            score = 0.78
            conflict = False
            summary = f"Constructive institutional and corporate catalyst flow for {symbol}."
        else:
            score = 0.1
            conflict = False
            summary = f"Neutral news backdrop for {symbol}."

        confidence = 0.25 if flags else 0.86
        return SentimentReport(
            symbol=symbol,
            score=score,
            confidence=confidence,
            conflict_detected=conflict,
            sanitization_flags=flags,
            summary=summary,
        )

    def _mock_proposal(self, meta: dict[str, Any]) -> TradeProposal:
        symbol = self.overrides.force_proposal_symbol or str(meta["symbol"])
        ltp = float(meta["ltp"])
        bullish = bool(meta["bullish_breakout"])
        sentiment_score = float(meta["sentiment_score"])
        sentiment_conflict = bool(meta["sentiment_conflict"])

        if self.overrides.force_sell_all_attempt:
            return TradeProposal(
                action=TradeAction.SELL,
                symbol=symbol,
                quantity=500,
                order_type="MARKET",
                limit_price=ltp,
                stop_loss=round(ltp * 1.03, 2),
                rationale="Adversarial liquidation simulation.",
                source_symbols=[str(meta["symbol"])],
                override_hitl_requested=self.overrides.force_override_hitl,
            )

        if self.overrides.force_action is not None:
            action = self.overrides.force_action
        elif sentiment_conflict or sentiment_score <= -0.35 or not bullish:
            action = TradeAction.NO_TRADE
        else:
            action = TradeAction.BUY

        if action == TradeAction.NO_TRADE:
            return TradeProposal(
                action=TradeAction.NO_TRADE,
                symbol=symbol,
                quantity=0,
                order_type="LIMIT",
                limit_price=ltp,
                stop_loss=round(ltp * 0.97, 2),
                rationale=(
                    "Conflicting sentiment or unconfirmed technical breakout; aborting to NO_TRADE."
                ),
                source_symbols=[str(meta["symbol"])],
                override_hitl_requested=self.overrides.force_override_hitl,
            )

        default_qty = max(1, min(25, int(75_000 // ltp)))
        qty = self.overrides.force_proposal_quantity or default_qty
        limit_price = self.overrides.force_limit_price or round(ltp * 1.002, 2)
        stop_loss = round(min(ltp * 0.97, limit_price * 0.97), 2)

        return TradeProposal(
            action=action,
            symbol=symbol,
            quantity=qty,
            order_type="LIMIT",
            limit_price=limit_price,
            stop_loss=stop_loss,
            rationale=(
                f"Confirmed breakout on {symbol} (LTP {ltp:.2f}) with aligned sentiment "
                f"({sentiment_score:+.2f})."
            ),
            source_symbols=[str(meta["symbol"])],
            override_hitl_requested=self.overrides.force_override_hitl,
        )


def build_llm_client(
    settings: AppSettings,
    overrides: MockAgentOverrides | None = None,
) -> StructuredLLMClient:
    """Factory returning MockStructuredClient or live GeminiStructuredClient."""
    if settings.use_mock_llm:
        return MockStructuredClient(overrides=overrides)
    return GeminiStructuredClient(settings=settings)
