"""Canonical NSE ticker allowlist and symbol disambiguation rules."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mcp_server.fixtures import (
    ACTIVE_NSE_SYMBOLS,
    AMBIGUOUS_COMPANY_ALIASES,
    MERGED_OR_DELISTED_SYMBOLS,
)


class TickerValidationResult(BaseModel):
    """Outcome of deterministic NSE ticker validation."""

    model_config = ConfigDict(extra="forbid")

    raw_input: str
    is_valid: bool
    canonical_symbol: str | None = None
    error_code: Literal[
        "OK",
        "EMPTY",
        "AMBIGUOUS",
        "DELISTED_MERGED",
        "NON_NSE_OR_FICTIONAL",
    ]
    reason: str
    candidates: list[str] = Field(default_factory=list)


def validate_ticker(raw_input: str) -> TickerValidationResult:
    """Validate an input symbol or alias against the canonical active NSE allowlist."""
    cleaned = re.sub(r"\s+", " ", raw_input.strip()).upper()
    if not cleaned:
        return TickerValidationResult(
            raw_input=raw_input,
            is_valid=False,
            error_code="EMPTY",
            reason="Empty ticker symbol provided.",
        )

    if cleaned in AMBIGUOUS_COMPANY_ALIASES:
        options = AMBIGUOUS_COMPANY_ALIASES[cleaned]
        return TickerValidationResult(
            raw_input=raw_input,
            is_valid=False,
            error_code="AMBIGUOUS",
            reason=(
                f"Ambiguous company reference '{raw_input}'. "
                f"Disambiguation required among: {', '.join(options)}."
            ),
            candidates=list(options),
        )

    if cleaned in MERGED_OR_DELISTED_SYMBOLS:
        return TickerValidationResult(
            raw_input=raw_input,
            is_valid=False,
            error_code="DELISTED_MERGED",
            reason=f"Stale/merged ticker '{cleaned}': {MERGED_OR_DELISTED_SYMBOLS[cleaned]}.",
        )

    if cleaned in ACTIVE_NSE_SYMBOLS:
        return TickerValidationResult(
            raw_input=raw_input,
            is_valid=True,
            canonical_symbol=cleaned,
            error_code="OK",
            reason=f"Active NSE symbol ({ACTIVE_NSE_SYMBOLS[cleaned]}).",
        )

    return TickerValidationResult(
        raw_input=raw_input,
        is_valid=False,
        error_code="NON_NSE_OR_FICTIONAL",
        reason=f"Symbol '{raw_input}' is not in the active NSE allowlist.",
    )
