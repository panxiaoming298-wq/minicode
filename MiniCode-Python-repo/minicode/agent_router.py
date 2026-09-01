"""Intelligent Agent Router for MiniCode.

Automatically selects the optimal model based on task complexity,
budget constraints, and performance requirements.

Routing strategies:
1. Complexity-based: Simple tasks use cheap models, complex tasks use powerful ones
2. Cost-optimized: Balance quality vs cost based on budget
3. Performance-optimized: Always use fastest available model
4. Custom rules: User-defined routing rules
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minicode.logging_config import get_logger
from minicode.model_registry import BUILTIN_MODELS, ModelInfo

logger = get_logger("agent_router")


# ---------------------------------------------------------------------------
# Task complexity classification
# ---------------------------------------------------------------------------

class TaskComplexity(str, Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    CRITICAL = "critical"


@dataclass
class TaskProfile:
    """Profile extracted from task description for routing decisions."""
    complexity: TaskComplexity = TaskComplexity.MODERATE
    estimated_tokens: int = 5000
    requires_coding: bool = False
    requires_reasoning: bool = False
    requires_creativity: bool = False
    is_dangerous: bool = False
    keywords: list[str] = field(default_factory=list)
    deadline_urgent: bool = False


# Complexity indicators
_SIMPLE_KEYWORDS = {
    "list", "show", "display", "what is", "explain", "summarize",
    "format", "convert", "count", "search", "find", "read",
}

_MODERATE_KEYWORDS = {
    "create", "write", "implement", "add", "modify", "update",
    "refactor", "optimize", "test", "debug", "fix", "analyze",
    "compare", "review", "check", "validate",
}

_COMPLEX_KEYWORDS = {
    "architect", "design", "build", "develop", "integrate",
    "migrate", "deploy", "automate", "orchestrate", "pipeline",
    "framework", "system", "platform", "infrastructure",
}

_CRITICAL_KEYWORDS = {
    "security", "production", "critical", "emergency", "urgent",
    "data loss", "breach", "vulnerability", "compliance",
}

_CODING_KEYWORDS = {
    "code", "function", "class", "python", "javascript", "typescript",
    "react", "django", "flask", "api", "database", "sql", "html", "css",
    "implement", "algorithm", "data structure", "bug", "test",
}

_REASONING_KEYWORDS = {
    "analyze", "evaluate", "compare", "reason", "explain why",
    "pros and cons", "trade-offs", "architecture", "design pattern",
    "best practice", "strategy",
}

_CREATIVITY_KEYWORDS = {
    "creative", "innovative", "unique", "original", "design",
    "brainstorm", "generate ideas", "concept", "vision",
}

_DANGEROUS_KEYWORDS = {
    "delete", "drop", "destroy", "remove all", "format",
    "rm -rf", "sudo", "admin", "production", "live",
}


@functools.lru_cache(maxsize=256)
def _classify_complexity(text: str) -> TaskComplexity:
    """Classify task complexity based on keywords and length.
    
    Result is cached to avoid re-analyzing the same or similar tasks.
    """
    text_lower = text.lower()
    
    # Check for critical keywords first
    if any(kw in text_lower for kw in _CRITICAL_KEYWORDS):
        return TaskComplexity.CRITICAL
    
    # Count complexity indicators
    complex_score = sum(1 for kw in _COMPLEX_KEYWORDS if kw in text_lower)
    moderate_score = sum(1 for kw in _MODERATE_KEYWORDS if kw in text_lower)
    simple_score = sum(1 for kw in _SIMPLE_KEYWORDS if kw in text_lower)
    
    # Length-based heuristic
    length_factor = min(len(text) / 500, 1.0)  # 0-1 based on 500 chars
    
    # Combined score
    total_score = (complex_score * 3 + moderate_score * 1.5 + simple_score * 0.5 + 
                   length_factor * 2)
    
    if total_score >= 6:
        return TaskComplexity.CRITICAL
    elif total_score >= 4:
        return TaskComplexity.COMPLEX
    elif total_score >= 2:
        return TaskComplexity.MODERATE
    else:
        return TaskComplexity.SIMPLE


def extract_task_profile(text: str) -> TaskProfile:
    """Extract a task profile from user input for routing decisions."""
    text_lower = text.lower()
    
    # Estimate token count (rough heuristic)
    estimated_tokens = max(500, len(text) // 4)
    
    # Detect requirements
    requires_coding = any(kw in text_lower for kw in _CODING_KEYWORDS)
    requires_reasoning = any(kw in text_lower for kw in _REASONING_KEYWORDS)
    requires_creativity = any(kw in text_lower for kw in _CREATIVITY_KEYWORDS)
    is_dangerous = any(kw in text_lower for kw in _DANGEROUS_KEYWORDS)
    deadline_urgent = any(kw in text_lower for kw in {"urgent", "asap", "immediately", "critical"})
    
    # Extract keywords
    keywords = []
    for kw_set in [_SIMPLE_KEYWORDS, _MODERATE_KEYWORDS, _COMPLEX_KEYWORDS, 
                   _CRITICAL_KEYWORDS, _CODING_KEYWORDS]:
        keywords.extend(kw for kw in kw_set if kw in text_lower)
    
    return TaskProfile(
        complexity=_classify_complexity(text),
        estimated_tokens=estimated_tokens,
        requires_coding=requires_coding,
        requires_reasoning=requires_reasoning,
        requires_creativity=requires_creativity,
        is_dangerous=is_dangerous,
        keywords=keywords[:10],  # Limit to top 10
        deadline_urgent=deadline_urgent,
    )


# ---------------------------------------------------------------------------
# Model tier definitions
# ---------------------------------------------------------------------------

@dataclass
class ModelTier:
    """A tier of models for a given complexity level."""
    name: str
    complexity: TaskComplexity
    primary_model: str  # Model name to use
    fallback_models: list[str] = field(default_factory=list)
    max_cost_per_task: float | None = None  # USD
    description: str = ""


# Default tier configuration
DEFAULT_TIERS: list[ModelTier] = [
    ModelTier(
        name="fast",
        complexity=TaskComplexity.SIMPLE,
        primary_model="claude-haiku-3-20240307",
        fallback_models=["gpt-4o-mini"],
        max_cost_per_task=0.01,
        description="Fast, cheap model for simple tasks",
    ),
    ModelTier(
        name="balanced",
        complexity=TaskComplexity.MODERATE,
        primary_model="claude-sonnet-4-20250514",
        fallback_models=["gpt-4o", "claude-sonnet-4"],
        max_cost_per_task=0.10,
        description="Balanced quality and cost for moderate tasks",
    ),
    ModelTier(
        name="powerful",
        complexity=TaskComplexity.COMPLEX,
        primary_model="claude-opus-4-20250514",
        fallback_models=["claude-sonnet-4-20250514"],
        max_cost_per_task=0.50,
        description="Powerful model for complex tasks",
    ),
    ModelTier(
        name="critical",
        complexity=TaskComplexity.CRITICAL,
        primary_model="claude-opus-4-20250514",
        fallback_models=["claude-sonnet-4-20250514"],
        max_cost_per_task=1.00,
        description="Best model for critical tasks",
    ),
]


# ---------------------------------------------------------------------------
# Agent Router
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    """Record of a routing decision for observability."""
    task_text: str
    profile: TaskProfile
    selected_model: str
    tier_name: str
    reasoning: str
    timestamp: float = field(default_factory=time.time)
    estimated_cost: float = 0.0
    
    def to_log(self) -> str:
        return (
            f"Routing: complexity={self.profile.complexity.value}, "
            f"model={self.selected_model}, tier={self.tier_name}, "
            f"cost~=${self.estimated_cost:.4f}"
        )


class AgentRouter:
    """Routes tasks to optimal models based on complexity and constraints."""
    
    def __init__(
        self,
        tiers: list[ModelTier] | None = None,
        available_models: dict[str, ModelInfo] | None = None,
        budget_per_hour: float = 5.0,  # USD
        force_model: str | None = None,  # Override all routing
    ):
        self.tiers = tiers or DEFAULT_TIERS
        self.available_models = available_models or BUILTIN_MODELS
        self.budget_per_hour = budget_per_hour
        self.force_model = force_model
        self._history: list[RoutingDecision] = []
        self._spending_this_hour: float = 0.0
        self._hour_start: float = time.time()
    
    def route_task(self, task_text: str) -> RoutingDecision:
        """Route a task to the optimal model.
        
        Args:
            task_text: User's task description
            
        Returns:
            RoutingDecision with selected model and reasoning
        """
        # If forced model is set, always use it
        if self.force_model:
            decision = RoutingDecision(
                task_text=task_text,
                profile=extract_task_profile(task_text),
                selected_model=self.force_model,
                tier_name="forced",
                reasoning=f"Forced to use {self.force_model}",
            )
            self._history.append(decision)
            return decision
        
        # Extract task profile
        profile = extract_task_profile(task_text)
        
        # Find matching tier
        tier = self._find_matching_tier(profile.complexity)
        
        # Check budget constraints
        if not self._check_budget(tier):
            tier = self._find_cheapest_tier()
        
        # Select model
        model = self._select_model(tier)
        
        # Estimate cost
        estimated_cost = self._estimate_cost(model, profile.estimated_tokens)
        
        decision = RoutingDecision(
            task_text=task_text[:200],  # Truncate for logging
            profile=profile,
            selected_model=model,
            tier_name=tier.name,
            reasoning=f"Complexity: {profile.complexity.value}, "
                     f"Coding: {profile.requires_coding}, "
                     f"Reasoning: {profile.requires_reasoning}",
            estimated_cost=estimated_cost,
        )
        
        self._history.append(decision)
        logger.info(decision.to_log())
        
        return decision
    
    def _find_matching_tier(self, complexity: TaskComplexity) -> ModelTier:
        """Find the tier matching the task complexity."""
        for tier in self.tiers:
            if tier.complexity == complexity:
                return tier
        # Fallback to balanced
        return next((t for t in self.tiers if t.complexity == TaskComplexity.MODERATE), self.tiers[0])
    
    def _check_budget(self, tier: ModelTier) -> bool:
        """Check if we're within budget."""
        if tier.max_cost_per_task is None:
            return True
        
        # Reset hourly budget
        now = time.time()
        if now - self._hour_start > 3600:
            self._spending_this_hour = 0.0
            self._hour_start = now
        
        return self._spending_this_hour + tier.max_cost_per_task <= self.budget_per_hour
    
    def _find_cheapest_tier(self) -> ModelTier:
        """Find the cheapest available tier."""
        return min(self.tiers, key=lambda t: t.max_cost_per_task or float('inf'))
    
    def _select_model(self, tier: ModelTier) -> str:
        """Select the best available model from a tier."""
        # Try primary first
        if tier.primary_model in self.available_models:
            return tier.primary_model
        
        # Try fallbacks
        for fallback in tier.fallback_models:
            if fallback in self.available_models:
                return fallback
        
        # Last resort: any available model
        if self.available_models:
            return next(iter(self.available_models))
        
        raise RuntimeError("No models available for routing")
    
    def _estimate_cost(self, model_name: str, estimated_tokens: int) -> float:
        """Estimate the cost of running a task on a model."""
        model = self.available_models.get(model_name)
        if not model:
            return 0.0
        
        # Rough estimate: input tokens + output tokens (assume 2x output)
        input_cost = (estimated_tokens / 1_000_000) * model.pricing_input
        output_cost = (estimated_tokens * 2 / 1_000_000) * model.pricing_output
        return input_cost + output_cost
    
    def record_actual_cost(self, actual_cost: float) -> None:
        """Record the actual cost of a completed task."""
        now = time.time()
        if now - self._hour_start > 3600:
            self._spending_this_hour = 0.0
            self._hour_start = now
        self._spending_this_hour += actual_cost
    
    def get_routing_stats(self) -> dict[str, Any]:
        """Get statistics about routing decisions."""
        if not self._history:
            return {
                "total_decisions": 0,
                "avg_estimated_cost": 0.0,
                "spending_this_hour": self._spending_this_hour,
            }
        
        complexity_counts: dict[str, int] = {}
        model_counts: dict[str, int] = {}
        total_cost = 0.0
        
        for decision in self._history:
            complexity_counts[decision.profile.complexity.value] = \
                complexity_counts.get(decision.profile.complexity.value, 0) + 1
            model_counts[decision.selected_model] = \
                model_counts.get(decision.selected_model, 0) + 1
            total_cost += decision.estimated_cost
        
        return {
            "total_decisions": len(self._history),
            "complexity_distribution": complexity_counts,
            "model_distribution": model_counts,
            "avg_estimated_cost": total_cost / len(self._history),
            "total_estimated_cost": total_cost,
            "spending_this_hour": self._spending_this_hour,
            "budget_remaining": max(0, self.budget_per_hour - self._spending_this_hour),
        }
    
    def force_model_selection(self, model_name: str | None) -> None:
        """Force or unforce a specific model."""
        self.force_model = model_name


# Module-level singleton
_default_router: AgentRouter | None = None


def get_agent_router() -> AgentRouter:
    """Get the global agent router."""
    global _default_router
    if _default_router is None:
        _default_router = AgentRouter()
    return _default_router


def reset_agent_router() -> None:
    """Reset the global router (useful for testing)."""
    global _default_router
    _default_router = None
