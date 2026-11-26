"""Isolated write-only Zerodha execution boundary with synchronous HITL gating.

Security invariants:
1. KITE_API_KEY and KITE_ACCESS_TOKEN are loaded ONLY inside this module.
2. Credentials are stored in pydantic.SecretStr, never serialized to logs or prompts.
3. Every order requires an ephemeral HMAC-signed HITLApprovalToken issued only after
   synchronous human confirmation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from kiteconnect import KiteConnect
from pydantic import SecretStr

from schemas.models import HITLApprovalToken, HITLDecision, OrderReceipt, OrderRequest


class HITLBypassError(PermissionError):
    """Raised when order execution is attempted without a valid signed HITL token."""


@dataclass(frozen=True)
class _KiteCredentials:
    api_key: SecretStr
    access_token: SecretStr

    def __repr__(self) -> str:
        return "_KiteCredentials(api_key='[REDACTED]', access_token='[REDACTED]')"


def _load_kite_credentials() -> _KiteCredentials | None:
    """Load brokerage credentials exclusively from environment inside execution layer."""
    api_key = os.getenv("KITE_API_KEY", "").strip()
    access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    if not api_key or not access_token:
        return None
    return _KiteCredentials(
        api_key=SecretStr(api_key),
        access_token=SecretStr(access_token),
    )


def compute_order_hash(order: OrderRequest) -> str:
    """Compute a deterministic SHA-256 digest of the validated order payload."""
    canonical = order.model_dump_json()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HITLGate:
    """Synchronous Human-in-the-Loop gate minting process-local HMAC approval tokens."""

    def __init__(self, ttl_seconds: float = 120.0) -> None:
        self._signing_secret = os.urandom(32)
        self._ttl_seconds = ttl_seconds

    def _sign(self, trace_id: str, order_hash: str, issued_at: float, expires_at: float) -> str:
        message = f"{trace_id}:{order_hash}:{issued_at:.6f}:{expires_at:.6f}".encode("utf-8")
        return hmac.new(self._signing_secret, message, hashlib.sha256).hexdigest()

    def mint_token(self, order: OrderRequest) -> HITLApprovalToken:
        """Issue an ephemeral approval token bound to trace_id and order_hash."""
        now = time.time()
        expires = now + self._ttl_seconds
        order_hash = compute_order_hash(order)
        sig = self._sign(order.trace_id, order_hash, now, expires)
        return HITLApprovalToken(
            trace_id=order.trace_id,
            order_hash=order_hash,
            issued_at=now,
            expires_at=expires,
            signature=sig,
        )

    def verify_token(self, order: OrderRequest, token: HITLApprovalToken | None) -> None:
        """Raise HITLBypassError if the token is missing, expired, mismatched, or forged."""
        if token is None:
            raise HITLBypassError("Missing HITL approval token. Synchronous approval is required.")
        if token.trace_id != order.trace_id:
            raise HITLBypassError("HITL token trace_id mismatch.")
        expected_hash = compute_order_hash(order)
        if not hmac.compare_digest(token.order_hash, expected_hash):
            raise HITLBypassError("HITL token order payload mismatch.")
        if time.time() > token.expires_at:
            raise HITLBypassError("HITL approval token expired.")
        expected_sig = self._sign(
            token.trace_id, token.order_hash, token.issued_at, token.expires_at
        )
        if not hmac.compare_digest(token.signature, expected_sig):
            raise HITLBypassError("Invalid HITL approval token signature.")

    def request_approval(
        self,
        order: OrderRequest,
        live_ltp: float,
        input_fn: Callable[[str], str] = input,
        echo_fn: Callable[[str], None] = print,
    ) -> tuple[HITLDecision, HITLApprovalToken | None]:
        """Display validated order payload and require synchronous [Approve / Reject] input."""
        notional = order.quantity * order.price
        echo_fn("\n=== HUMAN-IN-THE-LOOP (HITL) ORDER APPROVAL GATE ===")
        echo_fn(f"Trace ID   : {order.trace_id}")
        echo_fn(f"Instrument : {order.exchange}:{order.symbol}")
        echo_fn(f"Action     : {order.transaction_type} {order.quantity} ({order.product})")
        echo_fn(f"Order Type : {order.order_type} @ INR {order.price:.2f} (LTP: {live_ltp:.2f})")
        echo_fn(f"Stop Loss  : INR {order.stop_loss:.2f}")
        echo_fn(f"Notional   : INR {notional:,.2f}")
        echo_fn("====================================================")

        raw_response = input_fn("Confirm execution? [Approve / Reject]: ").strip().lower()
        approved = raw_response in {"approve", "approved", "y", "yes"}
        decision = HITLDecision(
            approved=approved,
            reason="approved_by_operator" if approved else f"rejected_by_operator ({raw_response})",
        )
        if not approved:
            return decision, None
        return decision, self.mint_token(order)


class ZerodhaExecutor:
    """Write-only brokerage wrapper isolated from agents, prompts, and MCP tools."""

    def __init__(self, hitl_gate: HITLGate, dry_run: bool = True) -> None:
        self._hitl_gate = hitl_gate
        self._dry_run = dry_run
        self._credentials = _load_kite_credentials()

    def __repr__(self) -> str:
        return f"ZerodhaExecutor(dry_run={self._dry_run}, credentials='[REDACTED]')"

    def _scrub_secrets(self, message: str) -> str:
        if self._credentials is None:
            return message
        scrubbed = message
        for secret in (
            self._credentials.api_key.get_secret_value(),
            self._credentials.access_token.get_secret_value(),
        ):
            if secret:
                scrubbed = scrubbed.replace(secret, "[REDACTED]")
        return scrubbed

    def place_order(
        self,
        order: OrderRequest,
        approval_token: HITLApprovalToken | None,
    ) -> OrderReceipt:
        """Execute a validated order ONLY after verifying a signed HITL approval token."""
        self._hitl_gate.verify_token(order, approval_token)
        now_iso = datetime.now(timezone.utc).isoformat()

        if self._dry_run:
            return OrderReceipt(
                trace_id=order.trace_id,
                order_id=f"DRY-{uuid.uuid4().hex[:10].upper()}",
                status="DRY_RUN_EXECUTED",
                symbol=order.symbol,
                transaction_type=order.transaction_type,
                quantity=order.quantity,
                price=order.price,
                dry_run=True,
                timestamp=now_iso,
            )

        if self._credentials is None:
            raise RuntimeError(
                "Live brokerage mode (DRY_RUN=false) requires KITE_API_KEY and KITE_ACCESS_TOKEN."
            )

        try:
            kite = KiteConnect(api_key=self._credentials.api_key.get_secret_value())
            kite.set_access_token(self._credentials.access_token.get_secret_value())
            variety = getattr(kite, "VARIETY_REGULAR", "regular")
            exchange = getattr(kite, "EXCHANGE_NSE", "NSE")
            txn_type = (
                getattr(kite, "TRANSACTION_TYPE_BUY", "BUY")
                if order.transaction_type == "BUY"
                else getattr(kite, "TRANSACTION_TYPE_SELL", "SELL")
            )
            order_type = (
                getattr(kite, "ORDER_TYPE_LIMIT", "LIMIT")
                if order.order_type == "LIMIT"
                else getattr(kite, "ORDER_TYPE_MARKET", "MARKET")
            )
            product = (
                getattr(kite, "PRODUCT_CNC", "CNC")
                if order.product == "CNC"
                else getattr(kite, "PRODUCT_MIS", "MIS")
            )
            kite_order_id = kite.place_order(
                variety=variety,
                exchange=exchange,
                tradingsymbol=order.symbol,
                transaction_type=txn_type,
                quantity=order.quantity,
                product=product,
                order_type=order_type,
                price=order.price if order.order_type == "LIMIT" else None,
                tag=order.order_tag,
            )
            return OrderReceipt(
                trace_id=order.trace_id,
                order_id=str(kite_order_id),
                status="LIVE_SUBMITTED",
                symbol=order.symbol,
                transaction_type=order.transaction_type,
                quantity=order.quantity,
                price=order.price,
                dry_run=False,
                timestamp=now_iso,
            )
        except Exception as exc:
            raise RuntimeError(self._scrub_secrets(f"Zerodha order failed: {exc}")) from None
