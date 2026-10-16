"""SQLite authority adapter used by local MiniCode and deterministic tests.

SQLite preserves atomic event/state invariants for local execution without
requiring a network service.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .lexical import (
    bm25_scores,
    memory_search_document,
    scope_boost,
    sqlite_match_expression,
)
from .models import (
    AgentEvent,
    ArchiveManifest,
    ArchiveStatus,
    ArtifactRecord,
    ArtifactStatus,
    CheckpointRecord,
    DecisionRecord,
    DependencyRecord,
    DependencyType,
    EventType,
    MemoryHit,
    MemoryMode,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryType,
    SourceRange,
    TaskRetentionPolicy,
    TaskStateSnapshot,
    apply_transactional_state_patch,
    content_hash_text,
    new_id,
    now_epoch,
    stable_json,
)
from .search_budget import SearchResults, bounded_search, remaining_seconds

SCHEMA_VERSION = 9
# Written last, in the same transaction as every required schema change.
# Advance this when adding schema objects so existing installations migrate
# before taking the read-only initialization path.
_INITIALIZATION_VERSION = 16


_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS memory_schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_task (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    current_run_id TEXT NOT NULL DEFAULT '',
    current_step_id TEXT NOT NULL DEFAULT '',
    manual_memory_mode TEXT,
    cross_session_required INTEGER NOT NULL DEFAULT 0,
    restart_recovery_required INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS agent_run (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    current_memory_mode TEXT NOT NULL,
    model_name TEXT NOT NULL DEFAULT '',
    model_context_limit INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    completed_at REAL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_step (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    parent_step_id TEXT NOT NULL DEFAULT '',
    sequence_no INTEGER NOT NULL,
    step_type TEXT NOT NULL DEFAULT 'AGENT',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    acceptance_criteria TEXT NOT NULL DEFAULT '{}',
    input_event_id TEXT NOT NULL DEFAULT '',
    output_event_id TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    completed_at REAL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES agent_run(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_event (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    content_text TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '{}',
    artifact_uri TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    caused_by_event_id TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_event_idempotency
    ON agent_event(tenant_id, task_id, idempotency_key) WHERE idempotency_key <> '';
CREATE INDEX IF NOT EXISTS agent_event_task_time
    ON agent_event(tenant_id, task_id, created_at DESC);
-- SQLite appends rowid to this index, allowing stable history keyset pages
-- without sorting all of the task's events for each request.
CREATE INDEX IF NOT EXISTS agent_event_history_order
    ON agent_event(tenant_id, task_id);
CREATE INDEX IF NOT EXISTS agent_event_step
    ON agent_event(tenant_id, task_id, step_id);

CREATE TRIGGER IF NOT EXISTS protect_agent_event_update
BEFORE UPDATE ON agent_event
BEGIN
    SELECT RAISE(ABORT, 'agent_event is append-only');
END;

CREATE TRIGGER IF NOT EXISTS protect_agent_event_delete
BEFORE DELETE ON agent_event
WHEN minicode_allow_hard_delete() = 0
BEGIN
    SELECT RAISE(ABORT, 'agent_event deletion requires an authorized hard-delete transaction');
END;

CREATE TABLE IF NOT EXISTS agent_task_state (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'INITIAL',
    current_step_id TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    active_constraints TEXT NOT NULL DEFAULT '[]',
    active_decisions TEXT NOT NULL DEFAULT '{}',
    open_errors TEXT NOT NULL DEFAULT '[]',
    completed_steps TEXT NOT NULL DEFAULT '[]',
    pending_steps TEXT NOT NULL DEFAULT '[]',
    active_artifacts TEXT NOT NULL DEFAULT '[]',
    blocked_by TEXT NOT NULL DEFAULT '[]',
    handoff_notes TEXT NOT NULL DEFAULT '{}',
    context_rotations TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, task_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_decision (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    decision_key TEXT NOT NULL,
    decision_text TEXT NOT NULL,
    structured_value TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    source_event_id TEXT NOT NULL DEFAULT '',
    supersedes_id TEXT NOT NULL DEFAULT '',
    superseded_by_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_decision_active
    ON agent_decision(tenant_id, task_id, decision_key, status);

CREATE TABLE IF NOT EXISTS agent_dependency (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    dependency_type TEXT NOT NULL,
    dependency_id TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    dependency_depth INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE (tenant_id, task_id, step_id, dependency_type, dependency_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_dependency_step
    ON agent_dependency(tenant_id, task_id, step_id, required);

CREATE TABLE IF NOT EXISTS agent_artifact (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_artifact_task
    ON agent_artifact(tenant_id, task_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_checkpoint (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    current_step_id TEXT NOT NULL DEFAULT '',
    memory_mode TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_checkpoint_latest
    ON agent_checkpoint(tenant_id, task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_memory (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    step_id TEXT NOT NULL DEFAULT '',
    memory_type TEXT NOT NULL,
    title TEXT NOT NULL,
    preview_text TEXT NOT NULL,
    status TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    source_event_id TEXT NOT NULL DEFAULT '',
    artifact_id TEXT NOT NULL DEFAULT '',
    source_start INTEGER NOT NULL DEFAULT 0,
    source_end INTEGER NOT NULL DEFAULT 0,
    source_unit TEXT NOT NULL DEFAULT 'CHAR',
    content_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT '',
    facets TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (
        tenant_id, task_id, memory_type, content_hash, source_event_id, artifact_id,
        source_start, source_end
    ),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_memory_search
    ON agent_memory(tenant_id, task_id, status, memory_type, phase);

CREATE TABLE IF NOT EXISTS memory_working_set (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 1,
    state_version INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, task_id, phase, memory_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_deletion_audit (
    request_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    actor_hash TEXT NOT NULL DEFAULT '',
    reason_hash TEXT NOT NULL DEFAULT '',
    execution_status TEXT NOT NULL DEFAULT 'COMPLETED',
    deleted_counts TEXT NOT NULL,
    requested_at REAL NOT NULL,
    completed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_retention_policy (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    retention_until REAL,
    legal_hold INTEGER NOT NULL DEFAULT 0,
    deletion_mode TEXT NOT NULL DEFAULT 'HARD_DELETE',
    set_by TEXT NOT NULL DEFAULT 'system',
    updated_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, task_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_archive_manifest (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    uri TEXT NOT NULL,
    archive_type TEXT NOT NULL,
    codec TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    uncompressed_size_bytes INTEGER NOT NULL,
    compressed_size_bytes INTEGER NOT NULL,
    first_event_id TEXT NOT NULL DEFAULT '',
    last_event_id TEXT NOT NULL DEFAULT '',
    first_created_at REAL NOT NULL DEFAULT 0,
    last_created_at REAL NOT NULL DEFAULT 0,
    rule_name TEXT NOT NULL DEFAULT '',
    backup_class TEXT NOT NULL DEFAULT 'STANDARD',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    metadata TEXT NOT NULL DEFAULT '{}',
    last_verified_at REAL,
    restored_at REAL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (tenant_id, task_id, artifact_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (artifact_id) REFERENCES agent_artifact(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_archive_manifest_task
    ON memory_archive_manifest(tenant_id, task_id, status, last_created_at);
"""


_AGENT_MEMORY_V4 = r"""
CREATE TABLE agent_memory_v4 (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    step_id TEXT NOT NULL DEFAULT '',
    memory_type TEXT NOT NULL,
    title TEXT NOT NULL,
    preview_text TEXT NOT NULL,
    status TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    source_event_id TEXT NOT NULL DEFAULT '',
    artifact_id TEXT NOT NULL DEFAULT '',
    source_start INTEGER NOT NULL DEFAULT 0,
    source_end INTEGER NOT NULL DEFAULT 0,
    source_unit TEXT NOT NULL DEFAULT 'CHAR',
    content_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT '',
    facets TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (
        tenant_id, task_id, memory_type, content_hash, source_event_id, artifact_id,
        source_start, source_end
    ),
    FOREIGN KEY (tenant_id, task_id)
        REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
)
"""


class StateVersionConflict(RuntimeError):
    pass


class SqliteMemoryStore:
    """Relational authority with atomic event/state/artifact bundles."""

    def __init__(self, path: str | Path, *, lock_timeout: float = 3.0) -> None:
        if not 0 <= lock_timeout <= 30:
            raise ValueError("lock_timeout must be between 0 and 30 seconds")
        self._lock_timeout = lock_timeout
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            timeout=min(0.1, lock_timeout),
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._savepoint_counter = 0
        self._query_deadline = None
        self._hard_delete_allowed = False
        self._fts_available = False
        self._connection.create_function(
            "minicode_allow_hard_delete",
            0,
            lambda: 1 if self._hard_delete_allowed else 0,
        )
        try:
            self.initialize()
        except BaseException:
            self._connection.close()
            raise

    @contextmanager
    def query_deadline(self, deadline):
        if deadline is None:
            yield
            return
        # The connection and progress handler are shared; keep the same lock
        # used by all ordinary reads/writes for the duration of the scope.
        if not self._lock.acquire(timeout=remaining_seconds(deadline)):
            raise TimeoutError("memory query deadline exceeded waiting for connection")
        try:
            previous = self._query_deadline
            effective = min(
                value
                for value in (previous, deadline, float("inf"))
                if value is not None
            )
            remaining = remaining_seconds(effective)
            previous_busy = self._connection.execute("PRAGMA busy_timeout").fetchone()[
                0
            ]
            self._query_deadline = effective
            self._connection.execute(
                f"PRAGMA busy_timeout={min(previous_busy, max(0, int(remaining * 1000)))}"
            )
            self._connection.set_progress_handler(
                lambda: time.monotonic() >= effective, 500
            )
            try:
                yield
            except sqlite3.OperationalError as exc:
                if (getattr(exc, "sqlite_errorcode", 0) & 0xFF) in {
                    sqlite3.SQLITE_INTERRUPT,
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                } or time.monotonic() >= effective:
                    raise TimeoutError("memory query deadline exceeded") from exc
                raise
            finally:
                self._query_deadline = previous
                self._connection.set_progress_handler(
                    (lambda: time.monotonic() >= previous)
                    if previous is not None
                    else None,
                    500,
                )
                self._connection.execute(f"PRAGMA busy_timeout={previous_busy}")
        finally:
            self._lock.release()

    def initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            if self._connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                previous_busy = self._connection.execute("PRAGMA busy_timeout").fetchone()[0]
                try:
                    self._connection.execute(f"PRAGMA busy_timeout = {int(self._lock_timeout * 1000)}")
                    self._connection.execute("PRAGMA journal_mode = WAL")
                finally:
                    self._connection.execute(f"PRAGMA busy_timeout = {previous_busy}")
            # Browsing history opens independent connections to this authority.
            # A ready database needs no write lock, even while an Agent writes.
            if self._schema_ready():
                return
            with self._transaction():
                self._initialize_schema()

    def _schema_ready(self) -> bool:
        tables = {row[0] for row in self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "memory_schema_version" not in tables:
            return False
        versions = {row[0] for row in self._connection.execute("SELECT version FROM memory_schema_version")}
        if not {SCHEMA_VERSION, 10, 11, 13, 14, 15, _INITIALIZATION_VERSION}.issubset(versions):
            return False
        for table, required in (
            ("agent_task_state", {"handoff_notes", "context_rotations"}),
            ("memory_deletion_audit", {"actor_hash", "reason_hash", "execution_status"}),
        ):
            columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
            if not required.issubset(columns):
                return False
        if not self._connection.execute("SELECT 1 FROM pragma_module_list WHERE name='fts5'").fetchone():
            self._fts_available = False
            return True
        if not {"agent_memory_fts", "memory_fts_state"}.issubset(tables):
            return False
        version = self._connection.execute("SELECT version FROM memory_fts_state WHERE id=1").fetchone()
        self._fts_available = bool(version and version[0] == 1)
        return self._fts_available

    def _execute_schema_script(self, script: str) -> None:
        # executescript() implicitly commits the caller's transaction. Execute
        # complete SQL statements instead, preserving triggers and atomic DDL.
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                self._connection.execute(statement)
                statement = ""
        if statement.strip():
            self._connection.execute(statement)

    def _initialize_schema(self) -> None:
        self._execute_schema_script(_SCHEMA)
        self._migrate_handoff_notes()
        self._migrate_context_rotations()
        self._execute_schema_script(
            (Path(__file__).with_name("sql") / "002_retrieval_control.sql").read_text(encoding="utf-8")
        )
        self._migrate_event_idempotency_scope()
        self._migrate_memory_source_identity()
        self._execute_schema_script(
            (Path(__file__).with_name("sql") / "003_memory_lifecycle.sql").read_text(encoding="utf-8")
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO memory_schema_version(version, applied_at) VALUES (11, ?)",
            (now_epoch(),),
        )
        self._initialize_task_write_fences()
        self._initialize_memory_fts()
        audit_columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(memory_deletion_audit)").fetchall()
        }
        for column, definition in (
            ("actor_hash", "TEXT NOT NULL DEFAULT ''"),
            ("reason_hash", "TEXT NOT NULL DEFAULT ''"),
            ("execution_status", "TEXT NOT NULL DEFAULT 'COMPLETED'"),
        ):
            if column not in audit_columns:
                self._connection.execute(f"ALTER TABLE memory_deletion_audit ADD COLUMN {column} {definition}")
        self._connection.execute(
            "INSERT OR IGNORE INTO memory_schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now_epoch()),
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO memory_schema_version(version, applied_at) VALUES (10, ?)",
            (now_epoch(),),
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO memory_schema_version(version, applied_at) VALUES (?, ?)",
            (_INITIALIZATION_VERSION, now_epoch()),
        )

    def _migrate_handoff_notes(self) -> None:
        # Serialize the existence check and additive migration across processes.
        with self._transaction() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(agent_task_state)")}
            if "handoff_notes" not in columns:
                db.execute("ALTER TABLE agent_task_state ADD COLUMN handoff_notes TEXT NOT NULL DEFAULT '{}'")
            db.execute(
                "INSERT OR IGNORE INTO memory_schema_version(version, applied_at) VALUES (13, ?)",
                (now_epoch(),),
            )

    def _migrate_context_rotations(self) -> None:
        with self._transaction() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(agent_task_state)")}
            if "context_rotations" not in columns:
                db.execute("ALTER TABLE agent_task_state ADD COLUMN context_rotations TEXT NOT NULL DEFAULT '{}'")
            db.execute(
                "INSERT OR IGNORE INTO memory_schema_version(version, applied_at) VALUES (14, ?)",
                (now_epoch(),),
            )

    def _migrate_event_idempotency_scope(self) -> None:
        """Make event idempotency local to one tenant/task boundary."""
        columns = [
            row["name"]
            for row in self._connection.execute(
                "PRAGMA index_info('agent_event_idempotency')"
            ).fetchall()
        ]
        if columns == ["tenant_id", "task_id", "idempotency_key"]:
            return
        with self._transaction() as db:
            db.execute("DROP INDEX IF EXISTS agent_event_idempotency")
            db.execute(
                "CREATE UNIQUE INDEX agent_event_idempotency "
                "ON agent_event(tenant_id,task_id,idempotency_key) "
                "WHERE idempotency_key<>''"
            )


    def _initialize_task_write_fences(self) -> None:
        """Reject task-scoped inserts/updates once real deletion begins."""
        self._execute_schema_script(
            (Path(__file__).with_name("sql") / "004_history_journal.sql").read_text(encoding="utf-8"))
        self._connection.execute(
            "INSERT OR IGNORE INTO memory_schema_version(version, applied_at) VALUES (15, ?)",
            (now_epoch(),),
        )
        tables = (
            "conversation_history_head", "conversation_history_event", "conversation_history_migration",
            "agent_run",
            "agent_step",
            "agent_event",
            "agent_task_state",
            "agent_decision",
            "agent_dependency",
            "agent_artifact",
            "agent_checkpoint",
            "agent_memory",
            "memory_working_set",
            "memory_retention_policy",
            "memory_archive_manifest",
        )
        for table in tables:
            for operation in ("INSERT", "UPDATE"):
                trigger = f"fence_{table}_{operation.casefold()}"
                self._connection.execute(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger}
                    BEFORE {operation} ON {table}
                    WHEN COALESCE((
                        SELECT status FROM agent_task
                        WHERE tenant_id=NEW.tenant_id AND id=NEW.task_id
                    ), '') <> 'ACTIVE'
                    BEGIN
                        SELECT RAISE(ABORT, 'task is not ACTIVE');
                    END
                    """
                )

    def _initialize_memory_fts(self) -> None:
        """Initialize the projection once; normal writes maintain it incrementally.

        History reads open short-lived connections. Rebuilding here on every
        connection serializes all conversation reads behind a full-memory scan.
        The version and backfill commit together so another opener cannot see
        an incomplete index or mistake an interrupted migration for success.
        """

        try:
            with self._transaction() as db:
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_memory_fts'"
                ).fetchone()
                db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(
                    memory_id UNINDEXED,
                    search_document,
                    tokenize='unicode61 remove_diacritics 2'
                )""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS agent_memory_fts_delete
                AFTER DELETE ON agent_memory
                BEGIN
                    DELETE FROM agent_memory_fts WHERE memory_id=OLD.id;
                END""")
                db.execute("""CREATE TABLE IF NOT EXISTS memory_fts_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL
                )""")
                version = db.execute(
                    "SELECT version FROM memory_fts_state WHERE id=1"
                ).fetchone()
                if not exists or version is None or version["version"] != 1:
                    rows = db.execute(
                        "SELECT id,title,preview_text,facets FROM agent_memory"
                    ).fetchall()
                    db.execute("DELETE FROM agent_memory_fts")
                    db.executemany(
                        "INSERT INTO agent_memory_fts(memory_id,search_document) VALUES (?,?)",
                        (
                            (
                                row["id"],
                                memory_search_document(
                                    title=row["title"],
                                    preview_text=row["preview_text"],
                                    facets=json.loads(row["facets"] or "[]"),
                                ),
                            )
                            for row in rows
                        ),
                    )
                    db.execute(
                        "INSERT INTO memory_fts_state(id,version) VALUES (1,1) "
                        "ON CONFLICT(id) DO UPDATE SET version=excluded.version"
                    )
            self._fts_available = True
        except sqlite3.OperationalError as error:
            # Some embedded Python distributions omit FTS5. Search remains
            # correct through the unbounded in-process BM25 fallback.
            if "no such module: fts5" not in str(error).lower():
                raise
            self._fts_available = False

    def _migrate_memory_source_identity(self) -> None:
        """Keep source-linked chunks distinct without dropping old rows."""
        unique_indexes = self._connection.execute(
            "PRAGMA index_list(agent_memory)"
        ).fetchall()
        target_identity = [
            "tenant_id",
            "task_id",
            "memory_type",
            "content_hash",
            "source_event_id",
            "artifact_id",
            "source_start",
            "source_end",
        ]
        for index in unique_indexes:
            if not index["unique"]:
                continue
            columns = [
                row["name"]
                for row in self._connection.execute(
                    f"PRAGMA index_info('{index['name']}')"
                ).fetchall()
            ]
            if columns == target_identity:
                return

        column_sql = (
            "id,tenant_id,task_id,run_id,step_id,memory_type,title,preview_text,"
            "status,importance,source_event_id,artifact_id,source_start,source_end,"
            "source_unit,content_hash,token_count,phase,facets,created_at,updated_at"
        )
        with self._transaction() as db:
            db.execute("DROP TABLE IF EXISTS agent_memory_v4")
            db.execute(_AGENT_MEMORY_V4)
            db.execute(
                f"INSERT INTO agent_memory_v4({column_sql}) "
                f"SELECT {column_sql} FROM agent_memory"
            )
            db.execute("DROP TABLE agent_memory")
            db.execute("ALTER TABLE agent_memory_v4 RENAME TO agent_memory")
            db.execute(
                "CREATE INDEX agent_memory_search "
                "ON agent_memory(tenant_id, task_id, status, memory_type, phase)"
            )

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Keep a consistent WAL snapshot without reserving the single writer."""
        with self._lock:
            if self._connection.in_transaction:
                yield self._connection
                return
            previous = self._connection.execute("PRAGMA query_only").fetchone()[0]
            self._connection.execute("PRAGMA query_only = ON")
            try:
                self._connection.execute("BEGIN")
                try:
                    yield self._connection
                finally:
                    self._connection.rollback()
            finally:
                self._connection.execute(f"PRAGMA query_only = {previous}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._query_deadline is not None:
                remaining_seconds(self._query_deadline)
            if self._connection.in_transaction:
                self._savepoint_counter += 1
                name = f"memory_{self._savepoint_counter}"
                self._connection.execute(f"SAVEPOINT {name}")
                try:
                    yield self._connection
                except BaseException:
                    self._connection.execute(f"ROLLBACK TO {name}")
                    raise
                finally:
                    self._connection.execute(f"RELEASE {name}")
                return
            # Retry admission only: no transaction body or external operation
            # is replayed. A short competing writer need not fail the Agent.
            deadline = time.monotonic() + self._lock_timeout
            if self._query_deadline is not None:
                deadline = min(deadline, self._query_deadline)
            previous_busy = self._connection.execute("PRAGMA busy_timeout").fetchone()[0]
            try:
                while True:
                    if self._query_deadline is not None:
                        remaining_seconds(self._query_deadline)
                    remaining = max(0, deadline - time.monotonic())
                    self._connection.execute(
                        f"PRAGMA busy_timeout={min(previous_busy, int(remaining * 1000))}"
                    )
                    try:
                        self._connection.execute("BEGIN IMMEDIATE")
                        break
                    except sqlite3.OperationalError as error:
                        code = getattr(error, "sqlite_errorcode", 0) & 0xff
                        remaining = deadline - time.monotonic()
                        if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or remaining <= 0:
                            raise
                        time.sleep(min(0.05, remaining))
            finally:
                self._connection.execute(f"PRAGMA busy_timeout={previous_busy}")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                try:
                    self._connection.commit()
                except BaseException:
                    self._connection.rollback()
                    raise

    def maintain_memories(self, *, tenant_id: str, task_id: str, now: float) -> dict[str, Any]:
        from .maintenance import maintain_records
        with self._transaction() as db:
            return maintain_records(db, placeholder="?", tenant_id=tenant_id, task_id=task_id, now=now)

    def touch_memories(self, *, tenant_id: str, task_id: str, memory_ids: Sequence[str], now: float) -> None:
        from .maintenance import touch_records
        with self._transaction() as db:
            touch_records(db, placeholder="?", tenant_id=tenant_id, task_id=task_id, memory_ids=memory_ids, now=now)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def health_check(self) -> bool:
        try:
            with self._lock:
                row = self._connection.execute("SELECT 1 AS ok").fetchone()
            return bool(row and row["ok"] == 1)
        except sqlite3.Error:
            return False

    def backup_to(self, target: str | Path) -> Path:
        """Create a transactionally consistent SQLite backup."""
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            backup = sqlite3.connect(destination)
            try:
                self._connection.backup(backup)
                row = backup.execute("PRAGMA quick_check").fetchone()
                if not row or row[0] != "ok":
                    raise sqlite3.DatabaseError("SQLite backup quick_check failed")
                backup.commit()
            finally:
                backup.close()
        return destination

    def ensure_task(
        self,
        *,
        tenant_id: str,
        task_id: str,
        goal: str,
        workspace_id: str = "",
        user_id: str = "",
        cross_session_required: bool = False,
        restart_recovery_required: bool = False,
    ) -> None:
        now = now_epoch()
        with self._transaction() as db:
            cursor = db.execute(
                """
                INSERT INTO agent_task(
                    tenant_id,id,workspace_id,user_id,goal,cross_session_required,
                    restart_recovery_required,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,id) DO UPDATE SET
                    goal=CASE WHEN excluded.goal<>'' THEN excluded.goal ELSE agent_task.goal END,
                    workspace_id=CASE WHEN excluded.workspace_id<>'' THEN excluded.workspace_id ELSE agent_task.workspace_id END,
                    user_id=CASE WHEN excluded.user_id<>'' THEN excluded.user_id ELSE agent_task.user_id END,
                    cross_session_required=MAX(agent_task.cross_session_required,excluded.cross_session_required),
                    restart_recovery_required=MAX(agent_task.restart_recovery_required,excluded.restart_recovery_required),
                    updated_at=excluded.updated_at
                WHERE agent_task.status='ACTIVE'
                """,
                (
                    tenant_id,
                    task_id,
                    workspace_id,
                    user_id,
                    goal,
                    int(cross_session_required),
                    int(restart_recovery_required),
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("task is not ACTIVE")
            db.execute(
                """
                INSERT OR IGNORE INTO agent_task_state(
                    tenant_id,task_id,goal,updated_at
                ) VALUES (?,?,?,?)
                """,
                (tenant_id, task_id, goal, now),
            )

    def create_run(
        self,
        *,
        tenant_id: str,
        task_id: str,
        run_id: str,
        mode: str,
        model_name: str = "",
        model_context_limit: int = 0,
    ) -> None:
        now = now_epoch()
        with self._transaction() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO agent_run(
                    tenant_id,id,task_id,current_memory_mode,model_name,
                    model_context_limit,started_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    tenant_id,
                    run_id,
                    task_id,
                    mode,
                    model_name,
                    model_context_limit,
                    now,
                ),
            )
            db.execute(
                "UPDATE agent_task SET current_run_id=?,updated_at=? WHERE tenant_id=? AND id=?",
                (run_id, now, tenant_id, task_id),
            )

    def task_status(self, *, tenant_id: str, task_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM agent_task WHERE tenant_id=? AND id=?",
                (tenant_id, task_id),
            ).fetchone()
        return str(row["status"]) if row else None

    def begin_task_delete(self, *, tenant_id: str, task_id: str) -> str | None:
        """Fence asynchronous consumers before external objects are removed."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT status FROM agent_task WHERE tenant_id=? AND id=?",
                (tenant_id, task_id),
            ).fetchone()
            if row is None or str(row["status"]) == "DELETING":
                return None
            previous = str(row["status"])
            db.execute(
                "UPDATE agent_task SET status='DELETING',updated_at=? "
                "WHERE tenant_id=? AND id=?",
                (now_epoch(), tenant_id, task_id),
            )
            return previous

    def restore_task_status(self, *, tenant_id: str, task_id: str, status: str) -> bool:
        with self._transaction() as db:
            cursor = db.execute(
                "UPDATE agent_task SET status=?,updated_at=? "
                "WHERE tenant_id=? AND id=? AND status='DELETING'",
                (status, now_epoch(), tenant_id, task_id),
            )
            return cursor.rowcount == 1

    def update_run_mode(
        self, *, tenant_id: str, task_id: str, run_id: str, mode: str
    ) -> None:
        with self._transaction() as db:
            cursor = db.execute(
                "UPDATE agent_run SET current_memory_mode=? "
                "WHERE tenant_id=? AND task_id=? AND id=?",
                (mode, tenant_id, task_id, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"run not found in task scope: {run_id}")

    def upsert_step(
        self,
        *,
        tenant_id: str,
        task_id: str,
        run_id: str,
        step_id: str,
        sequence_no: int,
        title: str = "",
        description: str = "",
        status: str = "RUNNING",
        acceptance_criteria: Mapping[str, Any] | None = None,
        parent_step_id: str = "",
    ) -> None:
        now = now_epoch()
        completed_at = now if status in {"COMPLETED", "FAILED", "CANCELLED"} else None
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO agent_step(
                    tenant_id,id,run_id,task_id,parent_step_id,sequence_no,title,
                    description,status,acceptance_criteria,started_at,completed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,id) DO UPDATE SET
                    title=excluded.title,description=excluded.description,status=excluded.status,
                    acceptance_criteria=excluded.acceptance_criteria,
                    completed_at=COALESCE(excluded.completed_at,agent_step.completed_at)
                """,
                (
                    tenant_id,
                    step_id,
                    run_id,
                    task_id,
                    parent_step_id,
                    sequence_no,
                    title,
                    description,
                    status,
                    stable_json(acceptance_criteria or {}),
                    now,
                    completed_at,
                ),
            )
            db.execute(
                "UPDATE agent_task SET current_step_id=?,updated_at=? WHERE tenant_id=? AND id=?",
                (step_id, now, tenant_id, task_id),
            )

    def get_step(
        self, *, tenant_id: str, task_id: str, step_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_step WHERE tenant_id=? AND task_id=? AND id=?",
                (tenant_id, task_id, step_id),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["acceptance_criteria"] = json.loads(
            value.get("acceptance_criteria") or "{}"
        )
        return value

    def sync_history(self, *, tenant_id, task_id, stream, messages):
        """Append a verified batch and its head in one durable transaction."""
        from .history_journal import genesis, reconcile, verify_records, HistoryIntegrityError
        scope = (tenant_id, task_id, stream)
        with self._transaction() as db:
            rows = db.execute("SELECT sequence,event_id,payload_hash,chain_hash,message FROM conversation_history_event "
                              "WHERE tenant_id=? AND task_id=? AND stream=? ORDER BY sequence", scope).fetchall()
            records = []
            for record_row in rows:
                try:
                    message = json.loads(record_row["message"])
                    if not isinstance(message, dict):
                        raise ValueError("history event must be an object")
                except (TypeError, ValueError) as error:
                    raise HistoryIntegrityError("stored_event_unreadable", sequence=record_row["sequence"],
                                                event_id=record_row["event_id"]) from error
                records.append({**dict(record_row), "message": message})
            row = db.execute("SELECT event_count,chain_hash FROM conversation_history_head "
                             "WHERE tenant_id=? AND task_id=? AND stream=?", scope).fetchone()
            if row is None and records:
                raise HistoryIntegrityError("stored_head_missing")
            head = {"count": row["event_count"], "hash": row["chain_hash"]} if row else {"count": 0, "hash": genesis(stream)}
            verify_records(records, stream, head)
            additions, observed = reconcile(records, messages, stream)
            for record in additions:
                db.execute("INSERT INTO conversation_history_event "
                           "(tenant_id,task_id,stream,sequence,event_id,payload_hash,chain_hash,message) VALUES (?,?,?,?,?,?,?,?)",
                           (*scope, record["sequence"], record["event_id"], record["payload_hash"], record["chain_hash"], stable_json(record["message"])))
            records += additions
            if row is None or additions:
                db.execute("INSERT INTO conversation_history_head (tenant_id,task_id,stream,event_count,chain_hash) VALUES (?,?,?,?,?) "
                           "ON CONFLICT(tenant_id,task_id,stream) DO UPDATE SET event_count=excluded.event_count,chain_hash=excluded.chain_hash",
                           (*scope, len(records), records[-1]["chain_hash"] if records else genesis(stream)))
        return records, observed

    def history_migration(self, *, tenant_id, task_id, stream, key, proof=None):
        scope = (tenant_id, task_id, stream, key)
        with self._transaction() as db:
            row = db.execute("SELECT proof FROM conversation_history_migration "
                             "WHERE tenant_id=? AND task_id=? AND stream=? AND boundary_key=?", scope).fetchone()
            if row is not None:
                return json.loads(row["proof"])
            if proof is not None:
                db.execute("INSERT INTO conversation_history_migration (tenant_id,task_id,stream,boundary_key,proof) VALUES (?,?,?,?,?)",
                           (*scope, stable_json(proof)))
            return proof

    def persist_event(
        self,
        event: AgentEvent,
        *,
        state_patch: Mapping[str, Any] | None = None,
        artifact: ArtifactRecord | None = None,
        memory: MemoryRecord | None = None,
        memories: Sequence[MemoryRecord] = (),
        decision: DecisionRecord | None = None,
        checkpoint: CheckpointRecord | None = None,
        step_status: str = "",
    ) -> tuple[AgentEvent, TaskStateSnapshot | None]:
        bundled = (artifact, memory, decision, checkpoint, *memories)
        if any(
            value is not None
            and (value.tenant_id != event.tenant_id or value.task_id != event.task_id)
            for value in bundled
        ):
            raise ValueError("event bundle must share one tenant/task scope")
        with self._transaction() as db:
            existing = None
            for name in ("_context_rotation_update", "_handoff_note_update"):
                anchor = (state_patch or {}).get(name, {}).get("history_anchor")
                if anchor is not None:
                    from .history_journal import HISTORY_BASIS, verify_boundary
                    if anchor.get("history_basis") == HISTORY_BASIS:
                        records, _ = self.sync_history(tenant_id=event.tenant_id, task_id=event.task_id,
                            stream=anchor["history_stream"], messages=[])
                        verify_boundary([r["message"] for r in records], anchor)
            if event.idempotency_key:
                existing = db.execute(
                    "SELECT * FROM agent_event "
                    "WHERE tenant_id=? AND task_id=? AND idempotency_key=?",
                    (event.tenant_id, event.task_id, event.idempotency_key),
                ).fetchone()
            if existing is not None:
                state_row = db.execute(
                    "SELECT * FROM agent_task_state WHERE tenant_id=? AND task_id=?",
                    (event.tenant_id, event.task_id),
                ).fetchone()
                return self._event(existing), self._state(
                    state_row
                ) if state_row else None

            db.execute(
                """
                INSERT INTO agent_event(
                    id,tenant_id,task_id,run_id,step_id,event_type,content_text,
                    content_json,artifact_uri,content_hash,token_count,
                    caused_by_event_id,correlation_id,idempotency_key,schema_version,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.id,
                    event.tenant_id,
                    event.task_id,
                    event.run_id,
                    event.step_id,
                    event.event_type.value,
                    event.content_text,
                    stable_json(event.content_json),
                    event.artifact_uri,
                    event.content_hash,
                    event.token_count,
                    event.caused_by_event_id,
                    event.correlation_id,
                    event.idempotency_key,
                    event.schema_version,
                    event.created_at,
                ),
            )
            if artifact is not None:
                self._put_artifact(db, artifact)
            if memory is not None:
                memory = self._put_memory(db, memory)
            for extra_memory in memories:
                self._put_memory(db, extra_memory)
            if decision is not None:
                self._put_decision(
                    db,
                    decision,
                    project_state=state_patch is None,
                )
            state = None
            if state_patch is not None:
                row = db.execute(
                    "SELECT * FROM agent_task_state WHERE tenant_id=? AND task_id=?",
                    (event.tenant_id, event.task_id),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO agent_task_state(tenant_id,task_id,updated_at) VALUES (?,?,?)",
                        (event.tenant_id, event.task_id, now_epoch()),
                    )
                    row = db.execute(
                        "SELECT * FROM agent_task_state WHERE tenant_id=? AND task_id=?",
                        (event.tenant_id, event.task_id),
                    ).fetchone()
                current = self._state(row)
                state = apply_transactional_state_patch(current, state_patch)
                self._write_state(db, state, expected_version=current.version)
            if checkpoint is not None:
                checkpoint_state = state
                if checkpoint_state is None:
                    checkpoint_row = db.execute(
                        "SELECT * FROM agent_task_state WHERE tenant_id=? AND task_id=?",
                        (event.tenant_id, event.task_id),
                    ).fetchone()
                    checkpoint_state = (
                        self._state(checkpoint_row)
                        if checkpoint_row is not None
                        else None
                    )
                if checkpoint_state is None:
                    raise StateVersionConflict("checkpoint state is unavailable")
                if checkpoint.state_version != checkpoint_state.version:
                    if state_patch is None:
                        raise StateVersionConflict(
                            "checkpoint state version conflict: "
                            f"expected {checkpoint.state_version}, got {checkpoint_state.version}"
                        )
                    # A sibling Worker may have committed between the caller's
                    # projection and this transaction.  The state patch above
                    # was merged while holding the authority lock, so seal the
                    # checkpoint over that exact committed view instead of
                    # rolling back a valid sibling-safe merge.
                    checkpoint.state_version = checkpoint_state.version
                    checkpoint.snapshot = {
                        **checkpoint.snapshot,
                        "task_state": checkpoint_state.to_dict(),
                    }
                    metrics = checkpoint.snapshot.get("metrics")
                    if isinstance(metrics, dict):
                        metrics["completed_steps"] = len(
                            checkpoint_state.completed_steps
                        )
                    checkpoint.content_hash = content_hash_text(
                        stable_json(checkpoint.snapshot)
                    )
                self._put_checkpoint(db, checkpoint)
            if step_status:
                if step_status not in {"RUNNING", "COMPLETED", "FAILED"}:
                    raise ValueError("invalid atomic step status")
                if not event.step_id:
                    raise ValueError("atomic step status requires event.step_id")
                completed_at = (
                    now_epoch() if step_status in {"COMPLETED", "FAILED"} else None
                )
                cursor = db.execute(
                    "UPDATE agent_step SET status=?,completed_at=? "
                    "WHERE tenant_id=? AND task_id=? AND id=?",
                    (
                        step_status,
                        completed_at,
                        event.tenant_id,
                        event.task_id,
                        event.step_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("atomic step update target is missing")
            return event, state

    def get_event(
        self, *, tenant_id: str, task_id: str, event_id: str
    ) -> AgentEvent | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_event WHERE tenant_id=? AND task_id=? AND id=?",
                (tenant_id, task_id, event_id),
            ).fetchone()
        return self._event(row) if row else None

    def history_event_page(self, *, tenant_id, task_id, snapshot=None, position=None,
                           recent_first=True, role="", tool_name="", run_id="", limit=100):
        """Browse original conversation events in persisted order, without memory status filters."""
        from .models import MEMORY_NAVIGATION_TOOL_NAMES
        role_sql = ("CASE event_type WHEN 'USER_INPUT' THEN 'user' "
                    "WHEN 'MODEL_OUTPUT' THEN COALESCE(json_extract(content_json,'$.role'),'assistant') "
                    "WHEN 'TOOL_CALL' THEN 'assistant_tool_call' ELSE 'tool_result' END")
        where = ["tenant_id=?", "task_id=?", "event_type IN ('USER_INPUT','MODEL_OUTPUT','TOOL_CALL','TOOL_RESULT','TOOL_ERROR')"]
        where.append(role_sql + " IN ('user','assistant','assistant_progress','assistant_tool_call','tool_result')")
        params = [tenant_id, task_id]
        where.append("COALESCE(json_extract(content_json,'$.tool_name'),'') NOT IN ("
                     + ",".join("?" for _ in MEMORY_NAVIGATION_TOOL_NAMES) + ")")
        params.extend(MEMORY_NAVIGATION_TOOL_NAMES)
        for expression, value in ((role_sql, role), ("json_extract(content_json,'$.tool_name')", tool_name),
                                  ("run_id", run_id)):
            if value:
                where.append(expression + "=?")
                params.append(value)
        with self._lock:
            if snapshot is None:
                snapshot = self._connection.execute(
                    "SELECT COALESCE(MAX(rowid),0) FROM agent_event WHERE tenant_id=? AND task_id=?",
                    (tenant_id, task_id)).fetchone()[0]
            where.append("rowid<=?")
            params.append(snapshot)
            if position is not None:
                where.append("rowid<?" if recent_first else "rowid>?")
                params.append(position)
            direction = "DESC" if recent_first else "ASC"
            rows = self._connection.execute(
                f"SELECT rowid AS history_sequence,* FROM agent_event WHERE {' AND '.join(where)} "
                f"ORDER BY rowid {direction} LIMIT ?", (*params, limit + 1)).fetchall()
        return snapshot, [(row["history_sequence"], self._event(row)) for row in rows[:limit]], len(rows) > limit

    def get_event_by_idempotency(
        self, *, tenant_id: str, task_id: str, idempotency_key: str
    ) -> AgentEvent | None:
        if not idempotency_key:
            return None
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM agent_event
                WHERE tenant_id=? AND task_id=? AND idempotency_key=?
                """,
                (tenant_id, task_id, idempotency_key),
            ).fetchone()
        return self._event(row) if row else None

    def tool_result_events(
        self, *, tenant_id: str, task_id: str, call_ids: Sequence[str]
    ) -> list[AgentEvent]:
        """Find scoped receipts without limiting a parallel batch to a tail count."""
        identifiers = sorted(set(call_ids))
        result = []
        with self._lock:
            for offset in range(0, len(identifiers), 128):
                batch = identifiers[offset:offset + 128]
                placeholders = ",".join("?" for _ in batch)
                rows = self._connection.execute(
                    "SELECT * FROM agent_event WHERE tenant_id=? AND task_id=? "
                    "AND event_type IN ('TOOL_RESULT','TOOL_ERROR') "
                    f"AND correlation_id IN ({placeholders}) ORDER BY created_at DESC,id DESC",
                    [tenant_id, task_id, *batch],
                ).fetchall()
                result.extend(self._event(row) for row in rows)
        return result

    def code_focus_events(
        self, *, tenant_id: str, task_id: str, owner: str, workspace: str, limit: int = 256,
    ) -> list[AgentEvent]:
        """Bound the source receipts, not unrelated logs or other Workers' events."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM agent_event WHERE tenant_id=? AND task_id=? AND event_type=? "
                "AND json_extract(content_json,'$.code_focus.owner')=? "
                "AND json_extract(content_json,'$.code_focus.workspace')=? "
                "ORDER BY created_at DESC,id DESC LIMIT ?",
                (tenant_id, task_id, EventType.TOOL_RESULT.value, owner, workspace, max(1, min(limit, 256))),
            ).fetchall()
        return [self._event(row) for row in reversed(rows)]

    def recent_events(
        self,
        *,
        tenant_id: str,
        task_id: str,
        limit: int = 100,
        event_types: Sequence[str] = (),
    ) -> list[AgentEvent]:
        params: list[Any] = [tenant_id, task_id]
        where = "tenant_id=? AND task_id=?"
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            where += f" AND event_type IN ({placeholders})"
            params.extend(event_types)
        params.append(max(1, limit))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_event WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._event(row) for row in reversed(rows)]

    def events_in_range(
        self,
        *,
        tenant_id: str,
        task_id: str,
        after: float | None = None,
        before: float | None = None,
        limit: int = 100_000,
        event_types: Sequence[str] = (),
        exclude_event_types: Sequence[str] = (),
    ) -> list[AgentEvent]:
        params: list[Any] = [tenant_id, task_id]
        where = ["tenant_id=?", "task_id=?"]
        if after is not None:
            where.append("created_at>=?")
            params.append(after)
        if before is not None:
            where.append("created_at<?")
            params.append(before)
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            where.append(f"event_type IN ({placeholders})")
            params.extend(event_types)
        if exclude_event_types:
            placeholders = ",".join("?" for _ in exclude_event_types)
            where.append(f"event_type NOT IN ({placeholders})")
            params.extend(exclude_event_types)
        params.append(max(1, limit))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM agent_event WHERE "
                + " AND ".join(where)
                + " ORDER BY created_at ASC,id ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._event(row) for row in rows]

    def get_state(self, *, tenant_id: str, task_id: str) -> TaskStateSnapshot | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_task_state WHERE tenant_id=? AND task_id=?",
                (tenant_id, task_id),
            ).fetchone()
        return self._state(row) if row else None

    def compare_and_set_state(
        self,
        state: TaskStateSnapshot,
        *,
        expected_version: int,
    ) -> TaskStateSnapshot:
        if state.version != expected_version + 1:
            raise ValueError("new state version must equal expected_version + 1")
        with self._transaction() as db:
            self._write_state(db, state, expected_version=expected_version)
        return state

    def put_decision(self, decision: DecisionRecord) -> DecisionRecord:
        with self._transaction() as db:
            self._put_decision(db, decision)
        return decision

    def _put_decision(
        self,
        db: sqlite3.Connection,
        decision: DecisionRecord,
        *,
        project_state: bool = True,
    ) -> DecisionRecord:
        old = db.execute(
            """
            SELECT * FROM agent_decision
            WHERE tenant_id=? AND task_id=? AND decision_key=? AND status='ACTIVE'
            ORDER BY created_at DESC LIMIT 1
            """,
            (decision.tenant_id, decision.task_id, decision.decision_key),
        ).fetchone()
        if old and old["id"] != decision.id:
            decision.supersedes_id = old["id"]
            db.execute(
                "UPDATE agent_decision SET status='SUPERSEDED',superseded_by_id=?,updated_at=? WHERE id=?",
                (decision.id, now_epoch(), old["id"]),
            )
            db.execute(
                """
                UPDATE agent_memory SET status='SUPERSEDED',updated_at=?
                WHERE tenant_id=? AND task_id=? AND id=?
                """,
                (now_epoch(), decision.tenant_id, decision.task_id, old["id"]),
            )
        db.execute(
            """
            INSERT INTO agent_decision(
                id,tenant_id,task_id,decision_key,decision_text,structured_value,status,
                source_event_id,supersedes_id,superseded_by_id,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                decision_text=excluded.decision_text,structured_value=excluded.structured_value,
                status=excluded.status,source_event_id=excluded.source_event_id,
                supersedes_id=excluded.supersedes_id,superseded_by_id=excluded.superseded_by_id,
                updated_at=excluded.updated_at
            """,
            (
                decision.id,
                decision.tenant_id,
                decision.task_id,
                decision.decision_key,
                decision.decision_text,
                stable_json(decision.structured_value),
                decision.status.value,
                decision.source_event_id,
                decision.supersedes_id,
                decision.superseded_by_id,
                decision.created_at,
                decision.updated_at,
            ),
        )
        state_row = (
            db.execute(
                "SELECT * FROM agent_task_state WHERE tenant_id=? AND task_id=?",
                (decision.tenant_id, decision.task_id),
            ).fetchone()
            if project_state
            else None
        )
        if state_row is not None:
            current = self._state(state_row)
            active = dict(current.active_decisions)
            active[decision.decision_key] = {
                "decision_id": decision.id,
                "source_event_id": decision.source_event_id,
                "status": decision.status.value,
            }
            self._write_state(
                db,
                current.apply_patch({"active_decisions": active}),
                expected_version=current.version,
            )
        return decision

    def active_decisions(
        self,
        *,
        tenant_id: str,
        task_id: str,
        decision_key: str = "",
    ) -> list[DecisionRecord]:
        params: list[Any] = [tenant_id, task_id]
        where = "tenant_id=? AND task_id=? AND status='ACTIVE'"
        if decision_key:
            where += " AND decision_key=?"
            params.append(decision_key)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_decision WHERE {where} ORDER BY updated_at DESC",
                params,
            ).fetchall()
        return [self._decision(row) for row in rows]

    def decision_history(
        self, *, tenant_id: str, task_id: str, decision_key: str
    ) -> list[DecisionRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM agent_decision
                WHERE tenant_id=? AND task_id=? AND decision_key=?
                ORDER BY created_at ASC
                """,
                (tenant_id, task_id, decision_key),
            ).fetchall()
        return [self._decision(row) for row in rows]

    def add_dependency(self, dependency: DependencyRecord) -> DependencyRecord:
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO agent_dependency(
                    id,tenant_id,task_id,step_id,dependency_type,dependency_id,
                    required,dependency_depth,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,task_id,step_id,dependency_type,dependency_id)
                DO UPDATE SET required=MAX(agent_dependency.required,excluded.required),
                              dependency_depth=MIN(agent_dependency.dependency_depth,excluded.dependency_depth)
                """,
                (
                    dependency.id,
                    dependency.tenant_id,
                    dependency.task_id,
                    dependency.step_id,
                    dependency.dependency_type.value,
                    dependency.dependency_id,
                    int(dependency.required),
                    dependency.dependency_depth,
                    dependency.created_at,
                ),
            )
            row = db.execute(
                """
                SELECT * FROM agent_dependency WHERE tenant_id=? AND task_id=? AND step_id=?
                AND dependency_type=? AND dependency_id=?
                """,
                (
                    dependency.tenant_id,
                    dependency.task_id,
                    dependency.step_id,
                    dependency.dependency_type.value,
                    dependency.dependency_id,
                ),
            ).fetchone()
        return self._dependency(row)

    def dependencies_for_step(
        self, *, tenant_id: str, task_id: str, step_id: str
    ) -> list[DependencyRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM agent_dependency
                WHERE tenant_id=? AND task_id=? AND step_id=?
                ORDER BY required DESC,dependency_depth ASC,created_at ASC
                """,
                (tenant_id, task_id, step_id),
            ).fetchall()
        return [self._dependency(row) for row in rows]

    def all_dependencies(
        self, *, tenant_id: str, task_id: str
    ) -> list[DependencyRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM agent_dependency
                WHERE tenant_id=? AND task_id=?
                ORDER BY step_id,required DESC,dependency_depth ASC,created_at ASC
                """,
                (tenant_id, task_id),
            ).fetchall()
        return [self._dependency(row) for row in rows]

    def get_artifact(
        self, *, tenant_id: str, task_id: str, artifact_id: str
    ) -> ArtifactRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_artifact WHERE tenant_id=? AND task_id=? AND id=?",
                (tenant_id, task_id, artifact_id),
            ).fetchone()
        return self._artifact(row) if row else None

    def list_artifacts(
        self,
        *,
        tenant_id: str,
        task_id: str,
        statuses: Sequence[str] = (),
    ) -> list[ArtifactRecord]:
        params: list[Any] = [tenant_id, task_id]
        where = "tenant_id=? AND task_id=?"
        if statuses:
            where += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_artifact WHERE {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def all_artifacts(
        self,
        *,
        tenant_id: str | None = None,
        task_id: str | None = None,
    ) -> list[ArtifactRecord]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id:
            where.append("tenant_id=?")
            params.append(tenant_id)
        if task_id:
            where.append("task_id=?")
            params.append(task_id)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_artifact{clause} ORDER BY created_at ASC", params
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def update_artifact_status(
        self,
        *,
        tenant_id: str,
        task_id: str,
        artifact_id: str,
        status: str,
        uri: str | None = None,
    ) -> None:
        with self._transaction() as db:
            if uri is None:
                db.execute(
                    "UPDATE agent_artifact SET status=?,updated_at=? WHERE tenant_id=? AND task_id=? AND id=?",
                    (status, now_epoch(), tenant_id, task_id, artifact_id),
                )
            else:
                db.execute(
                    "UPDATE agent_artifact SET status=?,uri=?,updated_at=? WHERE tenant_id=? AND task_id=? AND id=?",
                    (status, uri, now_epoch(), tenant_id, task_id, artifact_id),
                )

    def put_memory(
        self, memory: MemoryRecord
    ) -> MemoryRecord:
        with self._transaction() as db:
            stored = self._put_memory(db, memory)
        return stored

    def get_memories(
        self,
        *,
        tenant_id: str,
        task_id: str,
        memory_ids: Sequence[str],
    ) -> list[MemoryRecord]:
        if not memory_ids:
            return []
        placeholders = ",".join("?" for _ in memory_ids)
        params: list[Any] = [tenant_id, task_id, *memory_ids]
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_memory WHERE tenant_id=? AND task_id=? AND id IN ({placeholders})",
                params,
            ).fetchall()
        by_id = {row["id"]: self._memory(row) for row in rows}
        return [by_id[memory_id] for memory_id in memory_ids if memory_id in by_id]

    @bounded_search
    def search_memories_lexical(self, request: MemorySearchRequest) -> list[MemoryHit]:
        if request.candidate_limit <= 0 or not request.statuses:
            return []
        params: list[Any] = [request.tenant_id, request.task_id]
        where = "m.tenant_id=? AND m.task_id=?"
        where += " AND m.status IN (" + ",".join("?" for _ in request.statuses) + ")"
        params.extend(request.statuses)
        if request.memory_types:
            where += (
                " AND m.memory_type IN ("
                + ",".join("?" for _ in request.memory_types)
                + ")"
            )
            params.extend(request.memory_types)
        if request.excluded_facets:
            where += (
                " AND NOT EXISTS (SELECT 1 FROM json_each(m.facets) AS facet WHERE facet.value IN ("
                + ",".join("?" for _ in request.excluded_facets)
                + "))"
            )
            params.extend(request.excluded_facets)
        pool_limit = request.candidate_limit * 4 + 1
        match = sqlite_match_expression(request.query)
        lexical_values: dict[str, float] = {}
        if match and self._fts_available:
            with self._lock:
                rows = self._connection.execute(
                    f"""
                    SELECT m.*, bm25(agent_memory_fts, 0.0, 1.0) AS lexical_rank
                    FROM agent_memory_fts
                    JOIN agent_memory AS m ON m.id=agent_memory_fts.memory_id
                    WHERE agent_memory_fts MATCH ? AND {where}
                    ORDER BY lexical_rank ASC, m.importance DESC, m.updated_at DESC
                    LIMIT ?
                    """,
                    (match, *params, pool_limit),
                ).fetchall()
            for row in rows:
                strength = max(0.0, -float(row["lexical_rank"] or 0.0))
                lexical_values[str(row["id"])] = strength / (1.0 + strength)
        else:
            with self._lock:
                rows = self._connection.execute(
                    f"""
                    SELECT m.* FROM agent_memory AS m
                    WHERE {where}
                    ORDER BY m.importance DESC,m.updated_at DESC
                    {"" if match else "LIMIT ?"}
                    """,
                    (*params, *(() if match else (pool_limit,))),
                ).fetchall()
            if match:
                documents = [
                    memory_search_document(
                        title=row["title"],
                        preview_text=row["preview_text"],
                        facets=json.loads(row["facets"] or "[]"),
                    )
                    for row in rows
                ]
                lexical_values = {
                    str(row["id"]): score
                    for row, score in zip(rows, bm25_scores(documents, request.query))
                    if score > 0
                }
        hits: list[MemoryHit] = []
        excluded_facets = set(request.excluded_facets)
        for row in rows:
            record = self._memory(row)
            if excluded_facets.intersection(record.facets):
                continue
            lexical = lexical_values.get(record.id, 0.0)
            if match and lexical <= 0:
                continue
            facet_gain = len(set(request.required_facets) & set(record.facets)) * 0.15
            recency = 1.0 / (1.0 + max(0.0, now_epoch() - record.updated_at) / 86400.0)
            score = (
                lexical
                + facet_gain
                + record.importance * 0.10
                + recency * 0.03
                + scope_boost(
                    record_phase=record.phase,
                    record_run_id=record.run_id,
                    phase=request.phase,
                    run_id=request.run_id,
                )
            )
            hits.append(
                MemoryHit(
                    record=record,
                    score=score,
                    lexical_score=lexical,
                    reason="sqlite-fts5-bm25"
                    if self._fts_available
                    else "bm25-fallback",
                )
            )
        hits.sort(key=lambda hit: (hit.score, hit.record.updated_at), reverse=True)
        return SearchResults(
            hits[: request.candidate_limit],
            has_more=len(hits) > request.candidate_limit or len(rows) >= pool_limit,
        )

    def verify_active_memories(
        self,
        *,
        tenant_id: str,
        task_id: str,
        memory_ids: Sequence[str],
    ) -> list[MemoryRecord]:
        return [
            memory
            for memory in self.get_memories(
                tenant_id=tenant_id, task_id=task_id, memory_ids=memory_ids
            )
            if memory.status in {MemoryStatus.ACTIVE, MemoryStatus.OPEN}
        ]


    def put_checkpoint(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        with self._transaction() as db:
            self._put_checkpoint(db, checkpoint)
        return checkpoint

    @staticmethod
    def _put_checkpoint(
        db: sqlite3.Connection, checkpoint: CheckpointRecord
    ) -> CheckpointRecord:
        db.execute(
            """
            INSERT OR IGNORE INTO agent_checkpoint(
                id,tenant_id,task_id,run_id,state_version,current_step_id,memory_mode,
                snapshot,content_hash,schema_version,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                checkpoint.id,
                checkpoint.tenant_id,
                checkpoint.task_id,
                checkpoint.run_id,
                checkpoint.state_version,
                checkpoint.current_step_id,
                checkpoint.memory_mode.value,
                stable_json(checkpoint.snapshot),
                checkpoint.content_hash,
                checkpoint.schema_version,
                checkpoint.created_at,
            ),
        )
        return checkpoint

    def latest_checkpoint(
        self, *, tenant_id: str, task_id: str
    ) -> CheckpointRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM agent_checkpoint WHERE tenant_id=? AND task_id=?
                AND COALESCE(json_extract(snapshot, '$.checkpoint_type'), 'TASK_STATE')='TASK_STATE'
                ORDER BY created_at DESC LIMIT 1
                """,
                (tenant_id, task_id),
            ).fetchone()
        return self._checkpoint(row) if row else None

    def all_checkpoints(
        self, *, tenant_id: str, task_id: str
    ) -> list[CheckpointRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM agent_checkpoint
                WHERE tenant_id=? AND task_id=? ORDER BY created_at ASC
                """,
                (tenant_id, task_id),
            ).fetchall()
        return [self._checkpoint(row) for row in rows]

    def get_checkpoint(
        self, *, tenant_id: str, task_id: str, checkpoint_id: str
    ) -> CheckpointRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM agent_checkpoint
                WHERE tenant_id=? AND task_id=? AND id=?
                """,
                (tenant_id, task_id, checkpoint_id),
            ).fetchone()
        return self._checkpoint(row) if row else None

    def set_retention_policy(self, policy: TaskRetentionPolicy) -> TaskRetentionPolicy:
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO memory_retention_policy(
                    tenant_id,task_id,retention_until,legal_hold,deletion_mode,set_by,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,task_id) DO UPDATE SET
                    retention_until=excluded.retention_until,
                    legal_hold=excluded.legal_hold,
                    deletion_mode=excluded.deletion_mode,
                    set_by=excluded.set_by,
                    updated_at=excluded.updated_at
                """,
                (
                    policy.tenant_id,
                    policy.task_id,
                    policy.retention_until,
                    int(policy.legal_hold),
                    policy.deletion_mode,
                    policy.set_by,
                    policy.updated_at,
                ),
            )
        return policy

    def get_retention_policy(
        self, *, tenant_id: str, task_id: str
    ) -> TaskRetentionPolicy | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memory_retention_policy WHERE tenant_id=? AND task_id=?",
                (tenant_id, task_id),
            ).fetchone()
        if row is None:
            return None
        return TaskRetentionPolicy(
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            retention_until=row["retention_until"],
            legal_hold=bool(row["legal_hold"]),
            deletion_mode=row["deletion_mode"],
            set_by=row["set_by"],
            updated_at=row["updated_at"],
        )

    def put_archive_manifest(self, manifest: ArchiveManifest) -> ArchiveManifest:
        with self._transaction() as db:
            artifact = db.execute(
                "SELECT * FROM agent_artifact WHERE id=? AND tenant_id=? AND task_id=?",
                (manifest.artifact_id, manifest.tenant_id, manifest.task_id),
            ).fetchone()
            if artifact is None:
                raise ValueError("archive manifest artifact is missing or out of scope")
            if (
                artifact["uri"] != manifest.uri
                or artifact["content_hash"] != manifest.object_hash
                or artifact["size_bytes"] != manifest.compressed_size_bytes
            ):
                raise ValueError("archive manifest does not match artifact registry")
            existing = db.execute(
                "SELECT * FROM memory_archive_manifest WHERE id=?", (manifest.id,)
            ).fetchone()
            if existing is not None:
                immutable = (
                    "tenant_id",
                    "task_id",
                    "artifact_id",
                    "uri",
                    "content_hash",
                    "object_hash",
                    "event_count",
                    "uncompressed_size_bytes",
                    "compressed_size_bytes",
                    "first_event_id",
                    "last_event_id",
                    "first_created_at",
                    "last_created_at",
                    "rule_name",
                    "backup_class",
                    "schema_version",
                )
                incoming = self._archive_manifest_values(manifest)
                if any(existing[key] != incoming[key] for key in immutable):
                    raise ValueError("archive manifest immutable fields changed")
            db.execute(
                """
                INSERT INTO memory_archive_manifest(
                    id,tenant_id,task_id,artifact_id,uri,archive_type,codec,media_type,
                    content_hash,object_hash,event_count,uncompressed_size_bytes,
                    compressed_size_bytes,first_event_id,last_event_id,first_created_at,
                    last_created_at,rule_name,backup_class,status,metadata,last_verified_at,
                    restored_at,schema_version,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,metadata=excluded.metadata,
                    last_verified_at=excluded.last_verified_at,
                    restored_at=excluded.restored_at,updated_at=excluded.updated_at
                """,
                tuple(self._archive_manifest_values(manifest).values()),
            )
        return manifest

    def get_archive_manifest(
        self, *, tenant_id: str, task_id: str, archive_id: str
    ) -> ArchiveManifest | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memory_archive_manifest "
                "WHERE tenant_id=? AND task_id=? AND id=?",
                (tenant_id, task_id, archive_id),
            ).fetchone()
        return self._archive_manifest(row) if row else None

    def list_archive_manifests(
        self,
        *,
        tenant_id: str,
        task_id: str,
        statuses: Sequence[str] = (),
    ) -> list[ArchiveManifest]:
        params: list[Any] = [tenant_id, task_id]
        where = "tenant_id=? AND task_id=?"
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where += f" AND status IN ({placeholders})"
            params.extend(statuses)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM memory_archive_manifest WHERE {where} "
                "ORDER BY first_created_at ASC,id ASC",
                params,
            ).fetchall()
        return [self._archive_manifest(row) for row in rows]

    def working_set_get(self, *, tenant_id: str, task_id: str, phase: str) -> list[str]:
        now = now_epoch()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT memory_id FROM memory_working_set
                WHERE tenant_id=? AND task_id=? AND phase=? AND expires_at>?
                ORDER BY hit_count DESC,last_used_at DESC
                """,
                (tenant_id, task_id, phase, now),
            ).fetchall()
        return [row["memory_id"] for row in rows]

    def working_set_touch(
        self,
        *,
        tenant_id: str,
        task_id: str,
        phase: str,
        memory_ids: Sequence[str],
        state_version: int,
        ttl_seconds: int = 3600,
    ) -> None:
        now = now_epoch()
        expires = now + ttl_seconds
        with self._transaction() as db:
            for memory_id in memory_ids:
                db.execute(
                    """
                    INSERT INTO memory_working_set(
                        tenant_id,task_id,phase,memory_id,hit_count,state_version,expires_at,last_used_at
                    ) VALUES (?,?,?,?,1,?,?,?)
                    ON CONFLICT(tenant_id,task_id,phase,memory_id) DO UPDATE SET
                        hit_count=memory_working_set.hit_count+1,state_version=excluded.state_version,
                        expires_at=excluded.expires_at,last_used_at=excluded.last_used_at
                    """,
                    (tenant_id, task_id, phase, memory_id, state_version, expires, now),
                )

    def working_set_invalidate(
        self,
        *,
        tenant_id: str,
        task_id: str,
        memory_ids: Sequence[str],
    ) -> None:
        if not memory_ids:
            return
        placeholders = ",".join("?" for _ in memory_ids)
        with self._transaction() as db:
            db.execute(
                f"DELETE FROM memory_working_set WHERE tenant_id=? AND task_id=? AND memory_id IN ({placeholders})",
                [tenant_id, task_id, *memory_ids],
            )

    def referenced_artifact_ids(self, *, tenant_id: str | None = None) -> set[str]:
        params: list[Any] = []
        where = "status<>'TOMBSTONED'"
        if tenant_id:
            where += " AND tenant_id=?"
            params.append(tenant_id)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT id FROM agent_artifact WHERE {where}", params
            ).fetchall()
        return {row["id"] for row in rows}

    def artifact_reference_exists(
        self, *, tenant_id: str, artifact_id: str, uri: str
    ) -> bool:
        serialized_id = f'%"{artifact_id}"%'
        checks = (
            (
                "SELECT 1 FROM agent_artifact WHERE tenant_id=? AND (id=? OR uri=?) LIMIT 1",
                (tenant_id, artifact_id, uri),
            ),
            (
                "SELECT 1 FROM agent_event WHERE tenant_id=? AND artifact_uri=? LIMIT 1",
                (tenant_id, uri),
            ),
            (
                "SELECT 1 FROM agent_memory WHERE tenant_id=? AND artifact_id=? LIMIT 1",
                (tenant_id, artifact_id),
            ),
            (
                "SELECT 1 FROM agent_task_state WHERE tenant_id=? AND active_artifacts LIKE ? LIMIT 1",
                (tenant_id, serialized_id),
            ),
            (
                "SELECT 1 FROM agent_checkpoint WHERE tenant_id=? AND snapshot LIKE ? LIMIT 1",
                (tenant_id, serialized_id),
            ),
            (
                (
                    "SELECT 1 FROM memory_archive_manifest WHERE tenant_id=? "
                    "AND (artifact_id=? OR uri=?) AND status<>'TOMBSTONED' LIMIT 1"
                ),
                (tenant_id, artifact_id, uri),
            ),
        )
        with self._lock:
            return any(
                self._connection.execute(query, params).fetchone() is not None
                for query, params in checks
            )

    def protected_artifact_ids(
        self, *, tenant_id: str, artifacts: Sequence[ArtifactRecord], ignore_candidate_rows: bool = False
    ) -> set[str]:
        """Trace all candidate artifact owners with one scan per source table.

        Checking each candidate separately repeatedly reads large checkpoint
        snapshots under the history write lease. Include every reference source
        used by artifact_reference_exists, and protect aliases of a retained URI.
        Before collection, candidates' own registry rows are not external owners.
        """
        by_id = {artifact.id: artifact for artifact in artifacts}
        by_uri: dict[str, set[str]] = {}
        for artifact in artifacts:
            by_uri.setdefault(artifact.uri, set()).add(artifact.id)
        referenced: set[str] = set()
        if not by_id:
            return referenced

        def protect(value: str) -> None:
            if value in by_id:
                referenced.update(by_uri[by_id[value].uri])
            referenced.update(by_uri.get(value, ()))

        with self._lock:
            for row in self._connection.execute(
                "SELECT id,uri FROM agent_artifact WHERE tenant_id=?", (tenant_id,)
            ):
                if ignore_candidate_rows and row["id"] in by_id:
                    continue
                protect(row["id"])
                protect(row["uri"])
            for query in (
                "SELECT artifact_uri FROM agent_event WHERE tenant_id=? AND artifact_uri<>''",
                "SELECT artifact_id FROM agent_memory WHERE tenant_id=? AND artifact_id<>''",
                "SELECT artifact_id,uri FROM memory_archive_manifest WHERE tenant_id=? AND status<>'TOMBSTONED'",
            ):
                for row in self._connection.execute(query, (tenant_id,)):
                    for value in row:
                        if isinstance(value, str):
                            protect(value)
            for query in (
                "SELECT active_artifacts FROM agent_task_state WHERE tenant_id=?",
                "SELECT snapshot FROM agent_checkpoint WHERE tenant_id=?",
            ):
                for row in self._connection.execute(query, (tenant_id,)):
                    pending = [json.loads(row[0])]
                    while pending:
                        value = pending.pop()
                        if isinstance(value, str):
                            protect(value)
                        elif isinstance(value, dict):
                            pending.extend(value.keys())
                            pending.extend(value.values())
                        elif isinstance(value, list):
                            pending.extend(value)
        return referenced

    def hard_delete_task(
        self,
        *,
        tenant_id: str,
        task_id: str,
        request_id: str = "",
        actor_hash: str = "",
        reason_hash: str = "",
    ) -> dict[str, int]:
        tables = [
            "conversation_history_migration",
            "conversation_history_event",
            "conversation_history_head",
            "memory_working_set",
            "memory_retention_policy",
            "memory_archive_manifest",
            "agent_memory",
            "agent_checkpoint",
            "agent_dependency",
            "agent_decision",
            "agent_artifact",
            "agent_event",
            "agent_step",
            "agent_task_state",
            "agent_run",
            "agent_task",
        ]
        counts: dict[str, int] = {}
        request_id = request_id or new_id("delete")
        self._hard_delete_allowed = True
        try:
            with self._transaction() as db:
                for table in tables:
                    column = "id" if table == "agent_task" else "task_id"
                    cursor = db.execute(
                        f"DELETE FROM {table} WHERE tenant_id=? AND {column}=?",
                        (tenant_id, task_id),
                    )
                    counts[table] = max(0, cursor.rowcount)
                db.execute(
                    """
                    INSERT INTO memory_deletion_audit(
                        request_id,tenant_id,task_id_hash,scope,actor_hash,reason_hash,
                        execution_status,deleted_counts,requested_at,completed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        request_id,
                        tenant_id,
                        content_hash_text(task_id),
                        "TASK",
                        actor_hash,
                        reason_hash,
                        "COMPLETED",
                        stable_json(counts),
                        now_epoch(),
                        now_epoch(),
                    ),
                )
        finally:
            self._hard_delete_allowed = False
        return counts

    def get_deletion_audit(self, *, request_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memory_deletion_audit WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["deleted_counts"] = json.loads(value["deleted_counts"])
        return value


    def all_memories(
        self, *, tenant_id: str | None = None, task_id: str | None = None
    ) -> list[MemoryRecord]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id:
            where.append("tenant_id=?")
            params.append(tenant_id)
        if task_id:
            where.append("task_id=?")
            params.append(task_id)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_memory{clause} ORDER BY created_at ASC", params
            ).fetchall()
        return [self._memory(row) for row in rows]

    def _put_artifact(self, db: sqlite3.Connection, artifact: ArtifactRecord) -> None:
        db.execute(
            """
            INSERT INTO agent_artifact(
                id,tenant_id,task_id,run_id,step_id,artifact_type,uri,content_hash,
                media_type,size_bytes,metadata,source_event_id,status,schema_version,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                uri=excluded.uri,content_hash=excluded.content_hash,size_bytes=excluded.size_bytes,
                metadata=excluded.metadata,source_event_id=excluded.source_event_id,
                status=excluded.status,updated_at=excluded.updated_at
            """,
            (
                artifact.id,
                artifact.tenant_id,
                artifact.task_id,
                artifact.run_id,
                artifact.step_id,
                artifact.artifact_type,
                artifact.uri,
                artifact.content_hash,
                artifact.media_type,
                artifact.size_bytes,
                stable_json(artifact.metadata),
                artifact.source_event_id,
                artifact.status.value,
                artifact.schema_version,
                artifact.created_at,
                artifact.updated_at,
            ),
        )

    def _put_memory(self, db: sqlite3.Connection, memory: MemoryRecord) -> MemoryRecord:
        existing = db.execute(
            """
            SELECT * FROM agent_memory
            WHERE tenant_id=? AND task_id=? AND memory_type=? AND content_hash=?
              AND source_event_id=? AND artifact_id=?
              AND source_start=? AND source_end=?
            """,
            (
                memory.tenant_id,
                memory.task_id,
                memory.memory_type.value,
                memory.content_hash,
                memory.source_event_id,
                memory.artifact_id,
                memory.source_range.start,
                memory.source_range.end,
            ),
        ).fetchone()
        if existing:
            return self._memory(existing)
        db.execute(
            """
            INSERT INTO agent_memory(
                id,tenant_id,task_id,run_id,step_id,memory_type,title,preview_text,status,
                importance,source_event_id,artifact_id,source_start,source_end,source_unit,
                content_hash,token_count,phase,facets,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                memory.id,
                memory.tenant_id,
                memory.task_id,
                memory.run_id,
                memory.step_id,
                memory.memory_type.value,
                memory.title,
                memory.preview_text,
                memory.status.value,
                memory.importance,
                memory.source_event_id,
                memory.artifact_id,
                memory.source_range.start,
                memory.source_range.end,
                memory.source_range.unit,
                memory.content_hash,
                memory.token_count,
                memory.phase,
                stable_json(memory.facets),
                memory.created_at,
                memory.updated_at,
            ),
        )
        if self._fts_available:
            db.execute("DELETE FROM agent_memory_fts WHERE memory_id=?", (memory.id,))
            db.execute(
                "INSERT INTO agent_memory_fts(memory_id,search_document) VALUES (?,?)",
                (
                    memory.id,
                    memory_search_document(
                        title=memory.title,
                        preview_text=memory.preview_text,
                        facets=memory.facets,
                    ),
                ),
            )
        return memory


    @staticmethod
    def _write_state(
        db: sqlite3.Connection, state: TaskStateSnapshot, *, expected_version: int
    ) -> None:
        cursor = db.execute(
            """
            UPDATE agent_task_state SET
                version=?,phase=?,current_step_id=?,goal=?,active_constraints=?,
                active_decisions=?,open_errors=?,completed_steps=?,pending_steps=?,
                active_artifacts=?,blocked_by=?,handoff_notes=?,context_rotations=?,updated_at=?
            WHERE tenant_id=? AND task_id=? AND version=?
            """,
            (
                state.version,
                state.phase,
                state.current_step_id,
                state.goal,
                stable_json(state.active_constraints),
                stable_json(state.active_decisions),
                stable_json(state.open_errors),
                stable_json(state.completed_steps),
                stable_json(state.pending_steps),
                stable_json(state.active_artifacts),
                stable_json(state.blocked_by),
                stable_json(state.handoff_notes),
                stable_json(state.context_rotations),
                state.updated_at,
                state.tenant_id,
                state.task_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise StateVersionConflict(
                f"task state version conflict for {state.task_id}: expected {expected_version}"
            )

    @staticmethod
    def _event(row: sqlite3.Row | Mapping[str, Any]) -> AgentEvent:
        return AgentEvent(
            id=row["id"],
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            event_type=EventType(row["event_type"]),
            content_text=row["content_text"],
            content_json=json.loads(row["content_json"] or "{}"),
            artifact_uri=row["artifact_uri"],
            content_hash=row["content_hash"],
            token_count=row["token_count"],
            caused_by_event_id=row["caused_by_event_id"],
            correlation_id=row["correlation_id"],
            idempotency_key=row["idempotency_key"],
            schema_version=row["schema_version"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _state(row: sqlite3.Row | Mapping[str, Any]) -> TaskStateSnapshot:
        return TaskStateSnapshot(
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            version=row["version"],
            phase=row["phase"],
            current_step_id=row["current_step_id"],
            goal=row["goal"],
            active_constraints=json.loads(row["active_constraints"] or "[]"),
            active_decisions=json.loads(row["active_decisions"] or "{}"),
            open_errors=json.loads(row["open_errors"] or "[]"),
            completed_steps=json.loads(row["completed_steps"] or "[]"),
            pending_steps=json.loads(row["pending_steps"] or "[]"),
            active_artifacts=json.loads(row["active_artifacts"] or "[]"),
            blocked_by=json.loads(row["blocked_by"] or "[]"),
            handoff_notes=json.loads(row["handoff_notes"] or "{}") if "handoff_notes" in row.keys() else {},
            context_rotations=json.loads(row["context_rotations"] or "{}") if "context_rotations" in row.keys() else {},
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _decision(row: sqlite3.Row | Mapping[str, Any]) -> DecisionRecord:
        return DecisionRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            decision_key=row["decision_key"],
            decision_text=row["decision_text"],
            structured_value=json.loads(row["structured_value"] or "{}"),
            status=MemoryStatus(row["status"]),
            source_event_id=row["source_event_id"],
            supersedes_id=row["supersedes_id"],
            superseded_by_id=row["superseded_by_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _dependency(row: sqlite3.Row | Mapping[str, Any]) -> DependencyRecord:
        return DependencyRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            step_id=row["step_id"],
            dependency_type=DependencyType(row["dependency_type"]),
            dependency_id=row["dependency_id"],
            required=bool(row["required"]),
            dependency_depth=row["dependency_depth"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _artifact(row: sqlite3.Row | Mapping[str, Any]) -> ArtifactRecord:
        return ArtifactRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            artifact_type=row["artifact_type"],
            uri=row["uri"],
            content_hash=row["content_hash"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            metadata=json.loads(row["metadata"] or "{}"),
            source_event_id=row["source_event_id"],
            status=ArtifactStatus(row["status"]),
            schema_version=row["schema_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _memory(row: sqlite3.Row | Mapping[str, Any]) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            memory_type=MemoryType(row["memory_type"]),
            title=row["title"],
            preview_text=row["preview_text"],
            status=MemoryStatus(row["status"]),
            importance=row["importance"],
            source_event_id=row["source_event_id"],
            artifact_id=row["artifact_id"],
            source_range=SourceRange(
                unit=row["source_unit"],
                start=row["source_start"],
                end=row["source_end"],
            ),
            content_hash=row["content_hash"],
            token_count=row["token_count"],
            phase=row["phase"],
            facets=json.loads(row["facets"] or "[]"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _checkpoint(row: sqlite3.Row | Mapping[str, Any]) -> CheckpointRecord:
        return CheckpointRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            state_version=row["state_version"],
            current_step_id=row["current_step_id"],
            memory_mode=MemoryMode(row["memory_mode"]),
            snapshot=json.loads(row["snapshot"] or "{}"),
            content_hash=row["content_hash"],
            schema_version=row["schema_version"],
            created_at=row["created_at"],
        )


    @staticmethod
    def _archive_manifest_values(manifest: ArchiveManifest) -> dict[str, Any]:
        return {
            "id": manifest.id,
            "tenant_id": manifest.tenant_id,
            "task_id": manifest.task_id,
            "artifact_id": manifest.artifact_id,
            "uri": manifest.uri,
            "archive_type": manifest.archive_type,
            "codec": manifest.codec,
            "media_type": manifest.media_type,
            "content_hash": manifest.content_hash,
            "object_hash": manifest.object_hash,
            "event_count": manifest.event_count,
            "uncompressed_size_bytes": manifest.uncompressed_size_bytes,
            "compressed_size_bytes": manifest.compressed_size_bytes,
            "first_event_id": manifest.first_event_id,
            "last_event_id": manifest.last_event_id,
            "first_created_at": manifest.first_created_at,
            "last_created_at": manifest.last_created_at,
            "rule_name": manifest.rule_name,
            "backup_class": manifest.backup_class,
            "status": manifest.status.value,
            "metadata": stable_json(manifest.metadata),
            "last_verified_at": manifest.last_verified_at,
            "restored_at": manifest.restored_at,
            "schema_version": manifest.schema_version,
            "created_at": manifest.created_at,
            "updated_at": manifest.updated_at,
        }

    @staticmethod
    def _archive_manifest(row: sqlite3.Row) -> ArchiveManifest:
        return ArchiveManifest(
            id=row["id"],
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            artifact_id=row["artifact_id"],
            uri=row["uri"],
            archive_type=row["archive_type"],
            codec=row["codec"],
            media_type=row["media_type"],
            content_hash=row["content_hash"],
            object_hash=row["object_hash"],
            event_count=row["event_count"],
            uncompressed_size_bytes=row["uncompressed_size_bytes"],
            compressed_size_bytes=row["compressed_size_bytes"],
            first_event_id=row["first_event_id"],
            last_event_id=row["last_event_id"],
            first_created_at=row["first_created_at"],
            last_created_at=row["last_created_at"],
            rule_name=row["rule_name"],
            backup_class=row["backup_class"],
            status=ArchiveStatus(row["status"]),
            metadata=json.loads(row["metadata"] or "{}"),
            last_verified_at=row["last_verified_at"],
            restored_at=row["restored_at"],
            schema_version=row["schema_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
