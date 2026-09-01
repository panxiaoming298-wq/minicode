"""Feedforward Controller based on Engineering Cybernetics."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from minicode.intent_parser import ParsedIntent, IntentType, ActionType

class PreemptionLevel(Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

@dataclass
class PreemptiveConfig:
    token_budget: int = 4000
    context_window_reserve: float = 0.2
    max_concurrent_tools: int = 4
    serial_tools_first: bool = False
    recommended_model: str = "claude-sonnet-4"
    force_model_upgrade: bool = False
    tool_timeout_seconds: float = 30.0
    max_turn_steps: int = 30
    preload_memory_tags: list[str] = field(default_factory=list)
    preload_memory_count: int = 10
    enable_backup_before_write: bool = False
    require_permission_for_all_writes: bool = False
    enable_early_termination: bool = False
    confidence: float = 0.7
    reasoning: str = ""
    def merge_with_defaults(self, defaults: "PreemptiveConfig") -> "PreemptiveConfig":
        result = PreemptiveConfig()
        for key in ["token_budget", "context_window_reserve", "max_concurrent_tools", "serial_tools_first", "recommended_model", "force_model_upgrade", "tool_timeout_seconds", "max_turn_steps", "preload_memory_count", "enable_backup_before_write", "require_permission_for_all_writes", "enable_early_termination", "confidence", "reasoning"]:
            setattr(result, key, getattr(self, key) if getattr(self, key) is not None else getattr(defaults, key))
        result.preload_memory_tags = self.preload_memory_tags or defaults.preload_memory_tags
        return result

@dataclass
class RiskAssessment:
    risk_level: str = "low"
    identified_risks: list[str] = field(default_factory=list)
    mitigation_steps: list[str] = field(default_factory=list)
    estimated_failure_probability: float = 0.0
    has_permission_risk: bool = False
    has_resource_risk: bool = False
    has_complexity_risk: bool = False
    has_timeout_risk: bool = False

class FeedforwardController:
    _INTENT_CONFIGS: dict[IntentType, dict[str, Any]] = {
        IntentType.CODE: {"token_budget": 6000, "max_concurrent_tools": 3, "tool_timeout_seconds": 45.0, "max_turn_steps": 40, "preload_memory_tags": ["coding-conventions", "architecture", "api-design"], "enable_backup_before_write": True, "confidence": 0.8},
        IntentType.DEBUG: {"token_budget": 5000, "max_concurrent_tools": 4, "tool_timeout_seconds": 30.0, "max_turn_steps": 35, "preload_memory_tags": ["debugging", "error-handling", "testing"], "confidence": 0.75},
        IntentType.REFACTOR: {"token_budget": 8000, "max_concurrent_tools": 2, "tool_timeout_seconds": 60.0, "max_turn_steps": 50, "preload_memory_tags": ["refactoring", "architecture", "coding-conventions"], "enable_backup_before_write": True, "require_permission_for_all_writes": True, "confidence": 0.7},
        IntentType.SEARCH: {"token_budget": 3000, "max_concurrent_tools": 6, "tool_timeout_seconds": 20.0, "max_turn_steps": 15, "preload_memory_tags": ["search-patterns", "file-structure"], "confidence": 0.9},
        IntentType.REVIEW: {"token_budget": 4000, "max_concurrent_tools": 5, "tool_timeout_seconds": 25.0, "max_turn_steps": 20, "preload_memory_tags": ["review-checklist", "coding-conventions", "security"], "confidence": 0.85},
        IntentType.TEST: {"token_budget": 5000, "max_concurrent_tools": 4, "tool_timeout_seconds": 45.0, "max_turn_steps": 30, "preload_memory_tags": ["testing", "test-patterns", "test-frameworks"], "enable_backup_before_write": True, "confidence": 0.8},
        IntentType.DOCUMENT: {"token_budget": 3000, "max_concurrent_tools": 5, "tool_timeout_seconds": 20.0, "max_turn_steps": 15, "preload_memory_tags": ["documentation", "api-design"], "confidence": 0.9},
        IntentType.SYSTEM: {"token_budget": 2000, "max_concurrent_tools": 2, "tool_timeout_seconds": 15.0, "max_turn_steps": 10, "preload_memory_tags": [], "enable_early_termination": True, "confidence": 0.95},
    }
    _COMPLEXITY_MULTIPLIERS: dict[str, dict[str, float]] = {
        "simple": {"token_budget": 0.5, "max_turn_steps": 0.5, "tool_timeout_seconds": 0.7, "max_concurrent_tools": 1.5},
        "moderate": {"token_budget": 1.0, "max_turn_steps": 1.0, "tool_timeout_seconds": 1.0, "max_concurrent_tools": 1.0},
        "complex": {"token_budget": 2.0, "max_turn_steps": 1.5, "tool_timeout_seconds": 1.3, "max_concurrent_tools": 0.7},
    }
    _ENTITY_ADJUSTMENTS: dict[str, dict[str, Any]] = {
        "files": {"max_concurrent_tools": 0.8, "tool_timeout_seconds": 1.2},
        "functions": {"max_concurrent_tools": 1.2, "token_budget": 1.3},
        "classes": {"max_concurrent_tools": 1.0, "token_budget": 1.5},
        "languages": {"token_budget": 1.2, "tool_timeout_seconds": 1.1},
    }
    def __init__(self):
        self._config_history: list[tuple[float, PreemptiveConfig]] = []
        self._max_history = 50
    def preconfigure(self, intent: ParsedIntent, raw_input: str = "") -> PreemptiveConfig:
        base_config = self._get_base_config(intent.intent_type)
        adjusted = self._apply_complexity_adjustment(base_config, intent.complexity_hint)
        final = self._apply_entity_adjustment(adjusted, intent.entities)
        final.reasoning = self._generate_reasoning(intent)
        final.confidence = self._compute_confidence(intent)
        self._config_history.append((time.time(), final))
        if len(self._config_history) > self._max_history:
            self._config_history.pop(0)
        return final
    def assess_risks(self, intent: ParsedIntent, config: PreemptiveConfig) -> RiskAssessment:
        assessment = RiskAssessment()
        if intent.action_type in (ActionType.CREATE, ActionType.UPDATE, ActionType.DELETE):
            assessment.has_permission_risk = True
            assessment.identified_risks.append("File modification requires permissions")
            assessment.mitigation_steps.append("Verify write permissions before execution")
            if intent.action_type == ActionType.DELETE:
                assessment.mitigation_steps.append("Enable backup before deletion")
        if intent.complexity_hint == "complex":
            assessment.has_resource_risk = True
            assessment.identified_risks.append("Complex task may exceed resource limits")
            assessment.mitigation_steps.append("Monitor token usage closely")
            assessment.mitigation_steps.append("Consider breaking into subtasks")
        if config.tool_timeout_seconds > 30.0:
            assessment.has_timeout_risk = True
            assessment.identified_risks.append("Long operations may timeout")
            assessment.mitigation_steps.append("Implement progress monitoring")
        if intent.confidence < 0.5:
            assessment.has_complexity_risk = True
            assessment.identified_risks.append("Low intent confidence may lead to wrong approach")
            assessment.mitigation_steps.append("Request clarification if ambiguous")
        risk_score = sum([assessment.has_permission_risk * 0.3, assessment.has_resource_risk * 0.3, assessment.has_timeout_risk * 0.2, assessment.has_complexity_risk * 0.2])
        if risk_score > 0.7:
            assessment.risk_level = "critical"
            assessment.estimated_failure_probability = 0.6
        elif risk_score > 0.5:
            assessment.risk_level = "high"
            assessment.estimated_failure_probability = 0.4
        elif risk_score > 0.2:
            assessment.risk_level = "medium"
            assessment.estimated_failure_probability = 0.2
        else:
            assessment.risk_level = "low"
            assessment.estimated_failure_probability = 0.05
        return assessment
    def get_optimal_preemption_level(self, intent: ParsedIntent) -> PreemptionLevel:
        if intent.complexity_hint == "simple" and intent.confidence > 0.7:
            return PreemptionLevel.LOW
        elif intent.complexity_hint == "complex" or intent.confidence < 0.5:
            return PreemptionLevel.HIGH
        return PreemptionLevel.MEDIUM
    def _get_base_config(self, intent_type: IntentType) -> PreemptiveConfig:
        overrides = self._INTENT_CONFIGS.get(intent_type, {})
        config = PreemptiveConfig()
        for key, value in overrides.items():
            setattr(config, key, value)
        return config
    def _apply_complexity_adjustment(self, config: PreemptiveConfig, complexity: str) -> PreemptiveConfig:
        multipliers = self._COMPLEXITY_MULTIPLIERS.get(complexity, {})
        if "token_budget" in multipliers:
            config.token_budget = int(config.token_budget * multipliers["token_budget"])
        if "max_turn_steps" in multipliers:
            config.max_turn_steps = int(config.max_turn_steps * multipliers["max_turn_steps"])
        if "tool_timeout_seconds" in multipliers:
            config.tool_timeout_seconds = config.tool_timeout_seconds * multipliers["tool_timeout_seconds"]
        if "max_concurrent_tools" in multipliers:
            config.max_concurrent_tools = max(1, int(config.max_concurrent_tools * multipliers["max_concurrent_tools"]))
        return config
    def _apply_entity_adjustment(self, config: PreemptiveConfig, entities: dict[str, list[str]]) -> PreemptiveConfig:
        for entity_type, entity_list in entities.items():
            if entity_type in self._ENTITY_ADJUSTMENTS and entity_list:
                adjustments = self._ENTITY_ADJUSTMENTS[entity_type]
                if "max_concurrent_tools" in adjustments:
                    config.max_concurrent_tools = max(1, int(config.max_concurrent_tools * adjustments["max_concurrent_tools"]))
                if "tool_timeout_seconds" in adjustments:
                    config.tool_timeout_seconds = config.tool_timeout_seconds * adjustments["tool_timeout_seconds"]
                if "token_budget" in adjustments:
                    config.token_budget = int(config.token_budget * adjustments["token_budget"])
        return config
    def _generate_reasoning(self, intent: ParsedIntent) -> str:
        parts = [f"Intent: {intent.intent_type.value}", f"Action: {intent.action_type.value}", f"Complexity: {intent.complexity_hint}", f"Confidence: {intent.confidence:.2f}"]
        if intent.entities:
            entity_summary = ", ".join(f"{k}: {len(v)}" for k, v in intent.entities.items() if v)
            parts.append(f"Entities: {entity_summary}")
        return " | ".join(parts)
    def _compute_confidence(self, intent: ParsedIntent) -> float:
        base = intent.confidence * 0.6
        if intent.intent_type in self._INTENT_CONFIGS:
            base += 0.2
        total_entities = sum(len(v) for v in intent.entities.values())
        if total_entities == 0:
            base -= 0.1
        elif total_entities > 5:
            base += 0.1
        return max(0.3, min(0.95, base))
    def get_config_history(self) -> list[tuple[float, PreemptiveConfig]]:
        return list(self._config_history)
    def reset(self) -> None:
        self._config_history = []
