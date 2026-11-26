"""Correlated structured JSON audit logging with automatic secret redaction."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schemas.models import AgentHandoff, AuditEvent

_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|bearer|kite[_-]?secret)\s*[:=]\s*['\"]?([A-Za-z0-9._-]{4,})['\"]?"
)
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "access_token",
        "kite_api_key",
        "kite_access_token",
        "gemini_api_key",
        "authorization",
        "password",
        "secret",
        "signature",
    }
)


class AuditLogger:
    """Writes trace-correlated JSONL events while redacting any sensitive credentials."""

    def __init__(
        self,
        log_path: str | Path = "logs/audit.jsonl",
        trace_id: str | None = None,
        extra_secrets: list[str] | None = None,
    ) -> None:
        self.trace_id: str = trace_id or uuid.uuid4().hex
        self.log_path: Path = Path(log_path)
        self.events: list[AuditEvent] = []
        self.handoffs: list[AgentHandoff] = []
        self._literal_secrets: list[str] = self._collect_secrets(extra_secrets or [])

    @staticmethod
    def _collect_secrets(extra: list[str]) -> list[str]:
        candidates = list(extra)
        for env_name in ("KITE_API_KEY", "KITE_ACCESS_TOKEN", "GEMINI_API_KEY"):
            val = os.getenv(env_name, "").strip()
            if len(val) >= 4:
                candidates.append(val)
        return [s for s in candidates if len(s) >= 4]

    def _redact_string(self, text: str) -> str:
        scrubbed = text
        for secret in self._literal_secrets:
            scrubbed = scrubbed.replace(secret, "[REDACTED]")
        scrubbed = _KEY_VALUE_SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", scrubbed)
        return scrubbed

    def _redact_value(self, value: Any, key_hint: str | None = None) -> Any:
        if key_hint and key_hint.lower() in _SENSITIVE_FIELD_NAMES:
            return "[REDACTED]"
        if isinstance(value, str):
            return self._redact_string(value)
        if isinstance(value, dict):
            return {str(k): self._redact_value(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value

    def log_event(
        self,
        event_type: str,
        component: str,
        status: str,
        latency_ms: float = 0.0,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Record and persist a single correlated JSON audit event."""
        safe_payload = self._redact_value(payload or {})
        event = AuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trace_id=self.trace_id,
            event_type=event_type,
            component=component,
            status=status,
            latency_ms=round(latency_ms, 3),
            payload=safe_payload,
        )
        self.events.append(event)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.model_dump()) + "\n")
        return event

    def record_handoff(
        self,
        agent: str,
        tool_or_action: str,
        status: str = "ok",
        latency_ms: float = 0.0,
        payload: dict[str, Any] | None = None,
    ) -> AgentHandoff:
        """Record an ordered agent handoff or tool invocation."""
        handoff = AgentHandoff(agent=agent, tool_or_action=tool_or_action, status=status)
        self.handoffs.append(handoff)
        merged_payload = {"agent": agent, "tool_or_action": tool_or_action}
        if payload:
            merged_payload.update(payload)
        self.log_event(
            event_type="agent_handoff",
            component=agent,
            status=status,
            latency_ms=latency_ms,
            payload=merged_payload,
        )
        return handoff

    def as_nse_event_hook(self):
        """Return an event callback compatible with NSEClient."""

        def _hook(
            event_type: str,
            component: str,
            status: str,
            latency_ms: float,
            payload: dict[str, Any],
        ) -> None:
            self.log_event(
                event_type=event_type,
                component=component,
                status=status,
                latency_ms=latency_ms,
                payload=payload,
            )

        return _hook
