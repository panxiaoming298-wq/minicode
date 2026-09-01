"""Decision Audit - Agent decision audit logging.

Inspired by explicit decision recording:
- All Agent decisions are recorded
- Decision chains are traceable and auditable
- Supports decision replay and analysis
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from minicode.logging_config import get_logger

logger = get_logger("decision_audit")


class DecisionType(str, Enum):
    ROUTING = "routing"
    TOOL_SELECTION = "tool_selection"
    MODEL_SELECTION = "model_selection"
    PERMISSION = "permission"
    MEMORY = "memory"
    CONTEXT = "context"
    RETRY = "retry"
    FALLBACK = "fallback"
    CUSTOM = "custom"


class DecisionOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    OVERRIDDEN = "overridden"


@dataclass
class DecisionRecord:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)
    decision_type: DecisionType = DecisionType.CUSTOM
    agent_id: str = ""
    session_id: str = ""
    input_context: dict[str, Any] = field(default_factory=dict)
    available_options: list[str] = field(default_factory=list)
    reasoning: str = ""
    selected_option: str = ""
    confidence: float = 0.0
    outcome: DecisionOutcome = DecisionOutcome.SUCCESS
    execution_time_ms: float = 0.0
    result_summary: str = ""
    error_message: str = ""
    parent_decision_id: str = ""
    child_decisions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "timestamp": self.timestamp,
            "decision_type": self.decision_type.value, "agent_id": self.agent_id,
            "session_id": self.session_id, "input_context": self.input_context,
            "available_options": self.available_options, "reasoning": self.reasoning,
            "selected_option": self.selected_option, "confidence": self.confidence,
            "outcome": self.outcome.value, "execution_time_ms": self.execution_time_ms,
            "result_summary": self.result_summary, "error_message": self.error_message,
            "parent_decision_id": self.parent_decision_id,
            "child_decisions": self.child_decisions,
        }


class DecisionAuditor:
    def __init__(self, log_dir: str | Path | None = None):
        self.log_dir = Path(log_dir) if log_dir else Path.home() / ".mini-code" / "audit"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._records: list[DecisionRecord] = []
        self._session_records: dict[str, list[DecisionRecord]] = {}
        self._current_session: str = ""
        self._decision_stack: list[str] = []

    def start_session(self, session_id: str) -> None:
        self._current_session = session_id
        self._decision_stack.clear()

    def record(self, decision_type: DecisionType, reasoning: str, selected_option: str,
               available_options: list[str] | None = None,
               input_context: dict[str, Any] | None = None,
               confidence: float = 0.0, parent_id: str | None = None) -> DecisionRecord:
        record = DecisionRecord(
            decision_type=decision_type, agent_id="minicode",
            session_id=self._current_session,
            input_context=input_context or {},
            available_options=available_options or [],
            reasoning=reasoning, selected_option=selected_option,
            confidence=confidence,
            parent_decision_id=parent_id or (self._decision_stack[-1] if self._decision_stack else ""),
        )
        self._records.append(record)
        if self._current_session:
            if self._current_session not in self._session_records:
                self._session_records[self._current_session] = []
            self._session_records[self._current_session].append(record)
        if record.parent_decision_id:
            for r in self._records:
                if r.id == record.parent_decision_id:
                    r.child_decisions.append(record.id)
                    break
        self._decision_stack.append(record.id)
        return record

    def update_outcome(self, record_id: str, outcome: DecisionOutcome,
                       execution_time_ms: float = 0.0,
                       result_summary: str = "", error_message: str = "") -> bool:
        for record in self._records:
            if record.id == record_id:
                record.outcome = outcome
                record.execution_time_ms = execution_time_ms
                record.result_summary = result_summary
                record.error_message = error_message
                if record_id in self._decision_stack:
                    self._decision_stack.remove(record_id)
                return True
        return False

    def complete_decision(self, outcome: DecisionOutcome = DecisionOutcome.SUCCESS,
                          execution_time_ms: float = 0.0,
                          result_summary: str = "", error_message: str = "") -> bool:
        if not self._decision_stack:
            return False
        record_id = self._decision_stack[-1]
        return self.update_outcome(record_id, outcome, execution_time_ms, result_summary, error_message)

    def get_session_decisions(self, session_id: str | None = None) -> list[DecisionRecord]:
        sid = session_id or self._current_session
        return list(self._session_records.get(sid, []))

    def get_decision_chain(self, record_id: str) -> list[DecisionRecord]:
        chain: list[DecisionRecord] = []
        current_id = record_id
        visited: set[str] = set()
        while current_id:
            if current_id in visited:
                break
            visited.add(current_id)
            for record in self._records:
                if record.id == current_id:
                    chain.append(record)
                    current_id = record.parent_decision_id
                    break
            else:
                break
        chain.reverse()
        return chain

    def get_stats(self) -> dict[str, Any]:
        if not self._records:
            return {"total_decisions": 0}
        outcomes = {}
        types = {}
        total_time = 0.0
        for record in self._records:
            outcomes[record.outcome.value] = outcomes.get(record.outcome.value, 0) + 1
            types[record.decision_type.value] = types.get(record.decision_type.value, 0) + 1
            total_time += record.execution_time_ms
        return {
            "total_decisions": len(self._records),
            "sessions": len(self._session_records),
            "outcomes": outcomes,
            "types": types,
            "avg_execution_time_ms": round(total_time / len(self._records), 2),
            "success_rate": round(outcomes.get("success", 0) / len(self._records) * 100, 1),
        }

    def save_session(self, session_id: str | None = None) -> Path:
        sid = session_id or self._current_session
        if not sid:
            raise ValueError("No session ID provided")
        records = self._session_records.get(sid, [])
        if not records:
            return Path()
        filename = f"audit_{sid}_{int(time.time())}.json"
        filepath = self.log_dir / filename
        data = {
            "session_id": sid, "saved_at": time.time(),
            "stats": self.get_stats(),
            "records": [r.to_dict() for r in records],
        }
        filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return filepath

    def export_report(self, session_id: str | None = None) -> str:
        sid = session_id or self._current_session
        records = self._session_records.get(sid, self._records)
        if not records:
            return "No decisions recorded."
        lines = ["# Decision Audit Report", f"Session: {sid or 'all'}", f"Total Decisions: {len(records)}", "", "## Outcomes"]
        outcomes: dict[str, int] = {}
        for r in records:
            outcomes[r.outcome.value] = outcomes.get(r.outcome.value, 0) + 1
        for outcome, count in sorted(outcomes.items(), key=lambda x: -x[1]):
            lines.append(f"- {outcome}: {count}")
        lines.extend(["", "## Decision Chain"])
        root_records = [r for r in records if not r.parent_decision_id]
        for root in root_records[:5]:
            lines.append(f"\n### Decision {root.id}")
            lines.append(f"- Type: {root.decision_type.value}")
            lines.append(f"- Selected: {root.selected_option}")
            lines.append(f"- Reasoning: {root.reasoning[:100]}...")
            lines.append(f"- Outcome: {root.outcome.value}")
            if root.child_decisions:
                lines.append(f"- Sub-decisions: {len(root.child_decisions)}")
        return "\n".join(lines)

    def clear(self) -> None:
        self._records.clear()
        self._session_records.clear()
        self._decision_stack.clear()


_auditor: DecisionAuditor | None = None


def get_auditor() -> DecisionAuditor:
    global _auditor
    if _auditor is None:
        _auditor = DecisionAuditor()
    return _auditor


def audited(decision_type: DecisionType, option_extractor: str | None = None):
    def decorator(func):
        def wrapper(*args, **kwargs):
            auditor = get_auditor()
            available = []
            if option_extractor and kwargs:
                available = kwargs.get(option_extractor, [])
            record = auditor.record(
                decision_type=decision_type,
                reasoning=f"Function: {func.__name__}",
                selected_option="pending",
                available_options=available,
                input_context={"args": str(args), "kwargs": str(kwargs)},
            )
            start = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = (time.time() - start) * 1000
                auditor.update_outcome(record.id, DecisionOutcome.SUCCESS, elapsed, str(result)[:200])
                return result
            except Exception as e:
                elapsed = (time.time() - start) * 1000
                auditor.update_outcome(record.id, DecisionOutcome.FAILURE, elapsed, error_message=str(e))
                raise
        return wrapper
    return decorator
