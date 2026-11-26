"""Read-only NSE MCP stdio server built with the official mcp SDK.

Security invariant: This server only exposes read-only market data tools and
explicitly strips any Zerodha/Kite credentials from its own runtime environment.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_server.fixtures import (
    ACTIVE_NSE_SYMBOLS,
    FIXTURE_NEWS,
    FIXTURE_OHLCV,
    FIXTURE_QUOTES,
)

# Defense-in-depth: ensure brokerage env vars cannot be read inside the MCP server process.
for _forbidden_var in ("KITE_API_KEY", "KITE_ACCESS_TOKEN"):
    os.environ.pop(_forbidden_var, None)

mcp = MCPServer("nse-readonly")


@mcp.tool()
def list_active_nse_symbols() -> dict[str, str]:
    """Return the canonical read-only map of active NSE symbols to company names."""
    return dict(ACTIVE_NSE_SYMBOLS)


@mcp.tool()
def get_nse_quote(symbol: str) -> dict[str, Any]:
    """Return the latest read-only NSE quote snapshot for an active NSE symbol."""
    normalized = symbol.strip().upper()
    if normalized not in FIXTURE_QUOTES:
        raise ValueError(f"Symbol '{normalized}' is not available in active NSE feed.")
    return FIXTURE_QUOTES[normalized].model_dump()


@mcp.tool()
def get_historical_ohlcv(symbol: str, days: int = 30) -> list[dict[str, Any]]:
    """Return historical daily OHLCV bars for an active NSE symbol."""
    normalized = symbol.strip().upper()
    if normalized not in FIXTURE_OHLCV:
        raise ValueError(f"Historical OHLCV unavailable for '{normalized}'.")
    clamped = max(1, min(days, len(FIXTURE_OHLCV[normalized])))
    return [bar.model_dump() for bar in FIXTURE_OHLCV[normalized][-clamped:]]


@mcp.tool()
def get_market_news(symbol: str) -> list[dict[str, Any]]:
    """Return recent public news items for an active NSE symbol."""
    normalized = symbol.strip().upper()
    if normalized not in ACTIVE_NSE_SYMBOLS:
        raise ValueError(f"News feed unavailable for non-NSE symbol '{normalized}'.")
    items = FIXTURE_NEWS.get(normalized, [])
    return [item.model_dump() for item in items]


if __name__ == "__main__":
    mcp.run(transport="stdio")
