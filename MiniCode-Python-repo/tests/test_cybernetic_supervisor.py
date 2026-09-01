from minicode.cybernetic_supervisor import (
    ControlSnapshot,
    CyberneticSupervisor,
    SupervisorRisk,
    load_supervisor_report,
    save_supervisor_report,
)
from minicode.intent_parser import ActionType, IntentType, ParsedIntent
from minicode.pipeline_engine import get_pipeline_engine
from minicode.task_object import TaskObject


class TestCyberneticSupervisor:
    def test_empty_report_is_low_risk(self):
        report = CyberneticSupervisor().report([])
        assert report.risk_level == SupervisorRisk.LOW
        assert report.overall_health == 1.0

    def test_context_snapshot_marks_high_usage_as_risky(self):
        supervisor = CyberneticSupervisor()
        snap = supervisor.snapshot_from_context({
            "sensor": {"current_usage": 0.92},
            "predictor": {"urgency": 0.3},
        })
        assert snap.action == "compact"
        assert snap.risk == 0.92

    def test_report_aggregates_highest_risk(self):
        supervisor = CyberneticSupervisor()
        report = supervisor.report([
            ControlSnapshot(name="context", health=0.2, risk=0.9, action="compact"),
            ControlSnapshot(name="memory", health=0.8, risk=0.2, action="standard"),
        ])
        assert report.risk_level == SupervisorRisk.CRITICAL
        assert "context: compact" in report.recommended_actions

    def test_decision_snapshot_reads_progress_stall_score(self):
        snap = CyberneticSupervisor().snapshot_from_decision(
            "progress",
            {"action": "switch_strategy", "health_score": 0.4, "stall_score": 0.6},
        )
        assert snap.action == "switch_strategy"
        assert snap.risk == 0.6

    def test_tool_decision_snapshot_tracks_parallelism_pressure(self):
        snap = CyberneticSupervisor().snapshot_from_tool_decision({
            "concurrency_multiplier": 0.4,
            "cooldown_seconds": 1.0,
            "retry_backoff_multiplier": 2.0,
            "reasons": ["high tool error rate"],
        })
        assert snap.name == "tool_scheduling"
        assert snap.action == "increase_retry_backoff"
        assert snap.risk > 0.5

    def test_summary_format_is_readable(self):
        report = CyberneticSupervisor().report([
            ControlSnapshot(name="verification", health=0.5, risk=0.75, action="full")
        ])
        summary = report.format_summary()
        assert "Cybernetic Supervisor" in summary
        assert "risk_level" in summary

    def test_report_persistence_roundtrip(self, tmp_path, monkeypatch):
        import minicode.cybernetic_supervisor as supervisor_module

        monkeypatch.setattr(
            supervisor_module,
            "SUPERVISOR_STATE_PATH",
            tmp_path / "cybernetic_supervisor.json",
        )
        report = CyberneticSupervisor().report([
            ControlSnapshot(name="context", health=0.2, risk=0.9, action="compact")
        ])
        save_supervisor_report(report)
        loaded = load_supervisor_report()
        assert loaded is not None
        assert loaded.risk_level == report.risk_level
        assert loaded.snapshots[0].name == "context"


class TestSupervisorPipelineIntegration:
    def test_pipeline_outputs_supervisor_report(self):
        task = TaskObject(
            raw_input="explain code",
            parsed_intent=ParsedIntent(
                raw_input="explain code",
                intent_type=IntentType.EXPLAIN,
                action_type=ActionType.READ,
                confidence=1.0,
            ),
        )
        result = get_pipeline_engine().run(task)
        assert "cybernetic_supervisor" in result.outputs
        assert "risk_level" in result.outputs["cybernetic_supervisor"]
        assert "recommended_actions" in result.outputs["cybernetic_supervisor"]
