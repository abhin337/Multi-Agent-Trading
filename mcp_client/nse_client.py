"""Read-only NSE MCP client wrapper.

Supports:
- 'fixture' mode: deterministic local replay for offline tests and demos.
- 'stdio' mode: connects over MCP stdio transport to mcp_server/nse_server.py.

Zero Zerodha credentials or write-enabled brokerage methods exist in this module.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_server.fixtures import (
    ACTIVE_NSE_SYMBOLS,
    FIXTURE_NEWS,
    FIXTURE_OHLCV,
    FIXTURE_QUOTES,
)
from schemas.models import MarketNewsItem, NSEQuote, OHLCVBar


class NSEMCPError(RuntimeError):
    """Raised when the NSE MCP server times out, returns 503, or fails."""


EventHook = Callable[[str, str, str, float, dict[str, Any]], None]


class NSEClient:
    """Strictly read-only wrapper around NSE MCP data tools."""

    def __init__(
        self,
        mode: Literal["fixture", "stdio"] = "fixture",
        news_overrides: dict[str, list[MarketNewsItem]] | None = None,
        quote_overrides: dict[str, NSEQuote] | None = None,
        ohlcv_overrides: dict[str, list[OHLCVBar]] | None = None,
        simulate_error: str | None = None,
        event_hook: EventHook | None = None,
    ) -> None:
        self.mode = mode
        self._news_overrides = news_overrides or {}
        self._quote_overrides = quote_overrides or {}
        self._ohlcv_overrides = ohlcv_overrides or {}
        self._simulate_error = simulate_error
        self._event_hook = event_hook
        self._project_root = Path(__file__).resolve().parent.parent

    def _emit(
        self,
        event_type: str,
        status: str,
        latency_ms: float,
        payload: dict[str, Any],
    ) -> None:
        if self._event_hook is not None:
            self._event_hook(event_type, "mcp_client.nse_client", status, latency_ms, payload)

    def _check_fault_injection(self, tool_name: str, symbol: str | None = None) -> None:
        if self._simulate_error:
            raise NSEMCPError(
                f"NSE MCP {tool_name} failed ({self._simulate_error}) for symbol={symbol}"
            )

    def _stdio_server_params(self) -> StdioServerParameters:
        # Sanitize child environment so brokerage keys never cross into the MCP process.
        safe_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("KITE_") and k != "GEMINI_API_KEY"
        }
        safe_env["PYTHONPATH"] = str(self._project_root)
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server.nse_server"],
            env=safe_env,
        )

    async def _call_stdio_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        server_params = self._stdio_server_params()
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                if result.isError:
                    raise NSEMCPError(f"MCP tool '{name}' returned error: {result.content}")
                if not result.content:
                    return None
                text_parts = [
                    getattr(item, "text", "")
                    for item in result.content
                    if getattr(item, "text", None) is not None
                ]
                raw = "\n".join(text_parts).strip()
                if not raw:
                    return None
                return json.loads(raw)

    async def list_active_symbols(self) -> dict[str, str]:
        """List active NSE symbols via MCP."""
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("list_active_nse_symbols")
            if self.mode == "stdio":
                data = await self._call_stdio_tool("list_active_nse_symbols", {})
                result = dict(data)
            else:
                result = dict(ACTIVE_NSE_SYMBOLS)
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit("mcp_tool_call", "ok", latency, {"tool": "list_active_nse_symbols"})
            return result
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_error",
                "error",
                latency,
                {"tool": "list_active_nse_symbols", "error": str(exc)},
            )
            raise

    async def get_quote(self, symbol: str) -> NSEQuote:
        """Fetch read-only live quote snapshot for an active NSE symbol."""
        normalized = symbol.strip().upper()
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("get_nse_quote", normalized)
            if normalized in self._quote_overrides:
                quote = self._quote_overrides[normalized]
            elif self.mode == "stdio":
                data = await self._call_stdio_tool("get_nse_quote", {"symbol": normalized})
                quote = NSEQuote.model_validate(data)
            else:
                if normalized not in FIXTURE_QUOTES:
                    raise NSEMCPError(f"Symbol '{normalized}' not found in NSE fixture feed.")
                quote = FIXTURE_QUOTES[normalized]
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_call",
                "ok",
                latency,
                {"tool": "get_nse_quote", "symbol": normalized, "ltp": quote.ltp},
            )
            return quote
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_error",
                "error",
                latency,
                {"tool": "get_nse_quote", "symbol": normalized, "error": str(exc)},
            )
            if isinstance(exc, NSEMCPError):
                raise
            raise NSEMCPError(str(exc)) from exc

    async def get_historical_ohlcv(self, symbol: str, days: int = 30) -> list[OHLCVBar]:
        """Fetch historical OHLCV bars for an active NSE symbol."""
        normalized = symbol.strip().upper()
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("get_historical_ohlcv", normalized)
            if normalized in self._ohlcv_overrides:
                bars = self._ohlcv_overrides[normalized][-days:]
            elif self.mode == "stdio":
                data = await self._call_stdio_tool(
                    "get_historical_ohlcv", {"symbol": normalized, "days": days}
                )
                bars = [OHLCVBar.model_validate(row) for row in data]
            else:
                if normalized not in FIXTURE_OHLCV:
                    raise NSEMCPError(f"OHLCV unavailable for '{normalized}'.")
                bars = FIXTURE_OHLCV[normalized][-days:]
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_call",
                "ok",
                latency,
                {"tool": "get_historical_ohlcv", "symbol": normalized, "bars": len(bars)},
            )
            return bars
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_error",
                "error",
                latency,
                {"tool": "get_historical_ohlcv", "symbol": normalized, "error": str(exc)},
            )
            if isinstance(exc, NSEMCPError):
                raise
            raise NSEMCPError(str(exc)) from exc

    async def get_market_news(self, symbol: str) -> list[MarketNewsItem]:
        """Fetch public news items for an active NSE symbol."""
        normalized = symbol.strip().upper()
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("get_market_news", normalized)
            if normalized in self._news_overrides:
                items = self._news_overrides[normalized]
            elif self.mode == "stdio":
                data = await self._call_stdio_tool("get_market_news", {"symbol": normalized})
                items = [MarketNewsItem.model_validate(row) for row in data]
            else:
                if normalized not in ACTIVE_NSE_SYMBOLS:
                    raise NSEMCPError(f"News feed unavailable for '{normalized}'.")
                items = FIXTURE_NEWS.get(normalized, [])
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_call",
                "ok",
                latency,
                {"tool": "get_market_news", "symbol": normalized, "items": len(items)},
            )
            return items
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_error",
                "error",
                latency,
                {"tool": "get_market_news", "symbol": normalized, "error": str(exc)},
            )
            if isinstance(exc, NSEMCPError):
                raise
            raise NSEMCPError(str(exc)) from exc
