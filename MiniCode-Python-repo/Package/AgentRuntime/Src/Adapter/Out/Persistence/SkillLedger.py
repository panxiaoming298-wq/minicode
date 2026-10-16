"""Additive Skill records in the existing local SQLite authority file.

No global user_version is changed. Old runtimes can leave these versioned
tables intact on rollback; neither audit records nor source bytes are deleted.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from ....Application.Port.Out.SkillStorage import SkillLedger


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class SQLiteSkillLedger(SkillLedger):
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def _connection(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("CREATE TABLE IF NOT EXISTS minicode_skill_state_v1 (owner TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, sha256 TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS minicode_skill_events_v1 (owner TEXT NOT NULL, revision INTEGER NOT NULL, event TEXT NOT NULL, created REAL NOT NULL, state_sha256 TEXT NOT NULL, PRIMARY KEY(owner, revision))")
            yield connection
        finally:
            connection.close()

    def _read(self, connection, owner):
        row = connection.execute("SELECT revision,state,sha256 FROM minicode_skill_state_v1 WHERE owner=?", (owner,)).fetchone()
        if row is None:
            return {"schema_version": 1, "revision": 0, "invocations": []}
        if hashlib.sha256(row[1].encode("utf-8")).hexdigest() != row[2]:
            raise ValueError("Skill state integrity check failed")
        state = json.loads(row[1])
        if state.get("schema_version") != 1 or state.get("revision") != row[0] or not isinstance(state.get("invocations"), list):
            raise ValueError("Unsupported or invalid Skill state")
        return state

    def read(self, owner):
        if not Path(self.path).exists():
            return {"schema_version": 1, "revision": 0, "invocations": []}
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='minicode_skill_state_v1'").fetchone():
                return {"schema_version": 1, "revision": 0, "invocations": []}
            return self._read(connection, owner)
        finally:
            connection.close()

    def change(self, owner, operation, *, event):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                state = self._read(connection, owner)
                before = deepcopy(state)
                result = operation(state)
                if state != before:
                    state["revision"] += 1
                    payload = _json(state)
                    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    connection.execute("INSERT INTO minicode_skill_state_v1 VALUES (?,?,?,?) ON CONFLICT(owner) DO UPDATE SET revision=excluded.revision,state=excluded.state,sha256=excluded.sha256",
                                       (owner, state["revision"], payload, digest))
                    connection.execute("INSERT INTO minicode_skill_events_v1 VALUES (?,?,?,?,?)",
                                       (owner, state["revision"], event, time.time(), digest))
                connection.execute("COMMIT")
                return deepcopy(result)
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def release_run(self, *, workspace, run_id, reason):
        if reason not in {"completed", "canceled", "explicit_switch", "host_ended"}:
            raise ValueError("Skill release requires an explicit host lifecycle reason")
        if not Path(self.path).exists():
            return 0
        with sqlite3.connect(self.path, timeout=10) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='minicode_skill_state_v1'").fetchone():
                return 0
            owners = [row[0] for row in connection.execute("SELECT owner FROM minicode_skill_state_v1")]
        released = 0
        for owner in owners:
            scope = json.loads(owner)
            if scope.get("workspace") != workspace or scope.get("run_id") != run_id:
                continue
            def release(state):
                count = 0
                state["blocked"] = {}
                for item in state["invocations"]:
                    if item["state"] != "RELEASED":
                        item.update(state="RELEASED", release_reason=reason)
                        count += 1
                return count
            released += self.change(owner, release, event="runtime_release:" + reason)
        return released

    def audit(self, *, workspace, invocation_id):
        """Explicit host read, including released sources; never activates."""
        if not Path(self.path).exists():
            raise ValueError("Unknown Skill invocation")
        with sqlite3.connect(self.path, timeout=10) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='minicode_skill_state_v1'").fetchone():
                raise ValueError("Unknown Skill invocation")
            owners = [row[0] for row in connection.execute("SELECT owner FROM minicode_skill_state_v1")]
            for owner in owners:
                if json.loads(owner).get("workspace") != workspace:
                    continue
                for item in self._read(connection, owner)["invocations"]:
                    if item["id"] == invocation_id:
                        return {"kind": "historical_skill_audit", "activates_rules": False, "invocation": deepcopy(item)}
        raise ValueError("Unknown Skill invocation in this workspace")
