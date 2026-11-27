"""Read-only Agent Analysis Microservice (Zero Zerodha Credentials).

Runs Scanner -> Sentiment (+ Sanitizer) -> ChartAnalyst (+ MarketMood & Corp Actions)
and returns a validated OrderRequest proposal or NO_TRADE outcome.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from agents.chart_analyst import ChartAnalystAgent
from agents.llm_client import build_llm_client
from agents.scanner import ScannerAgent
from agents.sentiment import SentimentAgent
from config import AppSettings, get_settings
from guardrails.validator import BoundaryValidator, GuardrailViolation
from mcp_client.nse_client import NSEClient, NSEMCPError
from observability.logger import AuditLogger
from schemas.models import (
    CorporateActionEvent,
    MarketMood,
    OrderRequest,
    ScanResult,
    SentimentReport,
    TechnicalIndicators,
    TradeAction,
    TradeProposal,
)

# Defense-in-depth: strip any brokerage vars from the read-only agent service process
for _forbidden in ("KITE_API_KEY", "KITE_ACCESS_TOKEN"):
    os.environ.pop(_forbidden, None)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(default_factory=lambda: ["INFY", "RELIANCE", "ICICIBANK"])


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    status: str
    scan_result: ScanResult | None = None
    sentiment_report: SentimentReport | None = None
    technical_indicators: TechnicalIndicators | None = None
    market_mood: MarketMood | None = None
    corporate_actions_applied: list[CorporateActionEvent] = Field(default_factory=list)
    proposal: TradeProposal | None = None
    validated_order: OrderRequest | None = None
    guardrail_reasons: list[str] = Field(default_factory=list)


def create_agent_app(settings: AppSettings | None = None) -> FastAPI:
    """Create the read-only FastAPI analysis application."""
    cfg = settings or get_settings()
    app = FastAPI(
        title="NSE Read-Only Agent Service",
        version="0.2.0",
        description="Isolated read-only 3-agent analysis pipeline with zero brokerage credentials.",
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "agent_readonly",
            "has_kite_credentials": False,
            "nse_mcp_mode": cfg.nse_mcp_mode,
        }

    @app.post("/analyze", response_model=AnalyzeResponse)
    def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
        audit = AuditLogger(log_path=cfg.audit_log_path)
        nse = NSEClient(
            mode=cfg.nse_mcp_mode,
            cm_market_url=cfg.nse_cm_market_mcp_url,
            bhavcopy_url=cfg.nse_bhavcopy_mcp_url,
            enable_live_news_rss=cfg.enable_live_news_rss,
            event_hook=audit.as_nse_event_hook(),
        )
        llm = build_llm_client(cfg)
        scanner = ScannerAgent(nse, llm, cfg, audit)
        sentiment_agent = SentimentAgent(nse, llm, audit)
        chart_agent = ChartAnalystAgent(nse, llm, audit)

        try:
            scan_result = scanner.run(req.symbols)
            if not scan_result.selected_symbol:
                return AnalyzeResponse(
                    trace_id=audit.trace_id,
                    status="NO_TRADE",
                    scan_result=scan_result,
                    guardrail_reasons=list(scan_result.rejected_inputs),
                )
            sym = scan_result.selected_symbol
            sentiment_report = sentiment_agent.run(sym)
            indicators, quote, proposal, mood, actions = chart_agent.run(
                sym,
                sentiment_report,
                high_vix_threshold=cfg.high_vix_threshold,
                min_ad_ratio=cfg.min_ad_ratio_for_breakout,
            )
        except NSEMCPError as exc:
            return AnalyzeResponse(
                trace_id=audit.trace_id,
                status="MCP_ERROR_ABORTED",
                guardrail_reasons=[str(exc)],
            )

        if proposal.action == TradeAction.NO_TRADE:
            return AnalyzeResponse(
                trace_id=audit.trace_id,
                status="NO_TRADE",
                scan_result=scan_result,
                sentiment_report=sentiment_report,
                technical_indicators=indicators,
                market_mood=mood,
                corporate_actions_applied=actions,
                proposal=proposal,
            )

        allowed_set = (
            {c.symbol.upper() for c in scan_result.candidates}
            if nse.mode == "nse_live" and scan_result.candidates
            else None
        )
        try:
            order = BoundaryValidator.validate_proposal(
                trace_id=audit.trace_id,
                proposal=proposal,
                scan_result=scan_result,
                sentiment=sentiment_report,
                indicators=indicators,
                live_quote=quote,
                settings=cfg,
                allowed_symbols=allowed_set,
            )
        except GuardrailViolation as exc:
            return AnalyzeResponse(
                trace_id=audit.trace_id,
                status="BLOCKED_GUARDRAIL",
                scan_result=scan_result,
                sentiment_report=sentiment_report,
                technical_indicators=indicators,
                market_mood=mood,
                corporate_actions_applied=actions,
                proposal=proposal,
                guardrail_reasons=exc.reasons,
            )

        return AnalyzeResponse(
            trace_id=audit.trace_id,
            status="READY_FOR_HITL",
            scan_result=scan_result,
            sentiment_report=sentiment_report,
            technical_indicators=indicators,
            market_mood=mood,
            corporate_actions_applied=actions,
            proposal=proposal,
            validated_order=order,
        )

    return app


app = create_agent_app()
