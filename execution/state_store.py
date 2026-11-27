"""SQLite persistence for order idempotency, open positions, and HITL decision history."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from schemas.models import HITLDecision, OrderReceipt, OrderRequest


class IdempotencyConflictError(RuntimeError):
    """Raised when an order with the same daily idempotency key was already executed."""


class SQLiteStateStore:
    """Thread-safe local SQLite store for execution idempotency and HITL audit trails."""

    def __init__(self, db_path: str | Path = "data/trading_state.db") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS executed_orders (
                    idempotency_key TEXT PRIMARY KEY,
                    trading_date TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    transaction_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hitl_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def build_idempotency_key(
        order: OrderRequest, trading_date: str | None = None
    ) -> str:
        """Build deterministic daily idempotency key (trading_date:symbol:side)."""
        day = trading_date or date.today().isoformat()
        return f"{day}:{order.symbol.upper()}:{order.transaction_type.upper()}"

    def has_executed_order(self, idempotency_key: str) -> bool:
        """Return True if an order with this idempotency key was already recorded."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM executed_orders WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return row is not None

    def list_open_symbols(self, trading_date: str | None = None) -> list[str]:
        """Return active BUY symbols recorded for trading_date (or all if None)."""
        day = trading_date or date.today().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT symbol FROM executed_orders
                WHERE trading_date = ? AND transaction_type = 'BUY'
                """,
                (day,),
            ).fetchall()
            return [str(r["symbol"]) for r in rows]

    def record_hitl_decision(
        self, trace_id: str, symbol: str, decision: HITLDecision
    ) -> None:
        """Persist a synchronous HITL decision."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO hitl_decisions (
                    trace_id, symbol, approved, reviewer, reason, decided_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    symbol.upper(),
                    1 if decision.approved else 0,
                    decision.reviewer,
                    decision.reason,
                    decision.decided_at,
                ),
            )

    def record_order(
        self,
        order: OrderRequest,
        receipt: OrderReceipt,
        trading_date: str | None = None,
    ) -> str:
        """Persist an executed order; raise IdempotencyConflictError on duplicate key."""
        day = trading_date or date.today().isoformat()
        key = self.build_idempotency_key(order, trading_date=day)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO executed_orders (
                        idempotency_key, trading_date, trace_id, symbol,
                        transaction_type, quantity, price, status, order_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        day,
                        order.trace_id,
                        order.symbol.upper(),
                        order.transaction_type,
                        order.quantity,
                        order.price,
                        receipt.status,
                        receipt.order_id,
                        now_iso,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise IdempotencyConflictError(
                f"Duplicate order blocked by idempotency key '{key}'."
            ) from exc
        return key
