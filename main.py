"""CLI entrypoint for running the NSE multi-agent trading pipeline or Kite login."""

from __future__ import annotations

import argparse
import json

from config import get_settings
from execution.kite_auth import run_kite_login_cli
from orchestrator import TradingOrchestrator


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the 3-agent NSE trading pipeline with guardrails & HITL gate."
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["INFY", "RELIANCE", "ICICIBANK"],
        help="Candidate NSE symbols to scan.",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Simulate synchronous HITL 'Approve' in non-interactive demos.",
    )
    parser.add_argument(
        "--stdio-mcp",
        action="store_true",
        help="Connect to local mcp_server/nse_server.py over MCP stdio transport.",
    )
    parser.add_argument(
        "--live-nse",
        action="store_true",
        help="Connect to live NSE India MCP servers (cm-market + nse-bhavcopy).",
    )
    parser.add_argument(
        "--live-news",
        action="store_true",
        help="Fetch untrusted public RSS news headlines in nse_live mode (sanitized pre-LLM).",
    )
    parser.add_argument(
        "--kite-login",
        action="store_true",
        help="Run interactive daily Zerodha Kite login to store KITE_ACCESS_TOKEN safely.",
    )
    parser.add_argument(
        "--kite-store",
        choices=["keyring", "dotenv"],
        default="keyring",
        help="Where --kite-login stores KITE_API_KEY and KITE_ACCESS_TOKEN.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.kite_login:
        run_kite_login_cli(target=args.kite_store)
        return

    settings = get_settings()
    if args.live_nse:
        settings.nse_mcp_mode = "nse_live"
    elif args.stdio_mcp:
        settings.nse_mcp_mode = "stdio"
    if args.live_news:
        settings.enable_live_news_rss = True

    orchestrator = TradingOrchestrator(settings=settings)
    input_fn = (lambda _prompt: "Approve") if args.auto_approve else input
    outcome = orchestrator.run(raw_candidates=args.symbols, hitl_input_fn=input_fn)
    print("\n--- Orchestration Outcome ---")
    print(json.dumps(outcome.model_dump(), indent=2))
    print(f"\nAudit log written to: {orchestrator.audit.log_path} (trace_id={outcome.trace_id})")


if __name__ == "__main__":
    main()
