"""Runs the deterministic evaluation suite (evaluation/run_eval.py) and enforces its thresholds."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[2] / "evaluation" / "run_eval.py"


def test_evaluation_thresholds(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("run_eval", EVAL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_eval"] = module
    spec.loader.exec_module(module)
    summary, results = module.run(tmp_path)
    assert summary["scenarios"] >= 50
    failed = [f"{r['id']}: {[k for k, v in r['checks'].items() if not v]}" for r in results if not r["passed"]]
    assert module.threshold_failures(summary) == [], failed
    assert (tmp_path / "latest.md").exists()
