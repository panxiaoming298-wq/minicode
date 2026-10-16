"""Session transactions over the existing memory authority and artifact store.

The installation pins its authority. A failed connection never redirects
history to another backend. Local installations can still use JSON history.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import atomic_write_json

_active = ContextVar("history_storage", default=None)
_read_only = ContextVar("history_storage_read_only", default=False)


class HistoryStorageError(RuntimeError):
    pass


def active_history():
    """Return the active SQLite history authority, or False for legacy file history."""
    return _active.get()


def resolve_history_backend(settings):
    backend = os.environ.get("MINI_CODE_HISTORY_BACKEND") or settings.get("sessionHistoryBackend", "auto")
    if backend == "auto":
        memory = os.environ.get("MEMORY_BACKEND") or settings.get("memoryBackend")
        backend = memory or "file"
    if backend not in {"file", "sqlite"}:
        raise HistoryStorageError("sessionHistoryBackend 必须为 auto、sqlite 或 file。")
    return backend


@contextmanager
def _sqlite_history(settings, marker, marker_path, *, deadline=None, read_only=False):
    from minicode.paged_memory.config import PagedMemoryConfig
    from minicode.paged_memory.artifacts import build_artifact_store
    from minicode.paged_memory.sqlite_store import SqliteMemoryStore
    from minicode.session_history_sqlite import SqliteSessionHistoryAuthority
    from minicode import session
    import sqlite3

    config = PagedMemoryConfig.from_sources(settings, cwd=session.MINI_CODE_DIR)
    identity = _digest({"path": os.path.normcase(os.path.abspath(config.sqlite_path))})
    artifact_id = sqlite_artifact_identity(config)
    expected = {"backend": "sqlite", "databaseId": identity, "artifactStoreId": artifact_id,
                "tenantId": config.tenant_id, "layout": "memory-authority-v1"}
    if marker and any(marker.get(key) != value for key, value in expected.items()):
        raise HistoryStorageError("历史数据库或文件目录已改变，请使用完整备份恢复或显式迁移。")
    updated = {**expected, "storeId": marker.get("storeId") or uuid.uuid4().hex}
    store = SqliteMemoryStore(config.sqlite_path)
    try:
        history = SqliteSessionHistoryAuthority(store, build_artifact_store(config), updated['storeId'], config.tenant_id)
        if updated != marker:
            atomic_write_json(marker_path, updated)
        transaction = store.read_transaction if read_only else store._transaction
        with store.query_deadline(deadline), transaction():
            token = _active.set(history)
            try:
                yield
            finally:
                _active.reset(token)
        history.after_commit()
    except sqlite3.Error as exc:
        raise HistoryStorageError("SQLite 历史存储不可用，本次操作未切换其他存储。") from exc
    finally:
        store.close()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def sqlite_artifact_identity(config):
    return _digest({'type': 'file', 'location': os.path.normcase(os.path.abspath(config.artifact_root))})


@contextmanager
def history_transaction(*, timeout=None, read_only=False):
    from minicode import session
    from minicode.store_lock import store_lock
    if _active.get() is not None:
        if _read_only.get() and not read_only:
            raise HistoryStorageError("A read-only history operation cannot start a write transaction.")
        yield
        return
    # Direct history tools also need the installation lease before creating or
    # checking the binding marker. Session decorators reenter this same lock.
    deadline = time.monotonic() + timeout if timeout is not None else None
    token = _read_only.set(read_only)
    try:
        with store_lock(session.SESSIONS_DIR / '.store.lock', timeout=30.0 if timeout is None else timeout):
            with _history_transaction(deadline=deadline, read_only=read_only):
                yield
    finally:
        _read_only.reset(token)


@contextmanager
def _history_transaction(*, deadline=None, read_only=False):
    if _active.get() is not None:
        yield
        return
    from minicode import session
    from minicode.config import read_settings_file
    settings = read_settings_file(session.MINI_CODE_DIR / "settings.json")
    backend = resolve_history_backend(settings)
    marker_path = session.SESSIONS_DIR / ".history-backend.json"
    if is_unsafe_stored_path(marker_path, boundary=session.SESSIONS_DIR, reject_hardlinks=True):
        raise HistoryStorageError("历史存储标识路径不安全。")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else {}
        if marker and (not isinstance(marker, dict) or not marker.get("storeId")):
            raise ValueError("invalid history authority")
    except (OSError, ValueError) as exc:
        raise HistoryStorageError("历史存储标识已损坏，请恢复记录后重试。") from exc
    if marker and marker.get("backend") != backend:
        raise HistoryStorageError("历史存储已绑定其他后端，请先迁移历史，不能自动切换。")
    if backend == "sqlite":
        with _sqlite_history(settings, marker, marker_path, deadline=deadline, read_only=read_only):
            yield
        return
    if backend == "file":
        if not marker:
            atomic_write_json(marker_path, {"backend": "file", "storeId": uuid.uuid4().hex})
        token = _active.set(False)
        try:
            yield
        finally:
            _active.reset(token)
        return

def history_operation(function=None, *, read_only=False):
    if function is None:
        return functools.partial(history_operation, read_only=read_only)
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with history_transaction(read_only=read_only):
            return function(*args, **kwargs)
    return wrapped
