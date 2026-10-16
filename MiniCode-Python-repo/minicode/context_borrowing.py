"""Request-scoped handoff preparation and recovery from context admission failure.

The conversation setting and administrative ceiling never change. A native
memory runtime owns the loan; model text and saved settings cannot grant one.
Only a verified rotation followed by normal admission can resume business work.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy


_current: ContextVar[ContextBorrowing | None] = ContextVar("context_handoff_borrowing", default=None)


def request_context_limit(model, runtime):
    from minicode.context_manager import resolve_context_limit

    normal = resolve_context_limit(model, runtime)
    recovery = _current.get()
    if recovery is not None:
        if recovery.handoff_only:
            recovery.validate_identity(model, runtime, normal)
            return recovery.borrowed_window if recovery.active else normal
        if recovery.restoring:
            return min(normal, recovery.working_window)
    return normal


def current_context_borrowing():
    return _current.get()


def _hard_context_window(model, runtime):
    from minicode.context_manager import configured_context_cap, get_model_context_window
    from minicode.model_registry import BUILTIN_MODELS

    # Substring recognition is useful for ordinary conservative planning, but
    # an unregistered alias cannot prove capacity for an emergency exception.
    registered = BUILTIN_MODELS.get(model.strip().lower())
    resolved = get_model_context_window(model)
    if registered is None or not resolved.known:
        return 0
    physical = min(registered.context_window, resolved.context_window)
    administrative = runtime.get("_conversationBaseContextCap", configured_context_cap(runtime))
    administrative = configured_context_cap({"contextWindow": administrative})
    return min(physical, administrative) if administrative else physical


@contextmanager
def bind_context_borrowing(recovery):
    if recovery is None or not recovery.recovering:
        yield
        return
    from minicode.usage_ledger import BudgetUnavailable

    if recovery.handoff_only:
        if recovery.requests >= recovery.memory.config.context_recovery_max_steps:
            raise BudgetUnavailable("Context borrowing handoff request limit reached",
                                    diagnostics={"classification": "context_recovery"})
        recovery.requests += 1
    token = _current.set(recovery)
    try:
        yield
    finally:
        _current.reset(token)


class ContextBorrowing:
    def __init__(self, memory):
        self.memory = memory
        self.handoff = False
        self.active = False
        self.restoring = False
        self.requests = 0
        self.working_window = 0
        self.borrowed_window = 0
        self.hard_window = 0
        self.epoch = 0
        self.model = ""
        self.protocol = ""
        self.endpoint = ""
        self.trigger = "normal_context_admission_failed"

    @property
    def handoff_only(self):
        return self.handoff or self.active

    @property
    def recovering(self):
        return self.handoff_only or self.restoring

    @staticmethod
    def capacity_failure(error):
        from minicode.paged_memory.service import ContextBudgetExceeded
        from minicode.usage_ledger import BudgetUnavailable

        if isinstance(error, ContextBudgetExceeded):
            estimated, limit = error.estimated_tokens, error.input_limit
        elif isinstance(error, BudgetUnavailable) and error.diagnostics.get("classification") == "context_window":
            estimated = error.diagnostics.get("input_reservation")
            limit = error.diagnostics.get("input_limit")
        else:
            return None
        if type(estimated) is not int or type(limit) is not int or estimated <= limit:
            return None
        return estimated, limit

    def begin_handoff(self, error, budget):
        if self.capacity_failure(error) is None:
            return False
        return self._begin(budget, trigger="normal_context_admission_failed")

    def begin_preparation(self, packet, budget):
        """Prepare a verified note before exposing business tools at high pressure.

        The caller has already tried automatic rotation and normal capacity
        admission. This grants no extra space and reuses the bounded handoff
        state machine, including its final normal-request restoration check.
        """
        if packet.debug_metadata.get("context_utilization", {}).get("stage") not in {"rotate", "protect"}:
            return False
        return self._begin(budget, trigger="context_preparation")

    def _begin(self, budget, *, trigger):
        if (budget is None or self.recovering
                or not self.memory.context_utilization.enabled or not self.memory.context_rotation.enabled
                or not self.memory.handoff_notes.prepared or budget.tools.find("memory_note_update") is None):
            return False
        self.working_window = self.borrowed_window = budget.context_window
        self.hard_window = _hard_context_window(budget.model_id, budget.runtime)
        self.model, self.protocol, self.endpoint = budget.model_id, budget.protocol, budget.endpoint.rstrip("/")
        self.epoch = self.memory.last_packet.debug_metadata.get("context_rotation", {}).get("epoch", 0)
        self.requests = 0
        self.handoff = True
        self.trigger = trigger
        return True

    def activate(self, error, budget):
        from minicode.paged_memory.context_utilization import RECOVERY_TOOLS

        failure = self.capacity_failure(error)
        if (budget is None or not self.handoff_only or self.restoring or failure is None
                or self.requests >= self.memory.config.context_recovery_max_steps
                or not {tool.name for tool in budget.tools.list()} <= RECOVERY_TOOLS):
            return False
        normal = getattr(budget, "normal_budget", budget)
        self.validate_identity(normal.model_id, normal.runtime, normal.context_window)
        estimated, limit = failure
        # An absent conversation boundary is an administrative cap, not spare
        # user-selected capacity. Explicit server limits are never overridden.
        hard = _hard_context_window(normal.model_id, normal.runtime)
        if hard <= self.working_window:
            return False
        # Automatic output can fit the recovery request just as it fits normal
        # dispatch. Explicit output keeps its fixed reservation contract.
        reserve = max(budget.capacity["output_reserve_tokens"],
                      budget.context_window - limit - budget.config.safety_token_reserve)
        needed = estimated + reserve + budget.config.safety_token_reserve
        if needed > hard:
            return False
        self.hard_window = hard
        self.borrowed_window = min(hard, max(self.borrowed_window, self.working_window + 8192, needed + 4096))
        self.active = True
        return True

    def recover(self, error, normal):
        # A final-wire failure first enters a normal-capacity, handoff-only
        # phase. Only failure of that restricted request can grant extra space.
        if self.capacity_failure(error) is None:
            return False
        if not self.recovering:
            return self.begin_handoff(error, normal)
        if self.restoring or normal is None:
            return False
        return self.activate(error, self.request_budget(normal))

    def validate_identity(self, model, runtime, normal):
        from minicode.usage_ledger import BudgetUnavailable

        hard = _hard_context_window(model, runtime)
        if (model != self.model or normal != self.working_window
                or (self.active and hard < self.borrowed_window)):
            raise BudgetUnavailable("Context borrowing belongs to a different model or capacity policy",
                                    diagnostics={"classification": "context_recovery"})

    def filter_tools(self, tools):
        from minicode.paged_memory.context_utilization import RECOVERY_TOOLS

        return tools.filtered_without_names(frozenset(tool.name for tool in tools.list() if tool.name not in RECOVERY_TOOLS))

    def validate_request(self, body, *, purpose, protocol, endpoint):
        from minicode.paged_memory.context_utilization import RECOVERY_TOOLS
        from minicode.usage_ledger import BudgetUnavailable

        names = {tool.get("function", {}).get("name") if protocol == "openai" else tool.get("name")
                 for tool in body.get("tools", [])}
        if (purpose != "agent" or protocol != self.protocol or endpoint.rstrip("/") != self.endpoint
                or body.get("model") != self.model or not names <= RECOVERY_TOOLS
                or "memory_note_update" not in names):
            raise BudgetUnavailable("Borrowed context is restricted to this native handoff request",
                                    diagnostics={"classification": "context_recovery"})

    def request_budget(self, normal):
        from minicode.output_budget import context_capacity
        from minicode.usage_ledger import BudgetUnavailable

        if normal is None:
            raise BudgetUnavailable("Handoff recovery requires a native request budget",
                                    diagnostics={"classification": "context_recovery"})
        self.validate_identity(normal.model_id, normal.runtime, normal.context_window)
        budget = copy(normal)
        budget.normal_budget = normal
        budget.tools = self.filter_tools(normal.tools)
        if self.active:
            budget.context_window = self.borrowed_window
            budget.capacity = context_capacity(context_window=self.borrowed_window,
                output_tokens=normal.capacity["output_reserve_tokens"], output_floor=normal.config.output_token_reserve,
                safety_tokens=normal.config.safety_token_reserve)
            # Pressure remains measured against the unchanged working window.
            budget.capacity["budget_policy"] = {**normal.capacity["budget_policy"], "input_limit": budget.capacity["effective_input"]}
            budget.input_limit = budget.capacity["effective_input"]
        return budget

    def mark_packet(self, packet):
        if not self.recovering:
            return
        packet.debug_metadata["context_borrowing"] = self.diagnostic()
        if self.handoff_only:
            status = packet.debug_metadata["context_utilization"]
            status.update(stage="protect", protection=True, handoff_only=True)

    def rotated(self, receipt):
        if self.handoff_only and receipt.get("epoch", 0) > self.epoch:
            self.handoff = False
            self.active = False
            self.restoring = True

    def restored(self, packet):
        if self.restoring:
            packet.debug_metadata["context_borrowing"] = {**self.diagnostic(), "status": "restored", "handoff_only": False}
            self.restoring = False

    def stop(self):
        self.handoff = False
        self.active = False
        self.restoring = False

    def diagnostic(self):
        status = "active" if self.active else "handoff" if self.handoff else "restoring" if self.restoring else "idle"
        return {"policy": "failed-context-handoff-borrow-v1", "status": status,
                "working_context_window": self.working_window, "recovery_context_window": self.borrowed_window,
                "hard_context_window": self.hard_window, "requests": self.requests, "handoff_only": True,
                "borrowed": self.borrowed_window > self.working_window,
                "trigger": self.trigger, "settings_changed": False}
