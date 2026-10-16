"""Database-backed rerank leases, cooldowns and scoped score caches.

Only digests and integer grades are persisted in the existing SQLite store.
Fencing tokens prevent a late worker from changing a newer lease's state.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager

from .search_budget import authority_deadline


class SharedRerankCoordinator:
    def __init__(self, authority):
        self.authority = authority


    @contextmanager
    def _transaction(self, deadline=None):
        bounded = min(deadline or float("inf"), time.monotonic() + 0.25)
        with (
            authority_deadline(self.authority, bounded),
            self.authority._transaction() as db,
        ):
            yield db

    def _now(self, db):
        return float(db.execute("SELECT (julianday('now') - 2440587.5) * 86400.0 AS now").fetchone()["now"])

    def acquire(self, key, *, lease_seconds, deadline=None):
        token = uuid.uuid4().hex
        with self._transaction(deadline) as db:
            now = self._now(db)
            row = db.execute(
                (
                    "INSERT INTO memory_rerank_control(key, failures, blocked_until, lease_token, lease_until, updated_at) "
                    "VALUES (?, 0, 0, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET lease_token=excluded.lease_token, "
                    "lease_until=excluded.lease_until, updated_at=excluded.updated_at "
                    "WHERE memory_rerank_control.blocked_until<=? AND memory_rerank_control.lease_until<=? "
                    "RETURNING lease_token"
                ),
                (key, token, now + lease_seconds, now, now, now),
            ).fetchone()
            if row:
                # Retain active cooldowns/leases, prune abandoned credential digests.
                db.execute(
                    (
                        "DELETE FROM memory_rerank_control WHERE updated_at<? AND blocked_until<? AND lease_until<?"
                    ),
                    (now - 86400, now, now),
                )
                return token, ""
            row = db.execute(
                (
                    "SELECT blocked_until FROM memory_rerank_control WHERE key=?"
                ),
                (key,),
            ).fetchone()
            if row and row["blocked_until"] == float("inf"):
                return "", "configuration_disabled"
            return "", "circuit_open" if row and row["blocked_until"] > now else "busy"

    def finish(
        self,
        key,
        token,
        *,
        error=None,
        threshold=2,
        cooldown=60,
        release=True,
        cancel=False,
        deadline=None,
    ):
        with self._transaction(deadline) as db:
            now = self._now(db)
            row = db.execute(
                (
                    "SELECT failures, blocked_until FROM memory_rerank_control WHERE key=? AND lease_token=?"
                ),
                (key, token),
            ).fetchone()
            if row is None:
                return False
            failures = int(row["failures"]) + 1 if error else 0
            blocked = 0.0
            if cancel:
                failures, blocked = int(row["failures"]), float(row["blocked_until"])
            if error:
                permanent = error.kind in {"authentication", "quota", "configuration"}
                if permanent or error.kind == "rate_limit" or failures >= threshold:
                    blocked = now + max(
                        cooldown, error.retry_after, 300 if permanent else 0
                    )
                    if error.kind == "configuration":
                        blocked = float("inf")
            db.execute(
                (
                    "UPDATE memory_rerank_control SET failures=?, blocked_until=?, updated_at=?, "
                    + (
                        "lease_token='', lease_until=0 "
                        if release
                        else "lease_token=lease_token "
                    )
                    + "WHERE key=? AND lease_token=?"
                ),
                (failures, blocked, now, key, token),
            )
            return True

    def get_cached(self, key, *, tenant_id, task_id, deadline=None, include_ttl=False):
        with self._transaction(deadline) as db:
            now = self._now(db)
            row = db.execute(
                (
                    "SELECT grades, expires_at FROM memory_rerank_cache WHERE key=? AND tenant_id=? AND task_id=? AND expires_at>?"
                ),
                (key, tenant_id, task_id, now),
            ).fetchone()
            if row is None:
                return None
            grades = json.loads(row["grades"])
            return (grades, max(0, row["expires_at"] - now)) if include_ttl else grades

    def put_cached(self, key, grades, *, tenant_id, task_id, ttl, deadline=None):
        with self._transaction(deadline) as db:
            now = self._now(db)
            db.execute(
                ("DELETE FROM memory_rerank_cache WHERE expires_at<=?"), (now,)
            )
            db.execute(
                (
                    "INSERT INTO memory_rerank_cache(key,tenant_id,task_id,grades,expires_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET grades=excluded.grades, expires_at=excluded.expires_at"
                ),
                (key, tenant_id, task_id, json.dumps(grades), now + ttl),
            )
            # Bounded shared cache, independent of the lifetime of web workers.
            db.execute(
                "DELETE FROM memory_rerank_cache WHERE key IN "
                "(SELECT key FROM memory_rerank_cache ORDER BY expires_at DESC, key LIMIT -1 OFFSET 4096)"
            )
