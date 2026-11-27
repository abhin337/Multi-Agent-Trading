"""Read-only NSE MCP client wrapper.

Supports:
- 'fixture' mode: deterministic synchronous local replay for offline tests and demos.
- 'stdio' mode: connects over MCP stdio transport to local mcp_server/nse_server.py.
- 'nse_live' mode: connects over Streamable HTTP to official NSE India MCP servers:
  * cm-market (https://mcp.nseindia.in/cmmkt/mcp) for near real-time quotes & equity lists.
  * nse-bhavcopy (https://mcp.nseindia.in/bhavcopy/cm/mcp) for 5-year EOD OHLCV,
    corporate actions, market mood (India VIX + breadth), and symbol lookup.

Zero Zerodha credentials or write-enabled brokerage methods exist in this module.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote_plus

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from mcp_server.fixtures import (
    ACTIVE_NSE_SYMBOLS,
    FIXTURE_NEWS,
    FIXTURE_OHLCV,
    FIXTURE_QUOTES,
)
from schemas.models import (
    CorporateActionEvent,
    MarketMood,
    MarketNewsItem,
    NSEQuote,
    OHLCVBar,
)


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


def parse_corporate_actions(
    symbol: str, raw_payload: dict[str, Any]
) -> list[CorporateActionEvent]:
    """Parse official NSE `get_corporate_actions` payload into typed events."""
    actions = (
        raw_payload.get("actions")
        or raw_payload.get("corporateActions")
        or raw_payload.get("data")
        or []
    )
    events: list[CorporateActionEvent] = []
    for entry in actions:
        raw_type = str(entry.get("actionType") or "OTHER").strip().upper()
        if raw_type not in {"SPLIT", "BONUS", "DIVIDEND", "OTHER"}:
            raw_type = "OTHER"
        factor = float(entry.get("adjustmentFactor") or 1.0)
        if factor <= 0:
            factor = 1.0
        events.append(
            CorporateActionEvent(
                symbol=symbol.strip().upper(),
                action_type=raw_type,  # type: ignore[arg-type]
                ex_date=str(entry.get("exDate") or entry.get("ex_date") or "").strip(),
                subject=str(
                    entry.get("purpose") or entry.get("subject") or "Corporate Action"
                ).strip(),
                adjustment_factor=factor,
            )
        )
    return events


def parse_corporate_actions_as_news(
    symbol: str, raw_payload: dict[str, Any]
) -> list[MarketNewsItem]:
    """Convert official NSE corporate actions into read-only MarketNewsItem records."""
    events = parse_corporate_actions(symbol, raw_payload)
    items: list[MarketNewsItem] = []
    for ev in events[:5]:
        items.append(
            MarketNewsItem(
                source="NSE Corporate Actions (Bhavcopy MCP)",
                headline=f"{ev.symbol}: {ev.subject}",
                body=f"Ex-Date: {ev.ex_date}. ActionType: {ev.action_type}.",
                published_at=ev.ex_date or date.today().isoformat(),
            )
        )
    return items


def parse_market_mood(
    raw_payload: dict[str, Any],
    high_vix_threshold: float = 22.0,
    min_ad_ratio: float = 0.65,
) -> MarketMood:
    """Parse NSE `get_market_mood` payload into a deterministic MarketMood regime."""
    if "error" in raw_payload:
        raise NSEMCPError(f"nse-bhavcopy market mood error: {raw_payload['error']}")
    vix_info = raw_payload.get("india_vix") or {}
    breadth = raw_payload.get("stock_breadth") or {}
    india_vix = float(vix_info.get("level") or 15.0)
    advances = int(breadth.get("up") or 0)
    declines = int(breadth.get("down") or 0)
    ad_ratio = round(advances / max(declines, 1), 2) if (advances or declines) else 1.0

    if india_vix >= high_vix_threshold or (declines > 0 and ad_ratio < min_ad_ratio):
        regime: Literal["BULLISH", "NEUTRAL", "HIGH_VIX_RISK_OFF"] = "HIGH_VIX_RISK_OFF"
    elif ad_ratio >= 1.25 and india_vix < 18.0:
        regime = "BULLISH"
    else:
        regime = "NEUTRAL"

    return MarketMood(
        date=str(raw_payload.get("date") or date.today().isoformat()),
        india_vix=india_vix,
        advances=advances,
        declines=declines,
        ad_ratio=ad_ratio,
        regime=regime,
    )


def parse_rss_headlines(xml_text: str, limit: int = 5) -> list[MarketNewsItem]:
    """Parse untrusted RSS XML feed into MarketNewsItem list (must be sanitized downstream)."""
    items: list[MarketNewsItem] = []
    root = ET.fromstring(xml_text)
    for node in root.findall(".//item")[:limit]:
        title = (node.findtext("title") or "").strip()
        desc = (node.findtext("description") or "").strip()
        pub = (node.findtext("pubDate") or date.today().isoformat()).strip()
        source_el = node.find("source")
        src = (source_el.text or "Financial RSS").strip() if source_el is not None else "RSS"
        if title:
            items.append(
                MarketNewsItem(
                    source=src,
                    headline=title,
                    body=desc or title,
                    published_at=pub,
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
        enable_live_news_rss: bool = False,
        news_overrides: dict[str, list[MarketNewsItem]] | None = None,
        quote_overrides: dict[str, NSEQuote] | None = None,
        ohlcv_overrides: dict[str, list[OHLCVBar]] | None = None,
        corporate_action_overrides: dict[str, list[CorporateActionEvent]] | None = None,
        mood_override: MarketMood | None = None,
        simulate_error: str | None = None,
        event_hook: EventHook | None = None,
    ) -> None:
        self.mode = mode
        self.cm_market_url = cm_market_url
        self.bhavcopy_url = bhavcopy_url
        self.enable_live_news_rss = enable_live_news_rss
        self._news_overrides = news_overrides or {}
        self._quote_overrides = quote_overrides or {}
        self._ohlcv_overrides = ohlcv_overrides or {}
        self._corp_overrides = corporate_action_overrides or {}
        self._mood_override = mood_override
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
        timeout = httpx2.Timeout(25.0, connect=10.0)
        async with httpx2.AsyncClient(timeout=timeout) as http_client:
            async with streamable_http_client(
                url, http_client=http_client, terminate_on_close=False
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    return self._decode_tool_result(name, result)

    def _call_stdio_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return asyncio.run(self._call_stdio_tool_async(name, arguments))

    def _call_http_tool(
        self, url: str, name: str, arguments: dict[str, Any], retries: int = 1
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return asyncio.run(self._call_http_tool_async(url, name, arguments))
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def _fetch_live_rss_news(self, symbol: str) -> list[MarketNewsItem]:
        """Fetch untrusted public RSS news for symbol with strict timeout & safe fallback."""
        query = quote_plus(f"{symbol} NSE India stock")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            with httpx2.Client(timeout=5.0, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return parse_rss_headlines(resp.text, limit=4)
        except Exception:
            return []

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
                        {"limit": 100, "symbolFilter": ""},
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

    def get_corporate_actions(self, symbol: str) -> list[CorporateActionEvent]:
        """Fetch split/bonus/dividend events for corporate-action OHLCV adjustment."""
        normalized = symbol.strip().upper()
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("get_corporate_actions", normalized)
            if normalized in self._corp_overrides:
                events = self._corp_overrides[normalized]
            elif self.mode == "nse_live":
                today_str = date.today().isoformat()
                from_str = (date.today() - timedelta(days=365)).isoformat()
                data = self._call_http_tool(
                    self.bhavcopy_url,
                    "get_corporate_actions",
                    {"symbol": normalized, "fromDate": from_str, "toDate": today_str},
                )
                events = parse_corporate_actions(normalized, data or {})
            else:
                events = []
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_call",
                "ok",
                latency,
                {"tool": "get_corporate_actions", "symbol": normalized, "count": len(events)},
            )
            return events
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_error",
                "error",
                latency,
                {"tool": "get_corporate_actions", "symbol": normalized, "error": str(exc)},
            )
            if isinstance(exc, NSEMCPError):
                raise
            raise NSEMCPError(str(exc)) from exc

    def get_market_mood(
        self,
        high_vix_threshold: float = 22.0,
        min_ad_ratio: float = 0.65,
    ) -> MarketMood:
        """Fetch broad NSE India VIX & advance/decline market regime."""
        t0 = time.perf_counter()
        try:
            self._check_fault_injection("get_market_mood")
            if self._mood_override is not None:
                mood = self._mood_override
            elif self.mode == "nse_live":
                data = self._call_http_tool(
                    self.bhavcopy_url, "get_market_mood", {"date": "today"}
                )
                mood = parse_market_mood(
                    data or {},
                    high_vix_threshold=high_vix_threshold,
                    min_ad_ratio=min_ad_ratio,
                )
            else:
                mood = MarketMood(
                    date=date.today().isoformat(),
                    india_vix=14.2,
                    advances=1650,
                    declines=1100,
                    ad_ratio=1.5,
                    regime="BULLISH",
                )
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_call",
                "ok",
                latency,
                {
                    "tool": "get_market_mood",
                    "india_vix": mood.india_vix,
                    "regime": mood.regime,
                },
            )
            return mood
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000.0
            self._emit(
                "mcp_tool_error",
                "error",
                latency,
                {"tool": "get_market_mood", "error": str(exc)},
            )
            if isinstance(exc, NSEMCPError):
                raise
            raise NSEMCPError(str(exc)) from exc

    def get_market_news(self, symbol: str) -> list[MarketNewsItem]:
        """Fetch public news, RSS headlines (if enabled), and corporate filings for symbol."""
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
                if self.enable_live_news_rss:
                    items.extend(self._fetch_live_rss_news(normalized))
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
