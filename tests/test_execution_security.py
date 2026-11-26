"""Tests for HITL cryptographic gating, Zerodha credential isolation, and log redaction."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from execution.zerodha_executor import HITLBypassError, HITLGate, ZerodhaExecutor
from observability.logger import AuditLogger
from schemas.models import OrderRequest


def _sample_order(trace_id: str = "trace-123") -> OrderRequest:
    return OrderRequest(
        trace_id=trace_id,
        symbol="INFY",
        exchange="NSE",
        transaction_type="BUY",
        quantity=10,
        order_type="LIMIT",
        price=1565.0,
        stop_loss=1520.0,
    )


def test_hitl_token_required_and_bound_to_order() -> None:
    gate = HITLGate()
    executor = ZerodhaExecutor(hitl_gate=gate, dry_run=True)
    order = _sample_order()

    with pytest.raises(HITLBypassError):
        executor.place_order(order, approval_token=None)

    decision, token = gate.request_approval(
        order=order,
        live_ltp=1565.0,
        input_fn=lambda _: "Approve",
        echo_fn=lambda _: None,
    )
    assert decision.approved and token is not None

    # Tampering with order quantity invalidates the token hash
    tampered = order.model_copy(update={"quantity": 99})
    with pytest.raises(HITLBypassError):
        executor.place_order(tampered, approval_token=token)

    receipt = executor.place_order(order, approval_token=token)
    assert receipt.status == "DRY_RUN_EXECUTED"
    assert receipt.dry_run is True


def test_audit_logger_redacts_kite_secrets(tmp_path: Path) -> None:
    os.environ["KITE_API_KEY"] = "kite_key_super_secret"
    os.environ["KITE_ACCESS_TOKEN"] = "kite_tok_super_secret"
    try:
        log_file = tmp_path / "audit.jsonl"
        audit = AuditLogger(log_path=log_file)
        audit.log_event(
            event_type="test",
            component="test",
            status="ok",
            payload={
                "api_key": "kite_key_super_secret",
                "nested": {"msg": "Authorization=kite_tok_super_secret"},
            },
        )
        raw = log_file.read_text(encoding="utf-8")
        assert "kite_key_super_secret" not in raw
        assert "kite_tok_super_secret" not in raw
        assert "[REDACTED]" in raw
    finally:
        os.environ.pop("KITE_API_KEY", None)
        os.environ.pop("KITE_ACCESS_TOKEN", None)
