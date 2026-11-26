"""Eval suite runner executing TC01-TC20 and emitting structured JSONL audit metrics."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evals.cases import ALL_EVAL_CASES
from schemas.models import EvaluationRecord


def run_all_evals(
    output_path: Path = Path("logs/eval_audit.jsonl"),
) -> list[EvaluationRecord]:
    """Run all 20 evaluation cases and write one JSON line per case."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    case_log_dir = output_path.parent / "eval_traces"
    case_log_dir.mkdir(parents=True, exist_ok=True)

    records: list[EvaluationRecord] = []
    with output_path.open("w", encoding="utf-8") as fh:
        for spec in ALL_EVAL_CASES:
            record = spec.fn(case_log_dir)
            records.append(record)
            fh.write(json.dumps(record.model_dump()) + "\n")
    return records


def print_scorecard(records: list[EvaluationRecord]) -> None:
    """Print a concise tabular summary of all 20 eval metrics."""
    total = len(records)
    passed = sum(1 for r in records if r.passed)
    schema_ok = sum(1 for r in records if r.schema_valid)
    hallucinated = sum(1 for r in records if r.hallucinated_ticker)
    hitl_bypassed = sum(1 for r in records if r.hitl_bypassed)
    injection_cases = [r for r in records if r.category in {"Prompt Injection", "Handoff Drift"}]
    injection_blocked = sum(1 for r in injection_cases if r.injection_blocked)

    print("\n=== 20-Case Multi-Agent Security & Reliability Scorecard ===")
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


def main() -> int:
    records = run_all_evals()
    print_scorecard(records)
    return 0 if all(r.passed for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
