"""Repeatable calibration gate for unified Runtime V2.

The benchmark is deliberately provider-independent.  It compares the retained
``runtimeV2=false`` compatibility path with unified Standard using the same
bounded Worker workload, then compares unified Standard serial execution with
Ultra's three-way read fan-out for the same five logical work units.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import minicode.agent_loop
from minicode.tooling import ToolRegistry
from minicode.unified_runtime import run_runtime_turn


SCHEMA_VERSION = "minicode-runtime-v2-calibration-v1"
DEFAULT_RESULTS_JSON = Path("benchmarks/runtime_v2_calibration_results.json")
DEFAULT_RESULTS_MARKDOWN = Path("benchmarks/runtime_v2_calibration_results.md")


@dataclass(frozen=True, slots=True)
class Sample:
    elapsed_ms: float
    succeeded: bool
    model_calls: int
    tokens: int
    selected_route: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "elapsed_ms": round(self.elapsed_ms, 4),
            "succeeded": self.succeeded,
            "model_calls": self.model_calls,
            "tokens": self.tokens,
            "selected_route": self.selected_route,
        }


def percentile(values: Iterable[float], fraction: float) -> float:
    materialized = sorted(float(value) for value in values)
    if not materialized:
        return 0.0
    rank = max(1, math.ceil(len(materialized) * fraction))
    return materialized[min(rank, len(materialized)) - 1]


def summarize(samples: list[Sample]) -> dict[str, Any]:
    elapsed = [sample.elapsed_ms for sample in samples]
    successes = sum(sample.succeeded for sample in samples)
    count = len(samples)
    return {
        "samples": count,
        "success_rate": successes / count if count else 0.0,
        "median_ms": round(statistics.median(elapsed), 4) if elapsed else 0.0,
        "p95_ms": round(percentile(elapsed, 0.95), 4),
        "mean_tokens": round(
            statistics.fmean(sample.tokens for sample in samples),
            4,
        ) if samples else 0.0,
        "mean_model_calls": round(
            statistics.fmean(sample.model_calls for sample in samples),
            4,
        ) if samples else 0.0,
        "routes": sorted({sample.selected_route for sample in samples if sample.selected_route}),
    }


def percent_delta(current: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0 if current == 0 else math.inf
    return (current / baseline - 1.0) * 100.0


def evaluate_calibration(
    *,
    legacy_standard: dict[str, Any],
    unified_standard: dict[str, Any],
    serial_workload: dict[str, Any],
    ultra_fanout: dict[str, Any],
) -> dict[str, Any]:
    standard_success_delta_pp = (
        unified_standard["success_rate"] - legacy_standard["success_rate"]
    ) * 100.0
    standard_p95_delta_pct = percent_delta(
        unified_standard["p95_ms"],
        legacy_standard["p95_ms"],
    )
    standard_token_delta_pct = percent_delta(
        unified_standard["mean_tokens"],
        legacy_standard["mean_tokens"],
    )
    standard_model_call_delta = (
        unified_standard["mean_model_calls"]
        - legacy_standard["mean_model_calls"]
    )
    ultra_success_delta_pp = (
        ultra_fanout["success_rate"] - serial_workload["success_rate"]
    ) * 100.0
    ultra_wall_clock_improvement_pct = -percent_delta(
        ultra_fanout["p95_ms"],
        serial_workload["p95_ms"],
    )
    standard_passed = (
        standard_success_delta_pp >= -2.0
        and standard_p95_delta_pct <= 10.0
        and standard_token_delta_pct <= 10.0
        and standard_model_call_delta <= 0.0
    )
    ultra_passed = (
        ultra_success_delta_pp >= 10.0
        or ultra_wall_clock_improvement_pct >= 25.0
    )
    return {
        "standard": {
            "passed": standard_passed,
            "success_delta_pp": round(standard_success_delta_pp, 4),
            "p95_delta_pct": round(standard_p95_delta_pct, 4),
            "token_delta_pct": round(standard_token_delta_pct, 4),
            "model_call_delta": round(standard_model_call_delta, 4),
            "thresholds": {
                "minimum_success_delta_pp": -2.0,
                "maximum_p95_delta_pct": 10.0,
                "maximum_token_delta_pct": 10.0,
                "maximum_model_call_delta": 0.0,
            },
        },
        "ultra": {
            "passed": ultra_passed,
            "success_delta_pp": round(ultra_success_delta_pp, 4),
            "wall_clock_improvement_pct": round(
                ultra_wall_clock_improvement_pct,
                4,
            ),
            "thresholds": {
                "minimum_success_delta_pp": 10.0,
                "minimum_wall_clock_improvement_pct": 25.0,
                "rule": "either",
            },
        },
        "passed": standard_passed and ultra_passed,
    }


def _run_once(
    *,
    workspace: Path,
    state_root: Path,
    runtime: dict[str, Any],
    fake_agent_loop: Callable[..., list[dict[str, Any]]],
    expected_output: str,
    sequence: int,
) -> Sample:
    calls = 0
    call_lock = threading.Lock()

    def counted_agent_loop(**kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
        return fake_agent_loop(**kwargs)

    original = minicode.agent_loop.run_agent_turn
    minicode.agent_loop.run_agent_turn = counted_agent_loop
    run_id = f"calibration-{sequence}-{time.perf_counter_ns()}"
    runtime = {
        **runtime,
        "runtimeRunId": run_id,
        "runtimeStatePath": str(state_root / f"{run_id}.json"),
    }
    started = time.perf_counter_ns()
    succeeded = False
    selected_route = "legacy-direct"
    tokens = calls
    try:
        messages = run_runtime_turn(
            model=object(),
            tools=ToolRegistry([]),
            messages=[
                {"role": "system", "content": "fixed calibration system prompt"},
                {"role": "user", "content": "fixed calibration golden task"},
            ],
            cwd=str(workspace),
            runtime=runtime,
        )
        succeeded = bool(messages) and messages[-1].get("content") == expected_output
        trace = runtime.get("lastRuntimeTrace")
        if isinstance(trace, dict):
            profile = trace.get("profile", {})
            scheduler = trace.get("scheduler", {})
            if isinstance(profile, dict):
                selected_route = str(profile.get("selected_route") or "")
            if isinstance(scheduler, dict):
                tokens = int(scheduler.get("tokens_used", 0) or 0)
    finally:
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        minicode.agent_loop.run_agent_turn = original
    if not runtime.get("runtimeV2", True):
        tokens = int(runtime.get("_calibrationTokensPerCall", 0)) * calls
    return Sample(elapsed_ms, succeeded, calls, tokens, selected_route)


def _collect(
    run: Callable[[int], Sample],
    *,
    samples: int,
    warmups: int,
) -> list[Sample]:
    for sequence in range(warmups):
        run(-sequence - 1)
    return [run(sequence) for sequence in range(samples)]


def run_calibration(
    *,
    samples: int = 30,
    warmups: int = 3,
    standard_worker_ms: float = 1_000.0,
    ultra_unit_ms: float = 1_200.0,
) -> dict[str, Any]:
    if samples < 5:
        raise ValueError("calibration requires at least five measured samples")
    if warmups < 0:
        raise ValueError("warmups must be non-negative")
    if standard_worker_ms <= 0 or ultra_unit_ms <= 0:
        raise ValueError("calibration workload durations must be positive")

    with tempfile.TemporaryDirectory(prefix="minicode-runtime-calibration-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "fixture.txt").write_text("fixed fixture\n", encoding="utf-8")
        state_root = root / "state"
        state_root.mkdir()
        temporary_workspaces = root / "isolated"

        def standard_fake(**kwargs):
            time.sleep(standard_worker_ms / 1000.0)
            return [
                *kwargs["messages"],
                {"role": "assistant", "content": "standard-calibration-ok"},
            ]

        legacy_runtime = {
            "runtimeV2": False,
            "executionProfile": "standard",
            "_calibrationTokensPerCall": 128,
        }
        unified_runtime = {
            "runtimeV2": True,
            "executionProfile": "standard",
            "_usageExtractor": lambda result, inputs: {
                "tokens": 128,
                "model_calls": 1,
                "cost": 0.0,
            },
        }
        legacy_samples = _collect(
            lambda sequence: _run_once(
                workspace=workspace,
                state_root=state_root,
                runtime=legacy_runtime,
                fake_agent_loop=standard_fake,
                expected_output="standard-calibration-ok",
                sequence=sequence,
            ),
            samples=samples,
            warmups=warmups,
        )
        unified_samples = _collect(
            lambda sequence: _run_once(
                workspace=workspace,
                state_root=state_root,
                runtime=unified_runtime,
                fake_agent_loop=standard_fake,
                expected_output="standard-calibration-ok",
                sequence=sequence,
            ),
            samples=samples,
            warmups=warmups,
        )

        logical_units = 5

        def serial_fake(**kwargs):
            time.sleep(logical_units * ultra_unit_ms / 1000.0)
            return [
                *kwargs["messages"],
                {"role": "assistant", "content": "workload-calibration-ok"},
            ]

        def ultra_fake(**kwargs):
            node_id = kwargs["runtime"]["_runtimeNodeId"]
            time.sleep(ultra_unit_ms / 1000.0)
            content = (
                "integrated workload verified <verification_passed>"
                if node_id == "final-verifier"
                else "workload-calibration-ok"
            )
            return [*kwargs["messages"], {"role": "assistant", "content": content}]

        serial_runtime = {
            "runtimeV2": True,
            "executionProfile": "standard",
            "_usageExtractor": lambda result, inputs: {
                "tokens": 500,
                "model_calls": 1,
                "cost": 0.0,
            },
        }
        ultra_runtime = {
            "runtimeV2": True,
            "executionProfile": "ultra",
            "runtimeMaxActiveWorkers": 3,
            "runtimeMaxAgents": 5,
            "runtimeRetryBudget": 0,
            "runtimeTemporaryRoot": str(temporary_workspaces),
            "ultraSubproblems": [
                "audit independent path alpha",
                "audit independent path beta",
                "audit independent path gamma",
            ],
            "_workerModelFactory": lambda tools: object(),
            "_usageExtractor": lambda result, inputs: {
                "tokens": 100,
                "model_calls": 1,
                "cost": 0.0,
            },
        }
        serial_samples = _collect(
            lambda sequence: _run_once(
                workspace=workspace,
                state_root=state_root,
                runtime=serial_runtime,
                fake_agent_loop=serial_fake,
                expected_output="workload-calibration-ok",
                sequence=10_000 + sequence,
            ),
            samples=samples,
            warmups=warmups,
        )
        ultra_samples = _collect(
            lambda sequence: _run_once(
                workspace=workspace,
                state_root=state_root,
                runtime=ultra_runtime,
                fake_agent_loop=ultra_fake,
                expected_output="workload-calibration-ok",
                sequence=20_000 + sequence,
            ),
            samples=samples,
            warmups=warmups,
        )

    summaries = {
        "legacy_standard": summarize(legacy_samples),
        "unified_standard": summarize(unified_samples),
        "serial_equivalent_workload": summarize(serial_samples),
        "ultra_read_fanout": summarize(ultra_samples),
    }
    evaluation = evaluate_calibration(
        legacy_standard=summaries["legacy_standard"],
        unified_standard=summaries["unified_standard"],
        serial_workload=summaries["serial_equivalent_workload"],
        ultra_fanout=summaries["ultra_read_fanout"],
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "processor_count": os.cpu_count(),
            "samples": samples,
            "warmups": warmups,
            "standard_worker_ms": standard_worker_ms,
            "ultra_unit_ms": ultra_unit_ms,
            "ultra_logical_units": logical_units,
        },
        "method": {
            "standard_baseline": "runtimeV2=false retained compatibility path",
            "standard_candidate": "unified standard-single path",
            "ultra_baseline": "unified Standard executing five equivalent logical units serially",
            "ultra_candidate": "three parallel read units, one exclusive writer unit, one verifier unit",
            "provider": "deterministic bounded Worker; no network",
        },
        "summaries": summaries,
        "evaluation": evaluation,
    }


def render_markdown(result: dict[str, Any]) -> str:
    summaries = result["summaries"]
    evaluation = result["evaluation"]
    lines = [
        "# Runtime V2 Calibration Results",
        "",
        f"- Overall: `{'passed' if evaluation['passed'] else 'failed'}`",
        f"- Samples: {result['environment']['samples']} measured + {result['environment']['warmups']} warmups per route",
        f"- Python: {result['environment']['python']}",
        "",
        "| Route | Success | p95 ms | Mean tokens | Mean model calls |",
        "|---|---:|---:|---:|---:|",
    ]
    for key in (
        "legacy_standard",
        "unified_standard",
        "serial_equivalent_workload",
        "ultra_read_fanout",
    ):
        row = summaries[key]
        lines.append(
            f"| {key} | {row['success_rate'] * 100:.2f}% | {row['p95_ms']:.4f} | "
            f"{row['mean_tokens']:.4f} | {row['mean_model_calls']:.4f} |"
        )
    lines.extend(
        [
            "",
            "## Gate Evaluation",
            "",
            f"- Standard: `{'passed' if evaluation['standard']['passed'] else 'failed'}`; "
            f"success {evaluation['standard']['success_delta_pp']:+.4f} pp, "
            f"p95 {evaluation['standard']['p95_delta_pct']:+.4f}%, "
            f"tokens {evaluation['standard']['token_delta_pct']:+.4f}%, "
            f"model calls {evaluation['standard']['model_call_delta']:+.4f}.",
            f"- Ultra: `{'passed' if evaluation['ultra']['passed'] else 'failed'}`; "
            f"success {evaluation['ultra']['success_delta_pp']:+.4f} pp, "
            f"wall-clock improvement {evaluation['ultra']['wall_clock_improvement_pct']:.4f}%.",
            "",
            "This is a controlled Runtime overhead/concurrency calibration, not a claim about any external model provider.",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--standard-worker-ms", type=float, default=1_000.0)
    parser.add_argument("--ultra-unit-ms", type=float, default=1_200.0)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_RESULTS_JSON)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_RESULTS_MARKDOWN)
    parser.add_argument("--fail-on-regression", action="store_true")
    args = parser.parse_args(argv)
    result = run_calibration(
        samples=args.samples,
        warmups=args.warmups,
        standard_worker_ms=args.standard_worker_ms,
        ultra_unit_ms=args.ultra_unit_ms,
    )
    rendered_json = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(args.json_out, rendered_json)
    _atomic_write(args.markdown_out, render_markdown(result))
    print(rendered_json, end="")
    return 1 if args.fail_on_regression and not result["evaluation"]["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
