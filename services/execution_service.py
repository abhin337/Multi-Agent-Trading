"""Isolated Write-Only Zerodha Execution Microservice.

Holds KITE_* credentials exclusively inside this service boundary, enforces
inter-service HMAC verification (when configured), HITL approval tokens,
portfolio/margin checks, and SQLite order idempotency.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from config import AppSettings, get_settings
from execution.state_store import IdempotencyConflictError, SQLiteStateStore
from execution.zerodha_executor import (
    HITLBypassError,
    HITLGate,
    PortfolioConstraintError,
    ZerodhaExecutor,
)
from schemas.models import (
    HITLApprovalToken,
    OrderReceipt,
    OrderRequest,
    PortfolioSnapshot,
)


class ExecuteOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: OrderRequest
    approval_token: HITLApprovalToken | None = None


def verify_service_signature(
    payload_bytes: bytes, signature: str | None, secret: str | None
) -> None:
    """Verify optional HMAC-SHA256 inter-service signature between agent & execution services."""
    if not secret:
        return
    if not signature:
        raise HTTPException(status_code=401, detail="Missing X-Service-Signature header.")
    expected = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="Invalid X-Service-Signature.")


def create_execution_app(
    settings: AppSettings | None = None,
    hitl_gate: HITLGate | None = None,
    state_store: SQLiteStateStore | None = None,
) -> FastAPI:
    """Create the isolated write-only execution FastAPI application."""
    cfg = settings or get_settings()
    gate = hitl_gate or HITLGate()
    store = state_store or SQLiteStateStore(db_path=cfg.state_db_path)
    executor = ZerodhaExecutor(
        hitl_gate=gate,
        dry_run=cfg.dry_run,
        state_store=store,
        max_concurrent_trades=cfg.max_concurrent_trades,
        simulated_available_margin_inr=cfg.simulated_available_margin_inr,
    )

    app = FastAPI(
        title="Zerodha Isolated Execution Service",
        version="0.2.0",
        description=(
            "Write-only brokerage boundary with HITL token, margin & idempotency enforcement."
        ),
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "service": "execution_writeonly",
            "dry_run": cfg.dry_run,
        }

    @app.get("/portfolio", response_model=PortfolioSnapshot)
    def portfolio() -> PortfolioSnapshot:
        return executor.get_portfolio_snapshot()

    @app.post("/hitl/approve", response_model=HITLApprovalToken)
    def approve_hitl(order: OrderRequest) -> HITLApprovalToken:
        """Human operator endpoint issuing an ephemeral signed HITLApprovalToken."""
        return gate.mint_token(order)

    @app.post("/orders/execute", response_model=OrderReceipt)
    def execute_order(
        req: ExecuteOrderRequest,
        x_service_signature: str | None = Header(default=None),
    ) -> OrderReceipt:
        secret = cfg.inter_service_secret.get_secret_value() if cfg.inter_service_secret else None
        verify_service_signature(
            req.order.model_dump_json().encode("utf-8"),
            x_service_signature,
            secret,
        )
        try:
            return executor.place_order(req.order, req.approval_token)
        except HITLBypassError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except (IdempotencyConflictError, PortfolioConstraintError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    return app


app = create_execution_app()
