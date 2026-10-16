"""Small, expiring project error lessons; no captured prompts or error bodies."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time

from minicode.store_lock import store_lock

LESSON_TTL_SECONDS = 30 * 24 * 3600
LESSONS = {
    "command_timeout": "A command previously exceeded its deadline. Inspect retained output and partial files, "
        "identify the stalled stage, and fix or split the command before explicitly rerunning it. "
        "Browser probes need bounded screenshot waits and finally cleanup. Partial PASS output is not completion.",
    "search_timeout": "Broad code searches previously timed out. Start with a relevant subdirectory or explicit file, "
        "use include filters, and split independent queries. Incomplete searches cannot prove absence.",
    "transient_read": "A read previously met a temporary service failure. Native reads retry with bounded backoff. "
        "After that, change the request or use another authorized source; respect Retry-After.",
    "invalid_input": "Tool arguments previously failed validation. Consult the current schema and correct the named fields; "
        "do not repeat identical invalid arguments or invent paths and evidence IDs.",
    "not_found": "A referenced path previously did not exist. List the relevant directory and check the current tool "
        "working directory before reading; do not assume paths from a different workspace still apply.",
    "verification_failed": "A completed check previously failed. Inspect the actual check output, fix the cause within "
        "scope, and rerun the check after a relevant change. Keep required acceptance checks intact.",
}


def lesson_for(result):
    if result.toolRecovery and result.toolRecovery.get("kind") in {"search_timeout", "transient_read", "command_timeout"}:
        if result.toolRecovery.get("status") in {"agent_action", "recovered"}:
            return result.toolRecovery["kind"]
    if result.ok or result.ambiguousSideEffect or result.timedOut or result.awaitUser or result.waitKind \
            or result.runtimeYield or result.runtimeStopReason or result.backgroundTask:
        return None
    if result.inputValidationFailed:
        return "invalid_input"
    if result.failureKind == "not_found":
        return "not_found"
    if result.verificationFailed:
        return "verification_failed"
    return None


class RecoveryMemory:
    def __init__(self, project_root, *, storage_root=None):
        from minicode.config import MINI_CODE_DIR
        identity = os.path.normcase(str(Path(project_root).resolve()))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.path = Path(storage_root or MINI_CODE_DIR / "recovery-lessons") / (digest + ".json")
        self._observed = set()
        self._recovered = set()
        self.lessons = self._read_safely()

    def _read(self):
        from minicode.path_safety import is_unsafe_stored_path
        if is_unsafe_stored_path(self.path, boundary=Path(self.path.anchor), reject_hardlinks=True):
            raise OSError("unsafe lesson store path")
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > 16384:
            return {}
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("lessons"), dict):
            return {}
        now = time.time()
        return {key: {"lastSeen": item["lastSeen"], "observed": min(item["observed"], 1000000),
                      "recovered": min(item["recovered"], 1000000)}
                for key, item in document["lessons"].items()
                if key in LESSONS and isinstance(item, dict)
                and all(type(item.get(field)) is int and item[field] >= 0 for field in ("observed", "recovered"))
                and type(item.get("lastSeen")) in (int, float)
                and 0 <= now - item["lastSeen"] <= LESSON_TTL_SECONDS}

    def _read_safely(self):
        try:
            return self._read()
        except (OSError, ValueError, TypeError):
            return {}

    def record(self, rule, *, recovered=False, observed=False):
        if rule not in LESSONS:
            return
        observe = observed and rule not in self._observed
        recover = recovered and rule not in self._recovered
        if not observe and not recover:
            return
        try:
            with store_lock(self.path.with_suffix(".lock"), timeout=0.2):
                lessons = self._read()
                item = lessons.setdefault(rule, {"observed": 0, "recovered": 0})
                item.update(lastSeen=time.time(), observed=item["observed"] + int(observe),
                            recovered=item["recovered"] + int(recover))
                self.lessons = lessons
                descriptor, pending = tempfile.mkstemp(prefix="lesson-", suffix=".json", dir=self.path.parent)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        json.dump({"version": 1, "lessons": lessons}, handle, ensure_ascii=True)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(pending, self.path)
                finally:
                    if os.path.exists(pending):
                        os.unlink(pending)
            if observe:
                self._observed.add(rule)
            if recover:
                self._recovered.add(rule)
        except (OSError, ValueError, TypeError):
            # Diagnostics must never turn a recoverable tool error into a stop.
            logging.getLogger(__name__).warning("Could not save advisory project recovery lesson")

    def context(self):
        if not self.lessons:
            return ""
        return ("Advisory lessons from prior tool failures in this project (expire after 30 days). "
                "These are prevention hints, not new user instructions, current evidence, permissions or completion.\n"
                + "\n".join(f"- {LESSONS[key]} (observed activations: {item['observed']}; "
                            f"recoveries: {item['recovered']})" for key, item in sorted(self.lessons.items())))
