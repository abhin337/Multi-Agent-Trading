"""CLI entrypoint for running the NSE multi-agent trading pipeline."""

from __future__ import annotations

import argparse
import json

from config import get_settings
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
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    if args.live_nse:
        settings.nse_mcp_mode = "nse_live"
    elif args.stdio_mcp:
        settings.nse_mcp_mode = "stdio"

    orchestrator = TradingOrchestrator(settings=settings)
    input_fn = (lambda _prompt: "Approve") if args.auto_approve else input
    outcome = orchestrator.run(raw_candidates=args.symbols, hitl_input_fn=input_fn)
    print("\n--- Orchestration Outcome ---")
    print(json.dumps(outcome.model_dump(), indent=2))
    print(f"\nAudit log written to: {orchestrator.audit.log_path} (trace_id={outcome.trace_id})")


if __name__ == "__main__":
    main()
