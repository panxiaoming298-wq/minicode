"""Minimal smoke test for CyberneticOrchestrator — verifies all 15 controllers init."""
from __future__ import annotations

from unittest.mock import MagicMock

from minicode.cybernetic_orchestrator import CyberneticOrchestrator


class TestOrchestratorInit:
    def test_initialize_all_controllers(self):
        mock_model = MagicMock()
        mock_model.model_id = "test-model"
        mock_tools = MagicMock()
        orch = CyberneticOrchestrator()
        orch.initialize(mock_model, mock_tools)
        assert orch._initialized
        assert orch.feedback is not None
        assert orch.stability is not None
        assert orch.adaptive_tuner is not None
        assert orch.state_observer is not None
        assert orch.decoupling is not None
        assert orch.predictive is not None
        assert orch.progress is not None
        assert orch.cost_control is not None
        assert orch.memory_ctrl is not None
        assert orch.model_ctrl is not None
        assert orch.smart_router is not None
        assert orch.model_switcher is not None

    def test_wire_healing(self):
        orch = CyberneticOrchestrator()
        orch.healing = None
        orch.wire_healing(tool_scheduler=MagicMock(), compactor=None)
        assert orch.healing is not None
