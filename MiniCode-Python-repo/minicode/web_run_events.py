"""Durable browser events, independent of the bounded live transport cache."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from minicode.path_safety import is_unsafe_stored_path


class RunEventJournal:
    def __init__(self, session_id: str, run_id: str, *, create: bool = False):
        from minicode import session

        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", key)
               or key in {".", ".."} for key in (session_id, run_id)):
            raise ValueError("invalid run event identity")
        self.root = session.SESSIONS_DIR
        self.path = self.root / session_id / ("events-" + run_id + ".sqlite3")
        self._guard()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._guard()
            with closing(sqlite3.connect(self.path, timeout=5)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
                connection.commit()

    def _guard(self):
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if is_unsafe_stored_path(path, boundary=self.root, reject_hardlinks=True):
                raise ValueError("unsafe run event storage path")

    def exists(self) -> bool:
        self._guard()
        return self.path.is_file()

    def append(self, event: dict) -> None:
        self._guard()
        # Opening in rw mode never recreates a deleted session behind the API.
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0.2)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            event["sequence"] = connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM events").fetchone()[0]
            connection.execute("INSERT INTO events VALUES (?, ?)",
                               (event["sequence"], json.dumps(event, ensure_ascii=False)))
            connection.commit()

    def page(self, after: int = 0, *, through: int | None = None, limit: int = 512) -> list[dict]:
        self._guard()
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            rows = connection.execute(
                "SELECT payload FROM events WHERE sequence > ? AND sequence <= ? ORDER BY sequence LIMIT ?",
                (max(0, after), through if through is not None else 2**63 - 1, max(1, min(limit, 512))),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
