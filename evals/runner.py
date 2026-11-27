"""Eval suite runner executing TC01-TC20 (mock or live Gemini benchmarking)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from evals.cases import ALL_EVAL_CASES, eval_runtime
from schemas.models import EvaluationRecord


def run_all_evals(
    output_path: Path = Path("logs/eval_audit.jsonl"),
    use_mock_llm: bool = True,
    model: str | None = None,
) -> list[EvaluationRecord]:
    """Run all 20 evaluation cases and write one JSON line per case."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    case_log_dir = output_path.parent / "eval_traces"
    case_log_dir.mkdir(parents=True, exist_ok=True)

    records: list[EvaluationRecord] = []
    with eval_runtime(use_mock_llm=use_mock_llm, model=model):
        with output_path.open("w", encoding="utf-8") as fh:
            for spec in ALL_EVAL_CASES:
                record = spec.fn(case_log_dir)
                records.append(record)
                fh.write(json.dumps(record.model_dump()) + "\n")
    return records


def print_scorecard(
    records: list[EvaluationRecord],
    label: str = "MockStructuredClient",
    elapsed_s: float | None = None,
) -> None:
    """Print a concise tabular summary of all 20 eval metrics."""
    total = len(records)
    passed = sum(1 for r in records if r.passed)
    schema_ok = sum(1 for r in records if r.schema_valid)
    hallucinated = sum(1 for r in records if r.hallucinated_ticker)
    hitl_bypassed = sum(1 for r in records if r.hitl_bypassed)
    injection_cases = [r for r in records if r.category in {"Prompt Injection", "Handoff Drift"}]
    injection_blocked = sum(1 for r in injection_cases if r.injection_blocked)

    timing = f" ({elapsed_s:.2f}s)" if elapsed_s is not None else ""
    print(f"\n=== 20-Case Security & Reliability Scorecard [{label}]{timing} ===")
    header = (
        f"{'ID':<6} {'Category':<18} {'Pass':<6} "
        f"{'Schema':<7} {'Halluc':<7} {'InjBlk':<7} {'HITLByp':<7}"
    )
    print(header)
    print("-" * 64)
    for r in records:
        print(
            f"{r.test_id:<6} {r.category:<18} {str(r.passed):<6} "
            f"{str(r.schema_valid):<7} {str(r.hallucinated_ticker):<7} "
            f"{str(r.injection_blocked):<7} {str(r.hitl_bypassed):<7}"
        )
    print("-" * 64)
    print(
        f"Total Passed: {passed}/{total} | Schema Valid: {schema_ok}/{total} | "
        f"Hallucinated Tickers: {hallucinated}/{total} | "
        f"Injections Blocked: {injection_blocked}/{len(injection_cases)} | "
        f"HITL Bypassed: {hitl_bypassed}/{total}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the 20-case security & reliability evaluation suite."
    )
    parser.add_argument(
        "--live-llm",
        action="store_true",
        help="Run TC01-TC20 against live Gemini API instead of deterministic mock LLM.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override Gemini model name when --live-llm is set (e.g. gemini-3.8-flash).",
    )
    parser.add_argument(
        "--compare-models",
        nargs="+",
        default=None,
        help="Benchmark multiple Gemini models sequentially with --live-llm.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.compare_models:
        all_passed = True
        summaries: list[tuple[str, int, int, float]] = []
        for model_name in args.compare_models:
            t0 = time.perf_counter()
            out_path = Path(f"logs/eval_audit_{model_name.replace('/', '_')}.jsonl")
            records = run_all_evals(output_path=out_path, use_mock_llm=False, model=model_name)
            elapsed = time.perf_counter() - t0
            print_scorecard(records, label=f"Live:{model_name}", elapsed_s=elapsed)
            passed_cnt = sum(1 for r in records if r.passed)
            summaries.append((model_name, passed_cnt, len(records), elapsed))
            if passed_cnt != len(records):
                all_passed = False
        print("\n=== Model Benchmark Comparison ===")
        for m, p, tot, secs in summaries:
            print(f"{m:<24} passed={p}/{tot} latency={secs:.2f}s")
        return 0 if all_passed else 1

    t0 = time.perf_counter()
    use_mock = not args.live_llm
    records = run_all_evals(use_mock_llm=use_mock, model=args.model)
    elapsed = time.perf_counter() - t0
    label = f"Live:{args.model or 'default'}" if args.live_llm else "MockStructuredClient"
    print_scorecard(records, label=label, elapsed_s=elapsed)
    return 0 if all(r.passed for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
