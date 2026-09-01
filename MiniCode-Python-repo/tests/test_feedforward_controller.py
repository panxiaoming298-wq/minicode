"""Unit tests for FeedforwardController — preemptive config, risk assessment, intent routing."""
from __future__ import annotations

from minicode.feedforward_controller import (
    FeedforwardController,
    PreemptiveConfig,
    PreemptionLevel,
    RiskAssessment,
)
from minicode.intent_parser import ActionType, IntentType, ParsedIntent


def _make_intent(
    intent_type=IntentType.CODE,
    action_type=ActionType.UPDATE,
    complexity="moderate",
    confidence=0.7,
):
    return ParsedIntent(
        intent_type=intent_type,
        action_type=action_type,
        complexity_hint=complexity,
        confidence=confidence,
        entities={},
        raw_input="test",
    )


class TestPreemptiveConfig:
    """PreemptiveConfig defaults and merge."""

    def test_default_values(self):
        cfg = PreemptiveConfig()
        assert cfg.token_budget == 4000
        assert cfg.max_concurrent_tools == 4
        assert cfg.tool_timeout_seconds == 30.0
        assert cfg.max_turn_steps == 30
        assert cfg.confidence == 0.7
        assert cfg.recommended_model == "claude-sonnet-4"

    def test_merge_with_defaults(self):
        a = PreemptiveConfig(token_budget=8000)
        b = PreemptiveConfig()
        merged = a.merge_with_defaults(b)
        assert merged.token_budget == 8000
        assert merged.max_concurrent_tools == 4  # From defaults
        assert merged.recommended_model == "claude-sonnet-4"


class TestFeedforwardPreconfigure:
    """FeedforwardController.preconfigure() per intent type."""

    def test_code_intent(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.CODE, ActionType.UPDATE, "complex")
        cfg = fc.preconfigure(intent, "Write a function")
        assert isinstance(cfg, PreemptiveConfig)
        assert cfg.confidence > 0.3
        assert cfg.max_turn_steps > 0

    def test_debug_intent(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.DEBUG, ActionType.READ, "moderate")
        cfg = fc.preconfigure(intent, "Debug the error")
        assert isinstance(cfg, PreemptiveConfig)

    def test_refactor_intent(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.REFACTOR, ActionType.UPDATE, "complex")
        cfg = fc.preconfigure(intent, "Refactor the module")
        assert isinstance(cfg, PreemptiveConfig)
        # Refactoring typically needs higher token budget
        assert cfg.token_budget >= 4000

    def test_simple_task(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.SEARCH, ActionType.READ, "simple")
        cfg = fc.preconfigure(intent, "Find X in file Y")
        assert cfg.max_turn_steps <= 30  # Simple tasks get fewer steps

    def test_complexity_multiplier_affects_config(self):
        fc = FeedforwardController()
        simple = _make_intent(IntentType.CODE, ActionType.UPDATE, "simple")
        complex_ = _make_intent(IntentType.CODE, ActionType.UPDATE, "complex")
        cfg_simple = fc.preconfigure(simple, "test")
        cfg_complex = fc.preconfigure(complex_, "test")
        # Complex tasks should have higher limits
        assert cfg_complex.token_budget >= cfg_simple.token_budget


class TestFeedforwardAssessRisks:
    """RiskAssessment logic."""

    def test_write_action_has_permission_risk(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.CODE, ActionType.CREATE, "moderate")
        cfg = PreemptiveConfig()
        ra = fc.assess_risks(intent, cfg)
        assert ra.has_permission_risk is True
        assert len(ra.identified_risks) > 0

    def test_delete_action_has_mitigation(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.CODE, ActionType.DELETE, "moderate")
        cfg = PreemptiveConfig()
        ra = fc.assess_risks(intent, cfg)
        assert ra.has_permission_risk is True
        assert any("backup" in m.lower() for m in ra.mitigation_steps)

    def test_read_only_no_permission_risk(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.SEARCH, ActionType.READ, "moderate")
        cfg = PreemptiveConfig()
        ra = fc.assess_risks(intent, cfg)
        assert ra.has_permission_risk is False

    def test_complex_task_high_risk(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.CODE, ActionType.UPDATE, "complex")
        cfg = PreemptiveConfig(tool_timeout_seconds=60.0)
        ra = fc.assess_risks(intent, cfg)
        assert ra.risk_level in ("high", "critical")

    def test_simple_task_low_risk(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.SEARCH, ActionType.READ, "simple", confidence=0.9)
        cfg = PreemptiveConfig()
        ra = fc.assess_risks(intent, cfg)
        assert ra.risk_level == "low"


class TestFeedforwardPreemptionLevel:
    """PreemptionLevel selection."""

    def test_simple_confident_low(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.SEARCH, ActionType.READ, "simple", confidence=0.9)
        level = fc.get_optimal_preemption_level(intent)
        assert level == PreemptionLevel.LOW

    def test_complex_high(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.CODE, ActionType.UPDATE, "complex")
        level = fc.get_optimal_preemption_level(intent)
        assert level == PreemptionLevel.HIGH

    def test_moderate_medium(self):
        fc = FeedforwardController()
        intent = _make_intent(IntentType.DEBUG, ActionType.READ, "moderate")
        level = fc.get_optimal_preemption_level(intent)
        assert level == PreemptionLevel.MEDIUM


class TestFeedforwardHistory:
    """Config history tracking."""

    def test_history_accumulates(self):
        fc = FeedforwardController()
        for _ in range(5):
            intent = _make_intent()
            fc.preconfigure(intent, "test")
        assert len(fc.get_config_history()) == 5

    def test_reset_clears_history(self):
        fc = FeedforwardController()
        fc.preconfigure(_make_intent(), "test")
        fc.reset()
        assert len(fc.get_config_history()) == 0
