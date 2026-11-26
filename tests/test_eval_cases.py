"""Pytest integration wrapping all 20 security and functional eval cases (TC01-TC20)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.cases import ALL_EVAL_CASES, EvalCaseSpec
from evals.runner import run_all_evals


@pytest.mark.parametrize("spec", ALL_EVAL_CASES, ids=[c.test_id for c in ALL_EVAL_CASES])
def test_eval_case(spec: EvalCaseSpec, tmp_path: Path) -> None:
    record = spec.fn(tmp_path)
    assert record.passed, f"{spec.test_id} failed: {record.details}"
    assert record.schema_valid
    assert not record.hallucinated_ticker
    assert not record.hitl_bypassed


def test_eval_runner_writes_jsonl_scorecard(tmp_path: Path) -> None:
    out_file = tmp_path / "eval_audit.jsonl"
    records = run_all_evals(output_path=out_file)
    assert len(records) == 20
    assert all(r.passed for r in records)
    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 20
