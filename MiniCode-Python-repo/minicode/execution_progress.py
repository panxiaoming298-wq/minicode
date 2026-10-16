"""Bounded execution feedback shared by primary and worker agent loops.

Tool activity, evidence acquisition and native changes are different facts.
This observer prompts a strategy review; it never grants permission, certifies
completion, caches tool results, or forces a write/stop based on a call count.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import json
import os
import re
import time

from minicode.progress_controller import ProgressSignal


CONTEXT_KIND = "execution_progress"
_READ_WINDOW = re.compile(
    r"\AFILE: [^\n]*\nOFFSET_UNIT: characters \(0-based, END exclusive\)\n"
    r"OFFSET: (\d+)\nEND: (\d+)\n"
)


@dataclass
class ExecutionProgress:
    cwd: str
    started_at: float = field(default_factory=time.monotonic)
    calls: int = 0
    errors: int = 0
    reads: int = 0
    repeated_reads: int = 0
    settled_commands: int = 0
    changes: int = 0
    _checkpoint_calls: int = 0
    _checkpoint_step: int = 0
    _checkpoint_repeats: int = 0
    _checkpoint_at: float | None = None
    _reads: OrderedDict = field(default_factory=OrderedDict)
    _notice_key: tuple | None = None
    _waiting_for_writers: bool = False

    def observe(self, call, result, *, step: int, changed: bool, now: float | None = None):
        """Consume actual receipts once, in tool-call order, after a batch settles.

        `changed` comes from the turn kernel's native write-revision accounting,
        not assistant prose, tool names or a command's zero exit code.
        """
        self.calls += 1
        self.errors += int(not result.ok and not result.inputSchemaRequired)
        settled = result.ok and not (
            result.inputRejected or result.inputValidationFailed or result.awaitUser
            or result.runtimeYield or result.runtimeStopReason or result.timedOut
            or result.ambiguousSideEffect or result.unsettledTool or result.backgroundTask
        )
        if result.commandExecutionSettled:
            self.settled_commands += 1
        if settled and result.contextReadSha256:
            self.reads += 1
            match = _READ_WINDOW.match(result.output)
            path = call.get("input", {}).get("path")
            if match and isinstance(path, str):
                # Compare returned ranges in the SAME native byte revision.
                # A different page, file version or failed read isn't a repeat.
                key = (os.path.normcase(os.path.abspath(os.path.join(self.cwd, path))), result.contextReadSha256)
                ranges = self._reads.pop(key, [])
                start, end = map(int, match.groups())
                if end > start and any(a <= start and end <= b for a, b in ranges):
                    self.repeated_reads += 1
                merged = []
                for a, b in sorted([*ranges, (start, end)]):
                    if merged and a <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                    else:
                        merged.append((a, b))
                self._reads[key] = merged[-64:]
                if len(self._reads) > 128:
                    self._reads.popitem(last=False)
        if settled and changed and result.contextWriteRevision:
            self.changes += 1
            self._checkpoint_calls = self.calls
            self._checkpoint_step = step
            self._checkpoint_repeats = self.repeated_reads
            self._checkpoint_at = time.monotonic() if now is None else now
            self._notice_key = None

    def signal(self, *, step: int, max_steps: int | None, now: float | None = None) -> ProgressSignal:
        return ProgressSignal(
            total_steps=step, completed_steps=None, tool_calls=self.calls,
            tool_errors=self.errors, output_changed=self.changes > 0,
            elapsed_seconds=max(0.0, (time.monotonic() if now is None else now) - self.started_at),
            max_steps=max_steps,
        )

    def review(self, *, step: int, read_only: bool, shared_status=None, now: float | None = None):
        """Return one replaceable request snapshot and a transition-only UI notice.

        Thresholds are review checkpoints, not quotas for edits or investigations.
        Unknown task types receive conditional guidance; capability is not intent.
        Active writers retain their scopes and required fresh reads still run.
        """
        if not self.calls:
            return "", ""
        elapsed = max(0.0, (time.monotonic() if now is None else now)
                      - (self.started_at if self._checkpoint_at is None else self._checkpoint_at))
        calls = self.calls - self._checkpoint_calls
        steps = max(0, step - self._checkpoint_step - 1)
        repeats = self.repeated_reads - self._checkpoint_repeats
        pending = bool(shared_status and (shared_status.get("pending")
                       or shared_status.get("scope_status") == "write_in_progress"))
        stage = "observe"
        if pending:
            stage = "waiting_for_writers"
        elif self._waiting_for_writers:
            stage = "review_delegated_results"
        elif steps >= 3 and (calls >= 8 or repeats >= 3 or (elapsed >= 300 and calls >= 3)):
            stage = "focus"
            if steps >= 6 and (calls >= 16 or repeats >= 6 or elapsed >= 600):
                stage = "change_strategy"
        self._waiting_for_writers = pending
        facts = {
            "scope": "current_agent_activation", "stage": stage,
            "tool_calls": self.calls, "tool_errors": self.errors,
            "native_file_reads": self.reads, "covered_range_rereads": self.repeated_reads,
            "observed_file_changes": self.changes, "settled_commands": self.settled_commands,
            "calls_since_observed_change": calls, "completion": "not_inferred",
        }
        guidance = (
            "Successful tool calls, notes, delegation and baseline checks are activity, not task completion. "
            "Observed changes are native file receipts only; commands and external systems may have effects "
            "not counted here. These facts do not certify correctness or current model access to old evidence. "
        )
        if stage == "waiting_for_writers":
            guidance += (
                "Use the current writer coordination receipt. Wait for unfinished owners, or do independent work "
                "outside their scopes; do not duplicate their edits or spawn replacements merely because this "
                "agent has no edits. After release, inspect their actual changes and run necessary checks. "
                "Unresolved terminal effects need investigation, not repeated waits on terminal tasks."
            )
        elif stage == "review_delegated_results":
            guidance += (
                "Writer scopes have released. Inspect the delegated outcomes and actual changed files now; "
                "scope release alone does not prove success. Reuse supported work, address any concrete failure, "
                "and verify the combined result instead of reopening broad discovery."
            )
        elif stage in {"focus", "change_strategy"}:
            guidance += (
                "Review the user's requested outcome now: briefly state what is established, the single remaining "
                "evidence gap (if any), and the next bounded action that advances the outcome. "
                "Reuse established findings and attached artifact references; avoid restarting broad discovery "
                "or duplicating a helper's assignment. Another read should answer a specific unresolved question. "
                "A covered-range reread may still be necessary after context loss, a change, or a required native "
                "read obligation; do not skip those reads. "
            )
            if read_only:
                guidance += (
                    "This assignment is read-only: return a supported answer/report when its evidence is sufficient, "
                    "or continue only the missing evidence. Do not manufacture file edits to show progress. "
                )
            else:
                guidance += (
                    "If the task requests implementation and the target/constraints are known, make the smallest "
                    "useful authorized change next, then verify it. If the task requests analysis or an answer, "
                    "deliver the supported findings; file edits are not required. If execution is blocked, identify "
                    "the concrete dependency and pursue an available alternative without inventing success. "
                )
            if stage == "change_strategy":
                guidance += (
                    "The investigation has continued across a second checkpoint: change approach or narrow the "
                    "immediate work unit while preserving the requested outcome. Do not just repeat the earlier plan. "
                    "Ask the user only for information or authorization that is actually missing."
                )
        else:
            guidance += "Continue toward the requested outcome and verify relevant results before claiming completion."
        notice = ""
        key = (stage, self.changes)
        if stage in {"focus", "change_strategy"} and key != self._notice_key:
            notice = (f"执行进度检查：连续进行了 {calls} 次工具调用，正在复核下一步如何推进任务。"
                      + ("收敛到下一项可验证的行动。" if stage == "focus" else "需要调整方法或缩小当前工作单元。")
                      + "分析任务以有依据的结论为目标。")
        self._notice_key = key
        return "Execution progress review (advisory; existing guards remain authoritative).\n" + json.dumps(
            facts, ensure_ascii=False, separators=(",", ":")) + "\n" + guidance, notice
