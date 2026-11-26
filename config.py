"""Centralized non-secret runtime configuration.

Note: Brokerage credentials (KITE_API_KEY, KITE_ACCESS_TOKEN) are intentionally
excluded from this module and loaded exclusively inside execution/zerodha_executor.py.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Application settings loaded from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: SecretStr | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")

    dry_run: bool = Field(default=True, alias="DRY_RUN")
    use_mock_llm: bool = Field(default=True, alias="USE_MOCK_LLM")
    nse_mcp_mode: Literal["fixture", "stdio", "nse_live"] = Field(
        default="fixture", alias="NSE_MCP_MODE"
    )
    nse_cm_market_mcp_url: str = Field(
        default="https://mcp.nseindia.in/cmmkt/mcp",
        alias="NSE_CM_MARKET_MCP_URL",
    )
    nse_bhavcopy_mcp_url: str = Field(
        default="https://mcp.nseindia.in/bhavcopy/cm/mcp",
        alias="NSE_BHAVCOPY_MCP_URL",
    )

    # Deterministic risk & guardrail caps
    allowed_exchange: Literal["NSE"] = "NSE"
    max_concurrent_trades: int = 1
    max_position_value_inr: float = 200_000.0
    max_order_quantity: int = 200
    max_price_deviation_pct: float = 5.0
    max_stop_loss_pct: float = 10.0

    # Observability
    audit_log_path: str = "logs/audit.jsonl"
    eval_log_path: str = "logs/eval_audit.jsonl"


def get_settings() -> AppSettings:
    """Return a fresh AppSettings instance."""
    return AppSettings()
