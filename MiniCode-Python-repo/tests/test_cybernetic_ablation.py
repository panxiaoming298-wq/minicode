import json

from minicode.cybernetic_ablation import (
    CyberneticAblationRunner,
    format_ablation_report,
    load_harness_task_profiles,
    load_harness_run_evidence,
)


def test_ablation_returns_paired_arms_for_each_task():
    data = CyberneticAblationRunner().run()

    assert data["task_count"] == 5
    assert data["arms"] == ["baseline", "cybernetic"]
    assert len(data["results"]) == data["task_count"] * 2

    task_ids = {item["task_id"] for item in data["results"]}
    for task_id in task_ids:
        arms = {item["arm"] for item in data["results"] if item["task_id"] == task_id}
        assert arms == {"baseline", "cybernetic"}


def test_cybernetic_arm_improves_control_metrics():
    data = CyberneticAblationRunner().run()
    delta = data["summary"]["delta_cybernetic_minus_baseline"]

    assert delta["completion_score"] > 0
    assert delta["tool_error_rate"] < 0
    assert delta["context_peak"] < 0
    assert delta["verification_strength"] > 0


def test_ablation_report_can_be_written(tmp_path):
    runner = CyberneticAblationRunner()
    data = runner.run()
    paths = runner.write_outputs(tmp_path, data)

    assert paths["json"].exists()
    assert paths["markdown"].exists()
    report = paths["markdown"].read_text(encoding="utf-8")
    assert "MiniCode 控制论消融实验" in report
    assert "Cybernetic - Baseline" in report


def test_report_formatter_includes_task_level_rows():
    data = CyberneticAblationRunner().run()
    report = format_ablation_report(data)

    assert "debug-core-loop" in report
    assert "failure-recovery" in report
    assert "supervisor risk" in report


def test_harness_loader_maps_oracle_and_metadata_to_profiles(tmp_path):
    task_dir = tmp_path / "G4-verification-trap-calibration"
    task_dir.mkdir()
    (task_dir / "metadata.yaml").write_text(
        "\n".join([
            "id: G4-verification-trap-calibration",
            "bundle: B4 Verification-Constraint Control",
            "variant: calibration",
            "primary_failure_labels:",
            "  - forbidden_shortcut_used",
            "  - verification_not_rerun",
            "  - false_completion",
        ]),
        encoding="utf-8",
    )
    (task_dir / "oracle.json").write_text(
        json.dumps({
            "key_files": ["repo/pkg/adapter.py", "repo/pkg/public_api.py"],
            "forbidden": ["repo/pkg/public_api.py"],
        }),
        encoding="utf-8",
    )

    profiles = load_harness_task_profiles(tmp_path)

    assert len(profiles) == 1
    assert profiles[0].task_id == "G4-verification-trap-calibration"
    assert profiles[0].coverage_sensitive is True
    assert profiles[0].tests_passed is False
    assert "repo/pkg/adapter.py" in profiles[0].changed_files


def test_runner_can_use_harness_profiles(tmp_path):
    task_dir = tmp_path / "G5-resource-fork-counterfactual"
    task_dir.mkdir()
    (task_dir / "metadata.yaml").write_text(
        "\n".join([
            "id: G5-resource-fork-counterfactual",
            "bundle: B5 Resource-Fork Control",
            "variant: counterfactual",
            "primary_failure_labels:",
            "  - expensive_suite_overused",
            "  - retry_policy_not_grounded",
        ]),
        encoding="utf-8",
    )
    (task_dir / "oracle.json").write_text(
        json.dumps({"key_files": ["repo/payment/retry.py"]}),
        encoding="utf-8",
    )

    data = CyberneticAblationRunner().run_from_harness(tmp_path)

    assert data["source"] == str(tmp_path)
    assert data["task_count"] == 1
    assert data["summary"]["delta_cybernetic_minus_baseline"]["verification_strength"] > 0


def test_results_json_evidence_is_summarized(tmp_path):
    evidence_path = tmp_path / "results.json"
    evidence_path.write_text(
        json.dumps([
            {
                "condition": "baseline",
                "status": "completed",
                "visible_pass": True,
                "hidden_pass": False,
                "grader_success": False,
                "elapsed_sec": 10.0,
                "diagnostic_labels": ["missing_verification"],
            },
            {
                "condition": "verification_gate",
                "status": "completed",
                "visible_pass": True,
                "hidden_pass": True,
                "grader_success": True,
                "elapsed_sec": 12.0,
                "diagnostic_labels": [],
            },
        ]),
        encoding="utf-8",
    )

    evidence = load_harness_run_evidence(evidence_path)

    assert evidence["schema"] == "results_json"
    assert evidence["conditions"]["baseline"]["grader_success_rate"] == 0.0
    assert evidence["conditions"]["verification_gate"]["grader_success_rate"] == 1.0
    assert evidence["delta"]["cybernetic_minus_baseline"] == 1.0


def test_profile_json_evidence_is_included_in_report(tmp_path):
    evidence_path = tmp_path / "profile.json"
    evidence_path.write_text(
        json.dumps({
            "hygiene_profiles": {
                "naive": {"green": 1, "red": 2, "labels": {"constraint_violation": 2}},
                "policy_full": {"green": 3, "red": 0, "labels": {}},
            },
        }),
        encoding="utf-8",
    )

    data = CyberneticAblationRunner().run()
    data["harness_evidence"] = load_harness_run_evidence(evidence_path)
    report = format_ablation_report(data)

    assert "已有 Harness 运行证据" in report
    assert "policy_full" in report
    assert "+0.667" in report
