"""Tests for read-only NSE MCP server tools, fixture replay, and fault handling."""

from __future__ import annotations

import pytest

from mcp_client.nse_client import NSEClient, NSEMCPError
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
