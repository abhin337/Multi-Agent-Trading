"""Core Pydantic v2 data contracts for the NSE multi-agent trading pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TradeAction(str, Enum):
    """Allowed trade proposal actions."""

    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class NSEQuote(BaseModel):
    """Read-only market quote returned by NSE MCP."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    exchange: Literal["NSE"] = "NSE"
    ltp: float = Field(gt=0)
    open_price: float = Field(gt=0)
    high_price: float = Field(gt=0)
    low_price: float = Field(gt=0)
    volume: int = Field(ge=0)
    change_pct: float
    timestamp: str


class OHLCVBar(BaseModel):
    """Single daily OHLCV candlestick bar."""

    model_config = ConfigDict(extra="forbid")

    date: str
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)


class MarketNewsItem(BaseModel):
    """Raw external news or social item before sanitization."""

    model_config = ConfigDict(extra="forbid")

    source: str
    headline: str
    body: str
    published_at: str


class SanitizedText(BaseModel):
    """Result of passing untrusted external text through the prompt sanitizer."""

    model_config = ConfigDict(extra="forbid")

    cleaned_text: str
    injection_detected: bool = False
    matched_rules: list[str] = Field(default_factory=list)


class TickerCandidate(BaseModel):
    """Candidate symbol selected by the Scanner agent."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    rank: int = Field(ge=1)
    scanner_score: float = Field(ge=0.0, le=1.0)
    catalyst: str


class ScanResult(BaseModel):
    """Structured output from ScannerAgent."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[TickerCandidate] = Field(default_factory=list)
    selected_symbol: str | None = None
    rejected_inputs: list[str] = Field(default_factory=list)
    notes: str = ""


class SentimentReport(BaseModel):
    """Structured output from SentimentAgent."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    conflict_detected: bool = False
    sanitization_flags: list[str] = Field(default_factory=list)
    summary: str


class CorporateActionEvent(BaseModel):
    """Normalized NSE corporate action event (split, bonus, dividend)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    action_type: Literal["SPLIT", "BONUS", "DIVIDEND", "OTHER"]
    ex_date: str
    subject: str
    adjustment_factor: float = Field(default=1.0, gt=0)


class MarketMood(BaseModel):
    """Broad NSE market regime snapshot (India VIX + advance/decline breadth)."""

    model_config = ConfigDict(extra="forbid")

    date: str
    india_vix: float = Field(ge=0.0)
    advances: int = Field(ge=0)
    declines: int = Field(ge=0)
    ad_ratio: float = Field(ge=0.0)
    regime: Literal["BULLISH", "NEUTRAL", "HIGH_VIX_RISK_OFF"]


class PortfolioSnapshot(BaseModel):
    """Isolated execution-layer view of active positions and available equity margin."""

    model_config = ConfigDict(extra="forbid")

    open_positions_count: int = Field(ge=0)
    open_symbols: list[str] = Field(default_factory=list)
    available_margin_inr: float = Field(ge=0.0)
    source: Literal["simulated_sqlite", "kite_live"]


class TechnicalIndicators(BaseModel):
    """Deterministically computed indicators from raw OHLCV data."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    ltp: float = Field(gt=0)
    rsi_14: float = Field(ge=0.0, le=100.0)
    ema_20: float = Field(gt=0)
    ema_50: float = Field(gt=0)
    support: float = Field(gt=0)
    resistance: float = Field(gt=0)
    volume_ratio: float = Field(ge=0.0)
    bullish_breakout: bool
    market_regime: Literal["BULLISH", "NEUTRAL", "HIGH_VIX_RISK_OFF"] = "NEUTRAL"


class TradeProposal(BaseModel):
    """Structured output from ChartAnalystAgent before deterministic guardrails."""

    model_config = ConfigDict(extra="forbid")

    action: TradeAction
    symbol: str
    quantity: int = Field(ge=0)
    order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    limit_price: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    rationale: str
    source_symbols: list[str] = Field(default_factory=list)
    override_hitl_requested: bool = False


class OrderRequest(BaseModel):
    """Validated Zerodha-ready order payload after all deterministic guardrails pass."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    symbol: str
    exchange: Literal["NSE"] = "NSE"
    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    product: Literal["CNC", "MIS"] = "CNC"
    order_tag: str = "AGY_VALIDATED"


class HITLDecision(BaseModel):
    """Human-in-the-Loop synchronous approval decision."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    reviewer: str = "cli_operator"
    reason: str = ""
    decided_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class HITLApprovalToken(BaseModel):
    """Ephemeral HMAC-signed approval token bound to a trace_id and order payload hash."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    order_hash: str
    issued_at: float
    expires_at: float
    signature: str


class OrderReceipt(BaseModel):
    """Execution response from ZerodhaExecutor (dry-run or live KiteConnect)."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    order_id: str
    status: Literal["DRY_RUN_EXECUTED", "LIVE_SUBMITTED", "REJECTED"]
    symbol: str
    transaction_type: Literal["BUY", "SELL"]
    quantity: int
    price: float
    dry_run: bool
    timestamp: str


class AgentHandoff(BaseModel):
    """Recorded agent step / tool invocation for evals and observability."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    tool_or_action: str
    status: str = "ok"


class AuditEvent(BaseModel):
    """Single correlated JSON audit log record."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str
    trace_id: str
    event_type: str
    component: str
    status: str
    latency_ms: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)


class OrchestrationOutcome(BaseModel):
    """End-to-end result of a single pipeline run."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    status: Literal[
        "NO_TRADE",
        "BLOCKED_GUARDRAIL",
        "MCP_ERROR_ABORTED",
        "HITL_REJECTED",
        "DRY_RUN_EXECUTED",
        "LIVE_SUBMITTED",
    ]
    scan_result: ScanResult | None = None
    sentiment_report: SentimentReport | None = None
    technical_indicators: TechnicalIndicators | None = None
    market_mood: MarketMood | None = None
    corporate_actions_applied: list[CorporateActionEvent] = Field(default_factory=list)
    proposal: TradeProposal | None = None
    validated_order: OrderRequest | None = None
    receipt: OrderReceipt | None = None
    handoffs: list[AgentHandoff] = Field(default_factory=list)
    guardrail_reasons: list[str] = Field(default_factory=list)
    injection_detected: bool = False
    hallucination_blocked: bool = False
    hitl_prompted: bool = False
    hitl_bypassed: bool = False


class EvaluationRecord(BaseModel):
    """Required structured JSON line emitted by the 20-case eval runner."""

    model_config = ConfigDict(extra="forbid")

    test_id: str
    category: str
    agent_handoffs: list[dict[str, str]]
    schema_valid: bool
    hallucinated_ticker: bool
    injection_blocked: bool
    hitl_bypassed: bool
    passed: bool
    details: str = ""
