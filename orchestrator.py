"""End-to-end 3-agent orchestration pipeline with deterministic guardrails and HITL gate."""

from __future__ import annotations

import time
from typing import Callable

from agents.chart_analyst import ChartAnalystAgent
from agents.llm_client import MockAgentOverrides, StructuredLLMClient, build_llm_client
from agents.scanner import ScannerAgent
from agents.sentiment import SentimentAgent
from config import AppSettings, get_settings
from execution.zerodha_executor import HITLGate, ZerodhaExecutor
from guardrails.validator import BoundaryValidator, GuardrailViolation
from mcp_client.nse_client import NSEClient, NSEMCPError
from observability.logger import AuditLogger
from schemas.models import OrchestrationOutcome, TradeAction


class TradingOrchestrator:
    """Coordinates read-only agents -> deterministic validator -> HITL gate -> ZerodhaExecutor."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        nse_client: NSEClient | None = None,
        llm_client: StructuredLLMClient | None = None,
        hitl_gate: HITLGate | None = None,
        executor: ZerodhaExecutor | None = None,
        audit_logger: AuditLogger | None = None,
        llm_overrides: MockAgentOverrides | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.audit = audit_logger or AuditLogger(log_path=self.settings.audit_log_path)
        self.nse_client = nse_client or NSEClient(
            mode=self.settings.nse_mcp_mode,
            cm_market_url=self.settings.nse_cm_market_mcp_url,
            bhavcopy_url=self.settings.nse_bhavcopy_mcp_url,
            event_hook=self.audit.as_nse_event_hook(),
        )
        if nse_client is not None and nse_client._event_hook is None:
            nse_client._event_hook = self.audit.as_nse_event_hook()

        self.llm_client = llm_client or build_llm_client(self.settings, overrides=llm_overrides)
        self.hitl_gate = hitl_gate or HITLGate()
        self.executor = executor or ZerodhaExecutor(
            hitl_gate=self.hitl_gate,
            dry_run=self.settings.dry_run,
        )

        self.scanner = ScannerAgent(self.nse_client, self.llm_client, self.settings, self.audit)
        self.sentiment = SentimentAgent(self.nse_client, self.llm_client, self.audit)
        self.chart_analyst = ChartAnalystAgent(self.nse_client, self.llm_client, self.audit)

    def run(
        self,
        raw_candidates: list[str] | None = None,
        hitl_input_fn: Callable[[str], str] = input,
        hitl_echo_fn: Callable[[str], None] = print,
    ) -> OrchestrationOutcome:
        """Execute the full pipeline for a set of candidate tickers."""
        t0 = time.perf_counter()
        self.audit.log_event(
            event_type="pipeline_start",
            component="orchestrator",
            status="started",
            payload={"raw_candidates": raw_candidates, "dry_run": self.settings.dry_run},
        )

        try:
            scan_result = self.scanner.run(raw_candidates)
        except NSEMCPError as exc:
            self.audit.log_event(
                event_type="pipeline_abort_mcp_error",
                component="orchestrator",
                status="aborted",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                payload={"error": str(exc)},
            )
            return OrchestrationOutcome(
                trace_id=self.audit.trace_id,
                status="MCP_ERROR_ABORTED",
                handoffs=list(self.audit.handoffs),
                guardrail_reasons=[str(exc)],
            )

        hallucination_blocked = len(scan_result.rejected_inputs) > 0
        if not scan_result.selected_symbol:
            return OrchestrationOutcome(
                trace_id=self.audit.trace_id,
                status="NO_TRADE",
                scan_result=scan_result,
                handoffs=list(self.audit.handoffs),
                guardrail_reasons=list(scan_result.rejected_inputs),
                hallucination_blocked=hallucination_blocked,
            )

        target_symbol = scan_result.selected_symbol

        try:
            sentiment_report = self.sentiment.run(target_symbol)
            indicators, live_quote, proposal = self.chart_analyst.run(
                target_symbol, sentiment_report
            )
        except NSEMCPError as exc:
            self.audit.log_event(
                event_type="pipeline_abort_mcp_error",
                component="orchestrator",
                status="aborted",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                payload={"symbol": target_symbol, "error": str(exc)},
            )
            return OrchestrationOutcome(
                trace_id=self.audit.trace_id,
                status="MCP_ERROR_ABORTED",
                scan_result=scan_result,
                handoffs=list(self.audit.handoffs),
                guardrail_reasons=[str(exc)],
                hallucination_blocked=hallucination_blocked,
            )

        injection_detected = len(sentiment_report.sanitization_flags) > 0

        if proposal.action == TradeAction.NO_TRADE:
            self.audit.log_event(
                event_type="pipeline_no_trade",
                component="orchestrator",
                status="no_trade",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                payload={"symbol": target_symbol, "rationale": proposal.rationale},
            )
            return OrchestrationOutcome(
                trace_id=self.audit.trace_id,
                status="NO_TRADE",
                scan_result=scan_result,
                sentiment_report=sentiment_report,
                technical_indicators=indicators,
                proposal=proposal,
                handoffs=list(self.audit.handoffs),
                injection_detected=injection_detected,
                hallucination_blocked=hallucination_blocked,
            )

        # Deterministic boundary validation before HITL gate or Zerodha execution
        allowed_set: set[str] | None = (
            {c.symbol.upper() for c in scan_result.candidates}
            if self.nse_client.mode == "nse_live" and scan_result.candidates
            else None
        )
        try:
            validated_order = BoundaryValidator.validate_proposal(
                trace_id=self.audit.trace_id,
                proposal=proposal,
                scan_result=scan_result,
                sentiment=sentiment_report,
                indicators=indicators,
                live_quote=live_quote,
                settings=self.settings,
                caller_role=BoundaryValidator.ALLOWED_EXECUTION_CALLER,
                allowed_symbols=allowed_set,
            )
        except GuardrailViolation as violation:
            self.audit.record_handoff(
                agent="BoundaryValidator",
                tool_or_action="block_proposal",
                status="blocked",
                payload={"reasons": violation.reasons},
            )
            self.audit.log_event(
                event_type="guardrail_rejection",
                component="guardrails.validator",
                status="blocked",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                payload={"symbol": proposal.symbol, "reasons": violation.reasons},
            )
            return OrchestrationOutcome(
                trace_id=self.audit.trace_id,
                status="BLOCKED_GUARDRAIL",
                scan_result=scan_result,
                sentiment_report=sentiment_report,
                technical_indicators=indicators,
                proposal=proposal,
                handoffs=list(self.audit.handoffs),
                guardrail_reasons=violation.reasons,
                injection_detected=injection_detected,
                hallucination_blocked=hallucination_blocked
                or any("ticker_allowlist_rejected" in r for r in violation.reasons),
            )

        self.audit.record_handoff(
            agent="BoundaryValidator",
            tool_or_action=f"validated_order:{validated_order.symbol}",
            status="ok",
        )

        # Synchronous Human-in-the-Loop Gate (cannot be bypassed by LLM flags)
        decision, approval_token = self.hitl_gate.request_approval(
            order=validated_order,
            live_ltp=live_quote.ltp,
            input_fn=hitl_input_fn,
            echo_fn=hitl_echo_fn,
        )
        self.audit.record_handoff(
            agent="HITLGate",
            tool_or_action="approve" if decision.approved else "reject",
            status="approved" if decision.approved else "rejected",
            payload={"reason": decision.reason},
        )

        if not decision.approved or approval_token is None:
            return OrchestrationOutcome(
                trace_id=self.audit.trace_id,
                status="HITL_REJECTED",
                scan_result=scan_result,
                sentiment_report=sentiment_report,
                technical_indicators=indicators,
                proposal=proposal,
                validated_order=validated_order,
                handoffs=list(self.audit.handoffs),
                injection_detected=injection_detected,
                hallucination_blocked=hallucination_blocked,
                hitl_prompted=True,
                hitl_bypassed=False,
            )

        receipt = self.executor.place_order(validated_order, approval_token)
        self.audit.record_handoff(
            agent="ZerodhaExecutor",
            tool_or_action=f"place_order:{receipt.status}",
            status="ok",
            payload={"order_id": receipt.order_id, "dry_run": receipt.dry_run},
        )
        return OrchestrationOutcome(
            trace_id=self.audit.trace_id,
            status=receipt.status,  # type: ignore[arg-type]
            scan_result=scan_result,
            sentiment_report=sentiment_report,
            technical_indicators=indicators,
            proposal=proposal,
            validated_order=validated_order,
            receipt=receipt,
            handoffs=list(self.audit.handoffs),
            injection_detected=injection_detected,
            hallucination_blocked=hallucination_blocked,
            hitl_prompted=True,
            hitl_bypassed=False,
        )
