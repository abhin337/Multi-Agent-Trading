"""Read-only NSE MCP client wrapper.

Supports:
- 'fixture' mode: deterministic synchronous local replay for offline tests and demos.
- 'stdio' mode: connects over MCP stdio transport to local mcp_server/nse_server.py.
- 'nse_live' mode: connects over Streamable HTTP to official NSE India MCP servers:
  * cm-market (https://mcp.nseindia.in/cmmkt/mcp) for near real-time quotes & equity lists.
  * nse-bhavcopy (https://mcp.nseindia.in/bhavcopy/cm/mcp) for 5-year EOD OHLCV & lookup.

Zero Zerodha credentials or write-enabled brokerage methods exist in this module.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

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


def parse_cm_stock_quote(raw_payload: dict[str, Any]) -> NSEQuote:
    """Parse NSE cm-market `cm_get_stock_quote` payload into an NSEQuote."""
    if "error" in raw_payload:
        raise NSEMCPError(f"cm-market quote error: {raw_payload['error']}")
    stock = raw_payload.get("stock") or raw_payload
    symbol = str(stock["symbol"]).strip().upper()
    ltp = float(stock.get("lastTradedPrice") or stock.get("ltp") or 0.0)
    open_p = float(stock.get("openPrice") or ltp)
    high_p = float(stock.get("highPrice") or ltp)
    low_p = float(stock.get("lowPrice") or ltp)
    volume = int(stock.get("volume") or 0)
    per_change = float(stock.get("perChange") or 0.0)
    ts = str(stock.get("latestTimestamp") or raw_payload.get("updatedAt") or "")
    if ltp <= 0:
        raise NSEMCPError(f"Invalid LTP ({ltp}) returned by cm-market for {symbol}.")
    return NSEQuote(
        symbol=symbol,
        exchange="NSE",
        ltp=ltp,
        open_price=open_p if open_p > 0 else ltp,
        high_price=high_p if high_p > 0 else ltp,
        low_price=low_p if low_p > 0 else ltp,
        volume=max(0, volume),
        change_pct=per_change,
        timestamp=ts,
    )


def parse_bhavcopy_history(raw_payload: dict[str, Any], days: int) -> list[OHLCVBar]:
    """Parse NSE nse-bhavcopy `get_stock_history` payload into chronological OHLCVBar list."""
    if "error" in raw_payload:
        raise NSEMCPError(f"nse-bhavcopy history error: {raw_payload['error']}")
    rows = raw_payload.get("data") or []
    bars: list[OHLCVBar] = []
    for row in rows:
        close_p = float(row.get("close") or row.get("ltp") or 0.0)
        if close_p <= 0:
            continue
        open_p = float(row.get("open") or close_p)
        high_p = float(row.get("high") or close_p)
        low_p = float(row.get("low") or close_p)
        bars.append(
            OHLCVBar(
                date=str(row["date"]),
                open=open_p if open_p > 0 else close_p,
                high=high_p if high_p > 0 else close_p,
                low=low_p if low_p > 0 else close_p,
                close=close_p,
                volume=max(0, int(row.get("volume") or 0)),
            )
        )
    bars.sort(key=lambda b: b.date)
    if not bars:
        raise NSEMCPError("nse-bhavcopy returned empty OHLCV history.")
    return bars[-days:]


def parse_corporate_actions_as_news(
    symbol: str, raw_payload: dict[str, Any]
) -> list[MarketNewsItem]:
    """Convert official NSE corporate actions into read-only MarketNewsItem records."""
    actions = (
        raw_payload.get("actions")
        or raw_payload.get("corporateActions")
        or raw_payload.get("data")
        or []
    )
    items: list[MarketNewsItem] = []
    for entry in actions[:5]:
        subject = str(entry.get("subject") or entry.get("purpose") or "Corporate Action").strip()
        ex_date = str(entry.get("exDate") or entry.get("ex_date") or "").strip()
        remarks = str(entry.get("remarks") or entry.get("series") or "").strip()
        items.append(
            MarketNewsItem(
                source="NSE Corporate Actions (Bhavcopy MCP)",
                headline=f"{symbol}: {subject}",
                body=f"Ex-Date: {ex_date}. {remarks}".strip(),
                published_at=ex_date or date.today().isoformat(),
            )
        )
    return items


class NSEClient:
    """Strictly read-only wrapper around NSE MCP data tools."""

    DEFAULT_CM_MARKET_URL = "https://mcp.nseindia.in/cmmkt/mcp"
    DEFAULT_BHAVCOPY_URL = "https://mcp.nseindia.in/bhavcopy/cm/mcp"

    def __init__(
        self,
        mode: Literal["fixture", "stdio", "nse_live"] = "fixture",
        cm_market_url: str = DEFAULT_CM_MARKET_URL,
        bhavcopy_url: str = DEFAULT_BHAVCOPY_URL,
        news_overrides: dict[str, list[MarketNewsItem]] | None = None,
        quote_overrides: dict[str, NSEQuote] | None = None,
        ohlcv_overrides: dict[str, list[OHLCVBar]] | None = None,
        simulate_error: str | None = None,
        event_hook: EventHook | None = None,
    ) -> None:
        self.mode = mode
        self.cm_market_url = cm_market_url
        self.bhavcopy_url = bhavcopy_url
        self._news_overrides = news_overrides or {}
        self._quote_overrides = quote_overrides or {}
        self._ohlcv_overrides = ohlcv_overrides or {}
        self._simulate_error = simulate_error
        self._event_hook = event_hook
        self._project_root = Path(__file__).resolve().parent.parent
        self._live_symbol_cache: dict[str, str] | None = None

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

    @staticmethod
    def _decode_tool_result(name: str, result: Any) -> Any:
        if getattr(result, "is_error", False):
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

    async def _call_stdio_tool_async(self, name: str, arguments: dict[str, Any]) -> Any:
        server_params = self._stdio_server_params()
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                return self._decode_tool_result(name, result)

    async def _call_http_tool_async(
        self, url: str, name: str, arguments: dict[str, Any]
    ) -> Any:
        async with streamable_http_client(url, terminate_on_close=False) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                return self._decode_tool_result(name, result)

    def _call_stdio_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return asyncio.run(self._call_stdio_tool_async(name, arguments))

    def _call_http_tool(self, url: str, name: str, arguments: dict[str, Any]) -> Any:
        return asyncio.run(self._call_http_tool_async(url, name, arguments))

    def lookup_live_symbol(self, query: str) -> list[str]:
        """Search NSE bhavcopy symbol catalog across 3,500+ listed equities."""
        normalized = query.strip().upper()
        if self.mode != "nse_live":
            return [s for s in ACTIVE_NSE_SYMBOLS if normalized in s]
        data = self._call_http_tool(
            self.bhavcopy_url, "nse_lookup_symbol", {"query": normalized}
        )
        return [str(s).upper() for s in (data or {}).get("symbols", [])]

    def list_active_symbols(self) -> dict[str, str]:
        """List active NSE symbols via MCP."""
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("list_active_nse_symbols")
            if self.mode == "stdio":
                data = self._call_stdio_tool("list_active_nse_symbols", {})
                result = dict(data)
            elif self.mode == "nse_live":
                if self._live_symbol_cache is None:
                    data = self._call_http_tool(
                        self.cm_market_url,
                        "cm_get_equity_stocks",
                        {"limit": 500, "symbolFilter": ""},
                    )
                    stocks = (data or {}).get("stocks", [])
                    self._live_symbol_cache = {
                        str(row["symbol"]).upper(): f"NSE Equity ({row.get('series', 'EQ')})"
                        for row in stocks
                        if row.get("symbol")
                    }
                result = dict(self._live_symbol_cache)
            else:
                result = dict(ACTIVE_NSE_SYMBOLS)
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_call",
                "ok",
                latency,
                {"tool": "list_active_nse_symbols", "mode": self.mode, "count": len(result)},
            )
            return result
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_error",
                "error",
                latency,
                {"tool": "list_active_nse_symbols", "error": str(exc)},
            )
            if isinstance(exc, NSEMCPError):
                raise
            raise NSEMCPError(str(exc)) from exc

    def get_quote(self, symbol: str) -> NSEQuote:
        """Fetch read-only live/near-real-time quote snapshot for an active NSE symbol."""
        normalized = symbol.strip().upper()
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("get_nse_quote", normalized)
            if normalized in self._quote_overrides:
                quote = self._quote_overrides[normalized]
            elif self.mode == "stdio":
                data = self._call_stdio_tool("get_nse_quote", {"symbol": normalized})
                quote = NSEQuote.model_validate(data)
            elif self.mode == "nse_live":
                data = self._call_http_tool(
                    self.cm_market_url, "cm_get_stock_quote", {"symbol": normalized}
                )
                quote = parse_cm_stock_quote(data)
            else:
                if normalized not in FIXTURE_QUOTES:
                    raise NSEMCPError(f"Symbol '{normalized}' not found in NSE fixture feed.")
                quote = FIXTURE_QUOTES[normalized]
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_call",
                "ok",
                latency,
                {
                    "tool": "cm_get_stock_quote" if self.mode == "nse_live" else "get_nse_quote",
                    "symbol": normalized,
                    "ltp": quote.ltp,
                },
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

    def get_historical_ohlcv(self, symbol: str, days: int = 30) -> list[OHLCVBar]:
        """Fetch historical daily OHLCV bars for an active NSE symbol."""
        normalized = symbol.strip().upper()
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("get_historical_ohlcv", normalized)
            if normalized in self._ohlcv_overrides:
                bars = self._ohlcv_overrides[normalized][-days:]
            elif self.mode == "stdio":
                data = self._call_stdio_tool(
                    "get_historical_ohlcv", {"symbol": normalized, "days": days}
                )
                bars = [OHLCVBar.model_validate(row) for row in data]
            elif self.mode == "nse_live":
                months = max(1, min(3, (days // 18) + 1))
                data = self._call_http_tool(
                    self.bhavcopy_url,
                    "get_stock_history",
                    {"symbol": normalized, "months": months, "endDate": "today"},
                )
                bars = parse_bhavcopy_history(data, days=days)
            else:
                if normalized not in FIXTURE_OHLCV:
                    raise NSEMCPError(f"OHLCV unavailable for '{normalized}'.")
                bars = FIXTURE_OHLCV[normalized][-days:]
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_call",
                "ok",
                latency,
                {
                    "tool": (
                        "get_stock_history"
                        if self.mode == "nse_live"
                        else "get_historical_ohlcv"
                    ),
                    "symbol": normalized,
                    "bars": len(bars),
                },
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

    def get_market_news(self, symbol: str) -> list[MarketNewsItem]:
        """Fetch public news or corporate actions for an active NSE symbol."""
        normalized = symbol.strip().upper()
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("get_market_news", normalized)
            if normalized in self._news_overrides:
                items = self._news_overrides[normalized]
            elif self.mode == "stdio":
                data = self._call_stdio_tool("get_market_news", {"symbol": normalized})
                items = [MarketNewsItem.model_validate(row) for row in data]
            elif self.mode == "nse_live":
                today_str = date.today().isoformat()
                from_str = (date.today() - timedelta(days=90)).isoformat()
                data = self._call_http_tool(
                    self.bhavcopy_url,
                    "get_corporate_actions",
                    {"symbol": normalized, "fromDate": from_str, "toDate": today_str},
                )
                items = parse_corporate_actions_as_news(normalized, data or {})
            else:
                if normalized not in ACTIVE_NSE_SYMBOLS:
                    raise NSEMCPError(f"News feed unavailable for '{normalized}'.")
                items = FIXTURE_NEWS.get(normalized, [])
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_call",
                "ok",
                latency,
                {
                    "tool": (
                        "get_corporate_actions"
                        if self.mode == "nse_live"
                        else "get_market_news"
                    ),
                    "symbol": normalized,
                    "items": len(items),
                },
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
