"""Tests for NSE MCP server tools, fixture replay, live NSE parsers, and fault handling."""

from __future__ import annotations

import pytest

from guardrails.allowlist import validate_ticker
from mcp_client.nse_client import (
    NSEClient,
    NSEMCPError,
    parse_bhavcopy_history,
    parse_cm_stock_quote,
    parse_corporate_actions_as_news,
)
from mcp_server.nse_server import (
    get_historical_ohlcv,
    get_market_news,
    get_nse_quote,
    list_active_nse_symbols,
)
from mcp_server.nse_server import mcp as nse_mcp_server


def test_mcp_fixture_and_server_tool_parity() -> None:
    fixture_client = NSEClient(mode="fixture")
    q_fix = fixture_client.get_quote("INFY")
    q_srv = get_nse_quote("INFY")
    assert q_fix.symbol == q_srv["symbol"] == "INFY"
    assert q_fix.ltp == q_srv["ltp"]

    bars_fix = fixture_client.get_historical_ohlcv("INFY", days=20)
    bars_srv = get_historical_ohlcv("INFY", days=20)
    assert len(bars_fix) == len(bars_srv) == 20

    news_fix = fixture_client.get_market_news("INFY")
    news_srv = get_market_news("INFY")
    assert len(news_fix) == len(news_srv)
    assert "INFY" in list_active_nse_symbols()


def test_mcp_timeout_or_503_aborts_cleanly() -> None:
    client = NSEClient(mode="fixture", simulate_error="503 Service Unavailable")
    with pytest.raises(NSEMCPError, match="503"):
        client.get_quote("INFY")


def test_mcp_server_exposes_only_readonly_tools() -> None:
    tool_names = set(nse_mcp_server._tool_manager._tools.keys())
    assert tool_names == {
        "list_active_nse_symbols",
        "get_nse_quote",
        "get_historical_ohlcv",
        "get_market_news",
    }
    assert not any("order" in name or "zerodha" in name or "kite" in name for name in tool_names)


def test_nse_live_parsers_and_client_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    quote = parse_cm_stock_quote(
        {
            "updatedAt": "2026-11-26T10:22:26Z",
            "stock": {
                "symbol": "INFY",
                "openPrice": 1151.0,
                "highPrice": 1185.0,
                "lowPrice": 1148.3,
                "lastTradedPrice": 1183.0,
                "perChange": 4.69,
                "volume": 16676550,
                "latestTimestamp": "2026-11-26 15:52:26",
            },
        }
    )
    assert quote.symbol == "INFY" and quote.ltp == 1183.0 and quote.change_pct == 4.69

    bars = parse_bhavcopy_history(
        {
            "data": [
                {
                    "date": "2026-11-25",
                    "open": 1120.0,
                    "high": 1135.0,
                    "low": 1118.0,
                    "close": 1130.0,
                    "volume": 1200000,
                },
                {
                    "date": "2026-11-24",
                    "open": 1105.0,
                    "high": 1125.0,
                    "low": 1100.0,
                    "close": 1120.0,
                    "volume": 1100000,
                },
            ]
        },
        days=2,
    )
    assert [b.date for b in bars] == ["2026-11-24", "2026-11-25"]

    actions = parse_corporate_actions_as_news(
        "INFY",
        {"actions": [{"subject": "Interim Dividend", "exDate": "2026-10-29", "remarks": "EQ"}]},
    )
    assert len(actions) == 1 and "Interim Dividend" in actions[0].headline

    client = NSEClient(mode="nse_live")

    def fake_http(url: str, name: str, args: dict) -> dict:
        if name == "cm_get_equity_stocks":
            return {"stocks": [{"symbol": "KOTAKBANK", "series": "EQ"}]}
        if name == "nse_lookup_symbol":
            return {"symbols": ["WIPRO"]}
        if name == "cm_get_stock_quote":
            return {
                "stock": {
                    "symbol": args["symbol"],
                    "openPrice": 500.0,
                    "highPrice": 512.0,
                    "lowPrice": 498.0,
                    "lastTradedPrice": 510.0,
                    "perChange": 2.0,
                    "volume": 900000,
                    "latestTimestamp": "2026-11-26 15:52:26",
                }
            }
        raise AssertionError(f"Unexpected tool {name}")

    monkeypatch.setattr(client, "_call_http_tool", fake_http)
    active = client.list_active_symbols()
    assert "KOTAKBANK" in active
    assert client.lookup_live_symbol("WIPRO") == ["WIPRO"]
    assert client.get_quote("KOTAKBANK").ltp == 510.0
    assert validate_ticker("KOTAKBANK", allowed_symbols=active.keys()).is_valid
    assert not validate_ticker("HDFC", allowed_symbols=active.keys()).is_valid
    assert not validate_ticker("NVDA", allowed_symbols=active.keys()).is_valid
