"""Agent 1: Read-only NSE Scanner Agent."""

from __future__ import annotations

import json
import time

from agents.llm_client import StructuredLLMClient
from config import AppSettings
from guardrails.allowlist import validate_ticker
from mcp_client.nse_client import NSEClient
from observability.logger import AuditLogger
from schemas.models import ScanResult

_DEFAULT_WATCHLIST = ["INFY", "RELIANCE", "ICICIBANK", "SBIN", "TCS"]

_SYSTEM_PROMPT = (
    "You are a read-only NSE equity scanner agent. "
    "Rank only the pre-validated NSE symbols provided to you by momentum and volume, "
    "and select the single highest-conviction symbol in selected_symbol. "
    "Never invent tickers or reference non-NSE symbols."
)


class ScannerAgent:
    """Scans candidate symbols via read-only NSE MCP and outputs a validated ScanResult."""

    def __init__(
        self,
        nse_client: NSEClient,
        llm_client: StructuredLLMClient,
        settings: AppSettings,
        audit_logger: AuditLogger,
    ) -> None:
        self._nse = nse_client
        self._llm = llm_client
        self._settings = settings
        self._audit = audit_logger

    def run(self, raw_candidates: list[str] | None = None) -> ScanResult:
        """Validate candidate tickers against the NSE allowlist, fetch quotes, and rank."""
        t0 = time.perf_counter()
        requested = raw_candidates if raw_candidates is not None else list(_DEFAULT_WATCHLIST)
        active_map = self._nse.list_active_symbols()
        allowed_set: set[str] | None = (
            set(active_map.keys()) if self._nse.mode == "nse_live" else None
        )

        valid_quotes: list[dict[str, object]] = []
        rejected_inputs: list[str] = []

        for item in requested:
            if (
                allowed_set is not None
                and item.strip().upper() not in allowed_set
                and " " not in item.strip()
            ):
                # Check nse-bhavcopy 3,500+ equity catalog when beyond top-500 stocks
                matches = self._nse.lookup_live_symbol(item)
                if item.strip().upper() in matches:
                    allowed_set.add(item.strip().upper())

            check = validate_ticker(item, allowed_symbols=allowed_set)
            if not check.is_valid or check.canonical_symbol is None:
                rejected_inputs.append(f"{item}: {check.reason}")
                self._audit.log_event(
                    event_type="scanner_ticker_rejected",
                    component="agents.scanner",
                    status="blocked",
                    payload={
                        "raw_input": item,
                        "error_code": check.error_code,
                        "reason": check.reason,
                    },
                )
                continue
            quote = self._nse.get_quote(check.canonical_symbol)
            valid_quotes.append(quote.model_dump())

        if not valid_quotes:
            latency = (time.perf_counter() - t0) * 1000.0
            result = ScanResult(
                candidates=[],
                selected_symbol=None,
                rejected_inputs=rejected_inputs,
                notes="All candidate symbols were rejected by the NSE allowlist.",
            )
            self._audit.record_handoff(
                agent="ScannerAgent",
                tool_or_action="scan_empty_or_rejected",
                status="no_candidates",
                latency_ms=latency,
                payload={"rejected_inputs": rejected_inputs},
            )
            return result

        user_prompt = (
            f"Max concurrent trades allowed: {self._settings.max_concurrent_trades}.\n"
            f"Validated NSE quotes:\n{json.dumps(valid_quotes, indent=2)}\n"
            f"Rejected inputs: {json.dumps(rejected_inputs)}"
        )
        result = self._llm.generate_structured(
            agent_name="ScannerAgent",
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_schema=ScanResult,
            metadata={"valid_quotes": valid_quotes, "rejected_inputs": rejected_inputs},
        )

        # Deterministic post-check: enforce allowlist & max_concurrent_trades selection
        valid_set = {q["symbol"] for q in valid_quotes}
        filtered_candidates = [c for c in result.candidates if c.symbol in valid_set]
        selected = (
            result.selected_symbol
            if result.selected_symbol in valid_set
            else (filtered_candidates[0].symbol if filtered_candidates else None)
        )
        final_result = ScanResult(
            candidates=filtered_candidates,
            selected_symbol=selected,
            rejected_inputs=rejected_inputs,
            notes=result.notes,
        )
        latency = (time.perf_counter() - t0) * 1000.0
        self._audit.record_handoff(
            agent="ScannerAgent",
            tool_or_action=f"selected:{final_result.selected_symbol}",
            status="ok",
            latency_ms=latency,
            payload={
                "selected_symbol": final_result.selected_symbol,
                "candidate_count": len(final_result.candidates),
                "rejected_count": len(rejected_inputs),
            },
        )
        return final_result
