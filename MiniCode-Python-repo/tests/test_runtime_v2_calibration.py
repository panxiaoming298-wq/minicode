from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _module():
    path = Path(__file__).parents[1] / "benchmarks" / "runtime_v2_calibration.py"
    spec = importlib.util.spec_from_file_location("runtime_v2_calibration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_calibration_gate_accepts_threshold_compliant_measurements() -> None:
    module = _module()
    evaluation = module.evaluate_calibration(
        legacy_standard={
            "success_rate": 1.0,
            "p95_ms": 1000.0,
            "mean_tokens": 100.0,
            "mean_model_calls": 1.0,
        },
        unified_standard={
            "success_rate": 0.99,
            "p95_ms": 1080.0,
            "mean_tokens": 105.0,
            "mean_model_calls": 1.0,
        },
        serial_workload={
            "success_rate": 1.0,
            "p95_ms": 1000.0,
        },
        ultra_fanout={
            "success_rate": 1.0,
            "p95_ms": 700.0,
        },
    )

    assert evaluation["passed"] is True
    assert evaluation["standard"]["passed"] is True
    assert evaluation["ultra"]["wall_clock_improvement_pct"] == 30.0


def test_calibration_gate_rejects_regression_and_unproven_ultra_gain() -> None:
    module = _module()
    evaluation = module.evaluate_calibration(
        legacy_standard={
            "success_rate": 1.0,
            "p95_ms": 1000.0,
            "mean_tokens": 100.0,
            "mean_model_calls": 1.0,
        },
        unified_standard={
            "success_rate": 0.95,
            "p95_ms": 1200.0,
            "mean_tokens": 120.0,
            "mean_model_calls": 2.0,
        },
        serial_workload={
            "success_rate": 1.0,
            "p95_ms": 1000.0,
        },
        ultra_fanout={
            "success_rate": 1.0,
            "p95_ms": 900.0,
        },
    )

    assert evaluation["passed"] is False
    assert evaluation["standard"]["passed"] is False
    assert evaluation["ultra"]["passed"] is False


def test_percentile_uses_nearest_rank() -> None:
    module = _module()
    assert module.percentile(range(1, 21), 0.95) == 19.0
