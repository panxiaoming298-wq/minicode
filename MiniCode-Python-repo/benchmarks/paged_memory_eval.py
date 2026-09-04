"""Deterministic offline evaluation for tiered context and lossless paging.

This does not claim model-quality results.  It measures real serialized model
input, required-evidence coverage and isolation with the production context
builder.  A future provider-backed suite can consume the same JSON schema.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.models import DependencyType, MemoryMode
from minicode.paged_memory.service import PagedMemoryRuntime


def _scenario(
    mode: MemoryMode, history_items: int, payload_size: int
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="minicode-paged-eval-") as temporary:
        root = Path(temporary)
        config = PagedMemoryConfig(
            enabled=True,
            auto_mode=False,
            default_mode=mode,
            manual_mode=mode,
            sqlite_path=str(root / "state.db"),
            artifact_root=str(root / "artifacts"),
            artifact_large_bytes=max(16_000, payload_size + 1),
            recent_event_token_budget=1_000,
            inline_recent_token_budget=100_000,
            supplemental_token_budget=600,
            output_token_reserve=256,
            tool_token_reserve=128,
            safety_token_reserve=128,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "Follow the task contract."},
            {
                "role": "user",
                "content": (
                    "Build the requested feature. HARD_CONSTRAINT: preserve every "
                    "authoritative source pointer."
                ),
            },
        ]
        for index in range(history_items):
            messages.extend(
                [
                    {
                        "role": "assistant_tool_call",
                        "toolUseId": f"call-{index}",
                        "toolName": "read_file",
                        "input": {"path": f"file-{index}.txt"},
                    },
                    {
                        "role": "tool_result",
                        "toolUseId": f"call-{index}",
                        "toolName": "read_file",
                        "content": f"row={index} " + ("evidence " * payload_size),
                        "isError": False,
                    },
                ]
            )
        runtime = PagedMemoryRuntime(
            config=config,
            cwd=root,
            task_id="benchmark-task",
            goal="Build the requested feature.",
            model_context_limit=128_000,
        )
        runtime.record_initial_messages(messages)
        step_id = runtime.begin_step(1, user_input="Build the requested feature.")
        constraint = runtime.record_constraint(
            text="HARD_CONSTRAINT: preserve every authoritative source pointer.",
            facets=["hard_constraint"],
        )
        runtime.add_dependency(
            step_id=step_id,
            dependency_type=DependencyType.CONSTRAINT,
            dependency_id=constraint.id,
            required=True,
        )
        model_messages, packet = runtime.prepare_model_messages(
            messages,
            required_facets=["hard_constraint"],
            system_context="Follow the task contract.",
        )
        baseline_tokens = sum(
            runtime.token_budget.estimate_message(item) for item in messages
        )
        actual_tokens = sum(
            runtime.token_budget.estimate_message(item) for item in model_messages
        )
        serialized = json.dumps(model_messages, ensure_ascii=False, default=str)
        runtime.close()
        return {
            "mode": mode.value,
            "history_items": history_items,
            "baseline_full_history_tokens": baseline_tokens,
            "actual_model_input_tokens": actual_tokens,
            "token_reduction_ratio": (
                0.0 if baseline_tokens == 0 else 1.0 - actual_tokens / baseline_tokens
            ),
            "required_dependency_coverage_ratio": packet.debug_metadata[
                "required_dependency_coverage_ratio"
            ],
            "required_facet_coverage_ratio": packet.debug_metadata[
                "required_facet_coverage_ratio"
            ],
            "hard_constraint_present": "HARD_CONSTRAINT" in serialized,
            "source_reference_count": len(packet.source_references),
            "token_estimated": packet.debug_metadata["token_estimated"],
        }


def run_evaluation() -> dict[str, Any]:
    scenarios = [
        _scenario(MemoryMode.INLINE, history_items=1, payload_size=4),
        _scenario(MemoryMode.WORKING_SET, history_items=12, payload_size=30),
        _scenario(MemoryMode.PAGED_MEMORY, history_items=60, payload_size=80),
    ]
    gates = {
        "required_dependency_coverage_100_percent": all(
            scenario["required_dependency_coverage_ratio"] == 1.0
            for scenario in scenarios
        ),
        "required_facet_coverage_100_percent": all(
            scenario["required_facet_coverage_ratio"] == 1.0 for scenario in scenarios
        ),
        "hard_constraint_recall_100_percent": all(
            scenario["hard_constraint_present"] for scenario in scenarios
        ),
        "short_inline_unchanged": (
            scenarios[0]["baseline_full_history_tokens"]
            == scenarios[0]["actual_model_input_tokens"]
        ),
        "long_paged_reduces_real_input": scenarios[-1]["token_reduction_ratio"] > 0.25,
    }
    return {
        "schema_version": 1,
        "evaluation_type": "deterministic-offline-no-model",
        "scenarios": scenarios,
        "gates": gates,
        "passed": all(gates.values()),
        "limitations": [
            "No external model was invoked; task success/answer quality is not measured.",
            "Token counts use tiktoken when installed, otherwise the conservative production estimator.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    report = run_evaluation()
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    print(rendered)
    if args.output:
        target = Path(args.output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
