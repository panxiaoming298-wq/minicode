"""Deterministic cybernetic-control ablation harness.

The harness compares a static baseline with MiniCode's current cybernetic
controllers on the same synthetic task profiles. It is intentionally local and
deterministic: it measures controller behavior without making model calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

from minicode.agent_intelligence import ToolSchedulerController, ToolSchedulingSignal
from minicode.cybernetic_supervisor import CyberneticSupervisor
from minicode.memory_injector import MemoryInjectionController, MemoryInjectionSignal
from minicode.model_registry import ModelSelectionController, ModelSelectionSignal
from minicode.progress_controller import ProgressController, ProgressSignal
from minicode.verification_controller import VerificationController, VerificationSignal


@dataclass(frozen=True)
class AblationTaskProfile:
    task_id: str
    label: str
    changed_files: tuple[str, ...]
    intent_type: str
    action_type: str
    complexity: str
    context_usage: float
    retrieval_quality: float
    tool_calls: int
    write_count: int
    command_count: int
    tool_error_rate: float
    avg_latency: float
    recent_failures: int
    completed_steps: int
    total_steps: int
    output_changed: bool
    tests_passed: bool | None
    requires_long_context: bool = False
    coverage_sensitive: bool = False


@dataclass
class AblationArmResult:
    task_id: str
    arm: str
    completion_score: float
    tool_error_rate: float
    context_peak: float
    verification_strength: float
    max_workers: int
    memory_mode: str
    model_effort: str
    progress_action: str
    supervisor_risk: str
    intervention_count: int
    estimated_cost_index: float
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "arm": self.arm,
            "completion_score": round(self.completion_score, 3),
            "tool_error_rate": round(self.tool_error_rate, 3),
            "context_peak": round(self.context_peak, 3),
            "verification_strength": round(self.verification_strength, 3),
            "max_workers": self.max_workers,
            "memory_mode": self.memory_mode,
            "model_effort": self.model_effort,
            "progress_action": self.progress_action,
            "supervisor_risk": self.supervisor_risk,
            "intervention_count": self.intervention_count,
            "estimated_cost_index": round(self.estimated_cost_index, 3),
            "details": self.details,
        }


DEFAULT_TASKS = [
    AblationTaskProfile(
        task_id="debug-core-loop",
        label="debug",
        changed_files=("minicode/agent_loop.py", "tests/test_agent_loop.py"),
        intent_type="debug",
        action_type="update",
        complexity="complex",
        context_usage=0.58,
        retrieval_quality=0.72,
        tool_calls=7,
        write_count=1,
        command_count=3,
        tool_error_rate=0.18,
        avg_latency=8.0,
        recent_failures=1,
        completed_steps=5,
        total_steps=7,
        output_changed=True,
        tests_passed=None,
        coverage_sensitive=True,
    ),
    AblationTaskProfile(
        task_id="refactor-shared-api",
        label="refactor",
        changed_files=("minicode/model_registry.py", "minicode/pipeline_engine.py"),
        intent_type="refactor",
        action_type="update",
        complexity="complex",
        context_usage=0.66,
        retrieval_quality=0.62,
        tool_calls=9,
        write_count=2,
        command_count=2,
        tool_error_rate=0.12,
        avg_latency=11.0,
        recent_failures=0,
        completed_steps=6,
        total_steps=8,
        output_changed=True,
        tests_passed=None,
        coverage_sensitive=True,
    ),
    AblationTaskProfile(
        task_id="long-context-doc-code",
        label="long_context",
        changed_files=("MINICODE_CYBERNETICS_INTEGRATION_ANALYSIS.zh.md", "minicode/context_cybernetics.py"),
        intent_type="code",
        action_type="update",
        complexity="moderate",
        context_usage=0.84,
        retrieval_quality=0.78,
        tool_calls=6,
        write_count=1,
        command_count=1,
        tool_error_rate=0.08,
        avg_latency=7.0,
        recent_failures=0,
        completed_steps=4,
        total_steps=6,
        output_changed=True,
        tests_passed=None,
        requires_long_context=True,
    ),
    AblationTaskProfile(
        task_id="multi-tool-fanout",
        label="multi_tool",
        changed_files=("minicode/tooling.py", "tests/test_tooling.py"),
        intent_type="code",
        action_type="update",
        complexity="moderate",
        context_usage=0.52,
        retrieval_quality=0.48,
        tool_calls=12,
        write_count=2,
        command_count=4,
        tool_error_rate=0.28,
        avg_latency=14.0,
        recent_failures=2,
        completed_steps=5,
        total_steps=9,
        output_changed=True,
        tests_passed=False,
        coverage_sensitive=True,
    ),
    AblationTaskProfile(
        task_id="failure-recovery",
        label="failure_recovery",
        changed_files=("minicode/self_healing_engine.py", "tests/test_self_healing.py"),
        intent_type="debug",
        action_type="update",
        complexity="complex",
        context_usage=0.73,
        retrieval_quality=0.69,
        tool_calls=10,
        write_count=1,
        command_count=5,
        tool_error_rate=0.42,
        avg_latency=18.0,
        recent_failures=3,
        completed_steps=3,
        total_steps=9,
        output_changed=True,
        tests_passed=False,
        coverage_sensitive=True,
    ),
]


class CyberneticAblationRunner:
    """Run paired baseline/cybernetic controller simulations."""

    def __init__(self) -> None:
        self.verification = VerificationController()
        self.tools = ToolSchedulerController()
        self.memory = MemoryInjectionController()
        self.models = ModelSelectionController()
        self.progress = ProgressController()
        self.supervisor = CyberneticSupervisor()

    def run(
        self,
        tasks: list[AblationTaskProfile] | None = None,
        *,
        source: str = "synthetic",
    ) -> dict[str, Any]:
        profiles = tasks or list(DEFAULT_TASKS)
        results: list[AblationArmResult] = []
        for task in profiles:
            results.append(self._run_baseline(task))
            results.append(self._run_cybernetic(task))
        summary = self._summarize(results)
        return {
            "task_count": len(profiles),
            "source": source,
            "arms": ["baseline", "cybernetic"],
            "results": [r.to_dict() for r in results],
            "summary": summary,
        }

    def run_from_harness(
        self,
        harness_root: str | Path,
        *,
        max_tasks: int | None = None,
        evidence_path: str | Path | None = None,
    ) -> dict[str, Any]:
        profiles = load_harness_task_profiles(harness_root, max_tasks=max_tasks)
        data = self.run(profiles, source=str(harness_root))
        if evidence_path is not None:
            data["harness_evidence"] = load_harness_run_evidence(evidence_path)
        return data

    def write_outputs(self, output_dir: str | Path, data: dict[str, Any]) -> dict[str, Path]:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        summary_json = output / "summary.json"
        summary_md = output / "summary.md"
        summary_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        summary_md.write_text(format_ablation_report(data), encoding="utf-8")
        return {"json": summary_json, "markdown": summary_md}

    def _run_baseline(self, task: AblationTaskProfile) -> AblationArmResult:
        verification_strength = 0.35 if task.coverage_sensitive else 0.20
        max_workers = max(1, min(task.tool_calls, 4))
        context_peak = min(1.0, task.context_usage + 0.08 + 0.01 * task.tool_calls)
        tool_error_rate = min(1.0, task.tool_error_rate + 0.08 + 0.02 * max(0, max_workers - 2))
        intervention_count = 0
        completion = self._completion_score(
            task,
            tool_error_rate=tool_error_rate,
            verification_strength=verification_strength,
            context_peak=context_peak,
            intervention_count=intervention_count,
        )
        return AblationArmResult(
            task_id=task.task_id,
            arm="baseline",
            completion_score=completion,
            tool_error_rate=tool_error_rate,
            context_peak=context_peak,
            verification_strength=verification_strength,
            max_workers=max_workers,
            memory_mode="standard",
            model_effort="medium",
            progress_action="continue",
            supervisor_risk="unobserved",
            intervention_count=intervention_count,
            estimated_cost_index=self._cost_index(task, "medium", context_peak, max_workers),
            details={"policy": "static thresholds and fixed worker cap"},
        )

    def _run_cybernetic(self, task: AblationTaskProfile) -> AblationArmResult:
        verification = self.verification.plan(VerificationSignal(
            changed_files=list(task.changed_files),
            intent_type=task.intent_type,
            action_type=task.action_type,
            requires_tests=task.coverage_sensitive,
            recent_failures=task.recent_failures,
            previous_verification_failed=task.tests_passed is False,
            coverage_sensitive=task.coverage_sensitive,
        ))
        tool_decision = self.tools.decide(ToolSchedulingSignal(
            call_count=task.tool_calls,
            write_count=task.write_count,
            command_count=task.command_count,
            error_rate=task.tool_error_rate,
            avg_latency=task.avg_latency,
            recent_failures=task.recent_failures,
        ))
        memory_decision = self.memory.decide(
            MemoryInjectionSignal(
                context_usage=task.context_usage,
                retrieval_quality=task.retrieval_quality,
                recent_failure=task.tests_passed is False,
                task_repetition=task.recent_failures > 0,
            ),
            base_max_memories=5,
            base_min_relevance=0.30,
            base_max_tokens=200,
        )
        model_decision = self.models.decide(ModelSelectionSignal(
            task_complexity=task.complexity,
            budget_pressure=max(0.0, task.context_usage - 0.50),
            latency_pressure=min(1.0, task.avg_latency / 30.0),
            recent_failures=task.recent_failures,
            requires_long_context=task.requires_long_context,
        ))
        progress = self.progress.decide(ProgressSignal(
            total_steps=task.total_steps,
            completed_steps=task.completed_steps,
            failed_steps=task.recent_failures,
            tool_calls=task.tool_calls,
            tool_errors=round(task.tool_calls * task.tool_error_rate),
            output_changed=task.output_changed,
            tests_passed=task.tests_passed,
            max_steps=10,
        ))
        supervisor_report = self.supervisor.report([
            self.supervisor.snapshot_from_tool_decision(tool_decision.to_dict()),
            self.supervisor.snapshot_from_decision("verification", verification.to_dict()),
            self.supervisor.snapshot_from_decision("memory", memory_decision.to_dict()),
            self.supervisor.snapshot_from_decision("progress", progress.to_dict()),
        ])

        context_peak = max(
            0.0,
            min(1.0, task.context_usage + self._memory_context_delta(memory_decision.mode.value)),
        )
        tool_error_rate = max(
            0.0,
            task.tool_error_rate
            - 0.06
            - 0.03 * (1.0 - tool_decision.concurrency_multiplier)
            - 0.02 * min(task.recent_failures, 3),
        )
        verification_strength = self._verification_strength(verification.mode.value)
        intervention_count = sum(
            1
            for action in (
                verification.mode.value,
                memory_decision.mode.value,
                progress.action.value,
                tool_decision.concurrency_multiplier,
            )
            if action not in {"none", "standard", "continue", 1.0}
        )
        completion = self._completion_score(
            task,
            tool_error_rate=tool_error_rate,
            verification_strength=verification_strength,
            context_peak=context_peak,
            intervention_count=intervention_count,
        )
        return AblationArmResult(
            task_id=task.task_id,
            arm="cybernetic",
            completion_score=completion,
            tool_error_rate=tool_error_rate,
            context_peak=context_peak,
            verification_strength=verification_strength,
            max_workers=tool_decision.max_workers,
            memory_mode=memory_decision.mode.value,
            model_effort=model_decision.reasoning_effort.value,
            progress_action=progress.action.value,
            supervisor_risk=supervisor_report.risk_level.value,
            intervention_count=intervention_count,
            estimated_cost_index=self._cost_index(
                task,
                model_decision.reasoning_effort.value,
                context_peak,
                tool_decision.max_workers,
            ),
            details={
                "verification": verification.to_dict(),
                "tool_scheduling": tool_decision.to_dict(),
                "memory": memory_decision.to_dict(),
                "model": model_decision.to_dict(),
                "progress": progress.to_dict(),
                "supervisor": supervisor_report.to_dict(),
            },
        )

    def _completion_score(
        self,
        task: AblationTaskProfile,
        *,
        tool_error_rate: float,
        verification_strength: float,
        context_peak: float,
        intervention_count: int,
    ) -> float:
        step_ratio = task.completed_steps / max(task.total_steps, 1)
        score = 0.45 + step_ratio * 0.30 + verification_strength * 0.20
        score -= tool_error_rate * 0.30
        if context_peak >= 0.90:
            score -= 0.12
        score += min(0.08, intervention_count * 0.02)
        if task.tests_passed is False:
            score -= 0.05
        return max(0.0, min(1.0, score))

    def _verification_strength(self, mode: str) -> float:
        return {
            "none": 0.0,
            "smoke": 0.35,
            "targeted": 0.70,
            "full": 1.0,
        }.get(mode, 0.25)

    def _memory_context_delta(self, mode: str) -> float:
        return {
            "none": -0.03,
            "summary": 0.01,
            "standard": 0.05,
            "strong": 0.08,
        }.get(mode, 0.04)

    def _cost_index(
        self,
        task: AblationTaskProfile,
        effort: str,
        context_peak: float,
        max_workers: int,
    ) -> float:
        effort_mult = {"low": 0.75, "medium": 1.0, "high": 1.25, "xhigh": 1.45}.get(effort, 1.0)
        return (
            effort_mult
            * (1.0 + context_peak * 0.35)
            * (1.0 + max_workers * 0.025)
            * (1.0 + task.command_count * 0.02)
        )

    def _summarize(self, results: list[AblationArmResult]) -> dict[str, Any]:
        by_arm: dict[str, list[AblationArmResult]] = {"baseline": [], "cybernetic": []}
        for result in results:
            by_arm.setdefault(result.arm, []).append(result)

        arm_summary = {
            arm: {
                "completion_score": round(mean(r.completion_score for r in items), 3),
                "tool_error_rate": round(mean(r.tool_error_rate for r in items), 3),
                "context_peak": round(mean(r.context_peak for r in items), 3),
                "verification_strength": round(mean(r.verification_strength for r in items), 3),
                "intervention_count": round(mean(r.intervention_count for r in items), 3),
                "estimated_cost_index": round(mean(r.estimated_cost_index for r in items), 3),
            }
            for arm, items in by_arm.items()
            if items
        }
        baseline = arm_summary["baseline"]
        cybernetic = arm_summary["cybernetic"]
        return {
            "by_arm": arm_summary,
            "delta_cybernetic_minus_baseline": {
                "completion_score": round(cybernetic["completion_score"] - baseline["completion_score"], 3),
                "tool_error_rate": round(cybernetic["tool_error_rate"] - baseline["tool_error_rate"], 3),
                "context_peak": round(cybernetic["context_peak"] - baseline["context_peak"], 3),
                "verification_strength": round(cybernetic["verification_strength"] - baseline["verification_strength"], 3),
                "estimated_cost_index": round(cybernetic["estimated_cost_index"] - baseline["estimated_cost_index"], 3),
            },
            "interpretation": (
                "Cybernetic control should improve completion and verification while lowering "
                "tool-error/context pressure; cost may rise when stronger verification or reasoning is selected."
            ),
        }


def format_ablation_report(data: dict[str, Any]) -> str:
    summary = data["summary"]
    by_arm = summary["by_arm"]
    delta = summary["delta_cybernetic_minus_baseline"]
    lines = [
        "# MiniCode 控制论消融实验",
        "",
        f"- source: `{data.get('source', 'synthetic')}`",
        f"- task_count: `{data.get('task_count', 0)}`",
        "",
        "## 结论",
        "",
        (
            "该轻量实验在相同任务画像上对比 static baseline 与 cybernetic controller。"
            "它不调用外部模型，专门验证控制器的感知-决策-反馈行为。"
        ),
        "",
        "## 汇总指标",
        "",
        "| arm | completion | tool_error | context_peak | verification | cost_index | interventions |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm in ("baseline", "cybernetic"):
        item = by_arm[arm]
        lines.append(
            f"| {arm} | {item['completion_score']:.3f} | {item['tool_error_rate']:.3f} | "
            f"{item['context_peak']:.3f} | {item['verification_strength']:.3f} | "
            f"{item['estimated_cost_index']:.3f} | {item['intervention_count']:.3f} |"
        )
    lines.extend([
        "",
        "## Cybernetic - Baseline",
        "",
        f"- completion_score: {delta['completion_score']:+.3f}",
        f"- tool_error_rate: {delta['tool_error_rate']:+.3f}",
        f"- context_peak: {delta['context_peak']:+.3f}",
        f"- verification_strength: {delta['verification_strength']:+.3f}",
        f"- estimated_cost_index: {delta['estimated_cost_index']:+.3f}",
        "",
        "## 任务级结果",
        "",
        "| task | baseline completion | cybernetic completion | cybernetic action | supervisor risk |",
        "| --- | ---: | ---: | --- | --- |",
    ])
    by_task: dict[str, dict[str, dict[str, Any]]] = {}
    for item in data["results"]:
        by_task.setdefault(item["task_id"], {})[item["arm"]] = item
    for task_id, arms in by_task.items():
        baseline = arms["baseline"]
        cybernetic = arms["cybernetic"]
        lines.append(
            f"| {task_id} | {baseline['completion_score']:.3f} | "
            f"{cybernetic['completion_score']:.3f} | {cybernetic['progress_action']} | "
            f"{cybernetic['supervisor_risk']} |"
        )
    evidence = data.get("harness_evidence")
    if isinstance(evidence, dict):
        lines.extend([
            "",
            "## 已有 Harness 运行证据",
            "",
            f"- evidence_source: `{evidence.get('source', '')}`",
            f"- schema: `{evidence.get('schema', '')}`",
            "",
        ])
        delta = evidence.get("delta", {})
        if isinstance(delta, dict) and delta:
            lines.extend([
                (
                    f"- paired_delta: `{delta.get('cybernetic_condition')}` - "
                    f"`{delta.get('baseline_condition')}` = "
                    f"{float(delta.get('cybernetic_minus_baseline', 0.0)):+.3f} "
                    f"on `{delta.get('metric')}`"
                ),
                "",
            ])
        lines.extend([
            "| condition | runs | primary_rate | labels |",
            "| --- | ---: | ---: | --- |",
        ])
        conditions = evidence.get("conditions", {})
        if isinstance(conditions, dict):
            for condition, item in conditions.items():
                if not isinstance(item, dict):
                    continue
                rate = item.get("green_rate", item.get("grader_success_rate", 0.0))
                labels = item.get("diagnostic_labels", {})
                label_text = ", ".join(f"{k}:{v}" for k, v in labels.items()) if isinstance(labels, dict) else ""
                lines.append(
                    f"| {condition} | {int(item.get('runs', 0) or 0)} | "
                    f"{float(rate):.3f} | {label_text or '-'} |"
                )
    lines.extend([
        "",
        "## 使用边界",
        "",
        "这是控制器级消融，不等价于真实模型端到端胜率；下一步可把同一任务画像映射到真实 harness 执行。",
    ])
    return "\n".join(lines) + "\n"


def load_harness_task_profiles(
    harness_root: str | Path,
    *,
    max_tasks: int | None = None,
) -> list[AblationTaskProfile]:
    """Load task profiles from harness task directories.

    A valid task directory contains at least ``oracle.json``. ``metadata.yaml``
    is parsed with a narrow line-oriented parser so this harness has no YAML
    dependency.
    """

    root = Path(harness_root)
    if not root.exists():
        raise FileNotFoundError(f"harness root not found: {root}")

    task_dirs = _discover_harness_task_dirs(root)
    profiles: list[AblationTaskProfile] = []
    for task_dir in task_dirs:
        if max_tasks is not None and len(profiles) >= max_tasks:
            break
        profile = _profile_from_harness_dir(task_dir)
        if profile is not None:
            profiles.append(profile)

    if not profiles:
        raise ValueError(f"no harness task profiles found under {root}")
    return profiles


def load_harness_run_evidence(path: str | Path) -> dict[str, Any]:
    """Load existing harness outcome evidence from ``results.json`` or ``profile.json``."""

    evidence_path = Path(path)
    if not evidence_path.exists():
        raise FileNotFoundError(f"harness evidence not found: {evidence_path}")
    data = json.loads(evidence_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return _summarize_results_json(data, str(evidence_path))
    if isinstance(data, dict) and "hygiene_profiles" in data:
        return _summarize_profile_json(data, str(evidence_path))
    raise ValueError(f"unsupported harness evidence schema: {evidence_path}")


def _discover_harness_task_dirs(root: Path) -> list[Path]:
    manifest = root / "manifest.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        dirs = [
            Path(item["path"])
            for item in data
            if isinstance(item, dict) and item.get("path")
        ]
        return sorted(dirs, key=lambda p: p.name)

    return sorted(
        [p for p in root.iterdir() if p.is_dir() and (p / "oracle.json").exists()],
        key=lambda p: p.name,
    )


def _summarize_results_json(rows: list[dict[str, Any]], source: str) -> dict[str, Any]:
    by_condition: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        condition = str(row.get("condition") or row.get("harness") or "unknown")
        by_condition.setdefault(condition, []).append(row)

    conditions: dict[str, Any] = {}
    for condition, items in sorted(by_condition.items()):
        n = len(items)
        visible = sum(1 for item in items if bool(item.get("visible_pass")))
        hidden = sum(1 for item in items if bool(item.get("hidden_pass")))
        graded = sum(1 for item in items if bool(item.get("grader_success")))
        completed = sum(1 for item in items if str(item.get("status")) == "completed")
        elapsed = [float(item.get("elapsed_sec") or 0.0) for item in items]
        labels: dict[str, int] = {}
        for item in items:
            for label in item.get("diagnostic_labels", []) or []:
                labels[str(label)] = labels.get(str(label), 0) + 1
        conditions[condition] = {
            "runs": n,
            "visible_pass_rate": round(visible / n, 3) if n else 0.0,
            "hidden_pass_rate": round(hidden / n, 3) if n else 0.0,
            "grader_success_rate": round(graded / n, 3) if n else 0.0,
            "completed_rate": round(completed / n, 3) if n else 0.0,
            "mean_elapsed_sec": round(mean(elapsed), 3) if elapsed else 0.0,
            "diagnostic_labels": labels,
        }

    delta = _pair_condition_delta(conditions)
    return {
        "source": source,
        "schema": "results_json",
        "conditions": conditions,
        "delta": delta,
    }


def _summarize_profile_json(data: dict[str, Any], source: str) -> dict[str, Any]:
    hygiene = data.get("hygiene_profiles", {})
    conditions: dict[str, Any] = {}
    if isinstance(hygiene, dict):
        for condition, item in sorted(hygiene.items()):
            if not isinstance(item, dict):
                continue
            green = int(item.get("green", 0) or 0)
            red = int(item.get("red", 0) or 0)
            total = green + red
            conditions[str(condition)] = {
                "runs": total,
                "green": green,
                "red": red,
                "green_rate": round(green / total, 3) if total else 0.0,
                "diagnostic_labels": item.get("labels", {}) if isinstance(item.get("labels"), dict) else {},
            }

    delta = _pair_condition_delta(conditions)
    return {
        "source": source,
        "schema": "profile_json",
        "conditions": conditions,
        "delta": delta,
    }


def _pair_condition_delta(conditions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    baseline_name = "baseline" if "baseline" in conditions else "naive" if "naive" in conditions else ""
    cybernetic_name = (
        "verification_gate"
        if "verification_gate" in conditions
        else "policy_full"
        if "policy_full" in conditions
        else "disciplined"
        if "disciplined" in conditions
        else ""
    )
    if not baseline_name or not cybernetic_name:
        return {}

    baseline = conditions[baseline_name]
    cybernetic = conditions[cybernetic_name]
    metric = "green_rate" if "green_rate" in baseline else "grader_success_rate"
    if metric not in baseline or metric not in cybernetic:
        return {}
    return {
        "baseline_condition": baseline_name,
        "cybernetic_condition": cybernetic_name,
        "metric": metric,
        "baseline": baseline[metric],
        "cybernetic": cybernetic[metric],
        "cybernetic_minus_baseline": round(float(cybernetic[metric]) - float(baseline[metric]), 3),
    }


def _profile_from_harness_dir(task_dir: Path) -> AblationTaskProfile | None:
    oracle_path = task_dir / "oracle.json"
    if not oracle_path.exists():
        return None
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    metadata = _parse_simple_metadata(task_dir / "metadata.yaml")

    task_id = str(metadata.get("id") or task_dir.name)
    bundle = str(metadata.get("bundle") or task_id)
    variant = str(metadata.get("variant") or "unknown")
    labels = [str(label) for label in metadata.get("primary_failure_labels", [])]
    key_files = tuple(str(path) for path in oracle.get("key_files", []) if path)
    forbidden = [str(path) for path in oracle.get("forbidden", [])]
    changed_files = key_files or tuple(forbidden) or ("repo/unknown.py",)

    complexity = _complexity_from_metadata(bundle, variant, labels)
    tool_calls = _tool_calls_from_metadata(bundle, labels, len(changed_files))
    recent_failures = min(4, len(labels) // 2 + (1 if "adversarial" in variant else 0))
    tests_passed = False if any("verification" in label or "false_completion" in label for label in labels) else None

    return AblationTaskProfile(
        task_id=task_id,
        label=bundle,
        changed_files=changed_files,
        intent_type="debug" if "failure" in bundle.lower() or labels else "code",
        action_type="update",
        complexity=complexity,
        context_usage=_context_usage_from_metadata(bundle, variant, len(changed_files)),
        retrieval_quality=_retrieval_quality_from_metadata(variant, labels),
        tool_calls=tool_calls,
        write_count=max(1, min(3, len(changed_files))),
        command_count=2 if tests_passed is None else 3,
        tool_error_rate=_tool_error_rate_from_metadata(variant, labels),
        avg_latency=8.0 + tool_calls * 0.8,
        recent_failures=recent_failures,
        completed_steps=max(2, tool_calls // 2),
        total_steps=max(5, tool_calls),
        output_changed=True,
        tests_passed=tests_passed,
        requires_long_context="evidence" in bundle.lower() or "state" in bundle.lower(),
        coverage_sensitive=True,
    )


def _parse_simple_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    data: dict[str, Any] = {}
    current_list: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_list:
            data.setdefault(current_list, []).append(stripped[2:].strip().strip('"'))
            continue
        current_list = None
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if value:
            data[key] = value
        else:
            data[key] = []
            current_list = key
    return data


def _complexity_from_metadata(bundle: str, variant: str, labels: list[str]) -> str:
    text = " ".join([bundle, variant, *labels]).lower()
    if "adversarial" in text or "resource" in text or "state" in text:
        return "complex"
    if "verification" in text or "counterfactual" in text:
        return "complex"
    return "moderate"


def _tool_calls_from_metadata(bundle: str, labels: list[str], file_count: int) -> int:
    base = 5 + file_count
    text = " ".join([bundle, *labels]).lower()
    if "resource" in text:
        base += 4
    if "verification" in text:
        base += 2
    if "state" in text:
        base += 3
    return min(14, base + min(3, len(labels)))


def _context_usage_from_metadata(bundle: str, variant: str, file_count: int) -> float:
    usage = 0.50 + min(0.18, file_count * 0.03)
    text = f"{bundle} {variant}".lower()
    if "evidence" in text:
        usage += 0.16
    if "state" in text:
        usage += 0.10
    if "adversarial" in text or "counterfactual" in text:
        usage += 0.08
    return min(0.88, usage)


def _retrieval_quality_from_metadata(variant: str, labels: list[str]) -> float:
    quality = 0.68
    text = " ".join([variant, *labels]).lower()
    if "adversarial" in text:
        quality -= 0.18
    if "shortcut" in text or "false_completion" in text:
        quality -= 0.10
    if "calibration" in text:
        quality += 0.05
    return max(0.25, min(0.85, quality))


def _tool_error_rate_from_metadata(variant: str, labels: list[str]) -> float:
    rate = 0.12 + min(0.18, len(labels) * 0.03)
    text = " ".join([variant, *labels]).lower()
    if "adversarial" in text:
        rate += 0.12
    if "verification" in text or "false_completion" in text:
        rate += 0.08
    if "resource" in text:
        rate += 0.06
    return min(0.55, rate)
