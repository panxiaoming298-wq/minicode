"""SQLite session history over the event, checkpoint and artifact authority.

History has independent task scopes in the same tenant. Retrieval memories and
their retention policies cannot delete conversation history. Immutable object
ownership belongs to the installation, so forks do not borrow parent-owned data.
All methods run inside history_storage's authority transaction and store lease.
"""
from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy

from minicode.paged_memory.models import AgentEvent, CheckpointRecord, EventType, MemoryMode, content_hash_text, stable_json


class SqliteSessionHistoryAuthority:
    def __init__(self, authority, artifact_store, store_id: str, tenant_id: str):
        self.authority = authority
        self.connection = authority._connection
        self.artifact_store = artifact_store
        self.store_id, self.tenant_id = store_id, tenant_id
        self.object_task = "session_objects_" + store_id
        self._objects: dict[str, bytes] = {}
        self._indexes: dict[str, list[dict]] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._tasks: set[str] = set()
        self._garbage: list = []

    def _id(self, prefix: str, value: str):
        return prefix + "_" + hashlib.sha256(f"{self.tenant_id}:{self.store_id}:{value}".encode()).hexdigest()[:56]

    def task(self, session_id: str):
        return self._id("session", session_id)

    def _ensure_task(self, task_id: str):
        if task_id not in self._tasks:
            self.authority.ensure_task(tenant_id=self.tenant_id, task_id=task_id,
                                       workspace_id="history:" + self.store_id, goal="Session history")
            self._tasks.add(task_id)

    def index(self, session_id: str):
        if session_id not in self._indexes:
            rows = self.connection.execute(
                "SELECT content_json FROM (SELECT content_json, ROW_NUMBER() OVER ("
                "PARTITION BY json_extract(content_json,'$.turnId') "
                "ORDER BY CAST(json_extract(content_json,'$.revision') AS INTEGER) DESC) AS rank "
                "FROM agent_event WHERE tenant_id=? AND task_id=? "
                "AND event_type='SESSION_TURN_STATUS' AND json_extract(content_json,'$.history_store')=?) "
                "WHERE rank=1", (self.tenant_id, self.task(session_id), self.store_id)).fetchall()
            documents = sorted((json.loads(row[0]) for row in rows), key=lambda row: row['sequence_no'])
            self._indexes[session_id] = [value['summary'] for value in documents]
            for value in documents:
                self._revisions[(session_id, value['turnId'])] = value['revision']
        return deepcopy(self._indexes[session_id])

    def save_index(self, session_id: str, rows: list[dict]):
        old = {row["turnId"]: row for row in self.index(session_id)}
        self._ensure_task(self.task(session_id))
        for sequence, row in enumerate(rows):
            if old.get(row["turnId"]) == row:
                continue
            key = (session_id, row["turnId"])
            revision = self._revisions.get(key, 0) + 1
            payload = {"history_store": self.store_id, "session_id": session_id,
                       "turnId": row["turnId"], "sequence_no": sequence,
                       "revision": revision, "summary": row}
            event = AgentEvent(tenant_id=self.tenant_id, task_id=self.task(session_id),
                               run_id="session:" + row["turnId"], event_type=EventType.SESSION_TURN_STATUS,
                               id=self._id("she", stable_json(payload)), content_json=payload,
                               correlation_id=row["turnId"], idempotency_key="session:" + content_hash_text(stable_json(payload)))
            self.authority.persist_event(event)
            self._revisions[key] = revision
        self._indexes[session_id] = deepcopy(rows)

    def manifest(self, session_id: str, turn_id: str):
        rows = self._snapshots(session_id, turn_id)
        return rows[0] if rows else None

    def save_manifest(self, session_id: str, value: dict):
        self._ensure_task(self.task(session_id))
        snapshot = {"checkpoint_type": "SESSION_TURN", "history_store": self.store_id,
                    "session_id": session_id, "turnId": value["turnId"], "manifest": value}
        checkpoint = CheckpointRecord(tenant_id=self.tenant_id, task_id=self.task(session_id),
                                      id=self._id("shc", session_id + ":" + value["turnId"]),
                                      run_id="session:" + value["turnId"], state_version=0,
                                      current_step_id=value["turnId"], memory_mode=MemoryMode.INLINE,
                                      snapshot=snapshot)
        self.authority.put_checkpoint(checkpoint)

    def _artifact(self, key: str):
        row = self.connection.execute(
            "SELECT * FROM agent_artifact WHERE id=? AND tenant_id=? AND task_id=? "
            "AND artifact_type='SESSION_HISTORY_OBJECT' AND json_extract(metadata,'$.raw_hash')=? "
            "AND status='ACTIVE'", (self._id('sho', key), self.tenant_id, self.object_task, key)).fetchone()
        return self.authority._artifact(row) if row else None

    def put_object(self, key: str, payload: bytes):
        if key in self._objects:
            return
        existing = self._artifact(key)
        if existing is None:
            self._ensure_task(self.object_task)
            artifact = self.artifact_store.put_bytes(
                payload, artifact_id=self._id("sho", key), tenant_id=self.tenant_id,
                task_id=self.object_task, run_id="session-history", step_id="",
                media_type="application/gzip", artifact_type="SESSION_HISTORY_OBJECT")
            if self.artifact_store.get_bytes(artifact) != payload:
                raise ValueError("session history upload checksum mismatch")
            artifact.metadata.update(history_store=self.store_id, raw_hash=key)
            self._publish_artifact(artifact)
        self._objects[key] = payload

    def _publish_artifact(self, artifact):
        self.authority._put_artifact(self.connection, artifact)

    def read_object(self, key: str, *, max_bytes=None):
        if max_bytes is not None:
            from minicode.history_objects import HistoryObjectTooLarge
            artifact = self._artifact(key)
            if artifact is None:
                raise ValueError("session history object is missing")
            if artifact.size_bytes > max_bytes:
                raise HistoryObjectTooLarge("Saved history source exceeds the search byte budget.")
        if key not in self._objects:
            artifact = self._artifact(key)
            if artifact is None:
                raise ValueError("session history object is missing")
            self._objects[key] = self.artifact_store.get_bytes(artifact)
        return self._objects[key]

    def manifests(self):
        return self._snapshots()

    def object_keys(self):
        rows = self.connection.execute(
            "SELECT id, metadata FROM agent_artifact WHERE tenant_id=? AND task_id=? "
            "AND artifact_type='SESSION_HISTORY_OBJECT' AND status='ACTIVE' "
            "AND json_extract(metadata,'$.history_store')=?",
            (self.tenant_id, self.object_task, self.store_id))
        keys = set()
        for row in rows:
            key = json.loads(row['metadata']).get('raw_hash')
            if isinstance(key, str) and row['id'] == self._id('sho', key):
                keys.add(key)
        return keys

    def delete_session(self, session_id: str):
        # Only this history task is removed. Project/task retrieval memories stay.
        self.authority.hard_delete_task(tenant_id=self.tenant_id, task_id=self.task(session_id),
                                        reason_hash=content_hash_text("session permanently deleted"))
        self._indexes.pop(session_id, None)

    def collect(self, retained: set[str]):
        rows = self.connection.execute(
            "SELECT * FROM agent_artifact WHERE tenant_id=? AND task_id=? "
            "AND artifact_type='SESSION_HISTORY_OBJECT' AND json_extract(metadata,'$.history_store')=?",
            (self.tenant_id, self.object_task, self.store_id)).fetchall()
        candidates = [self.authority._artifact(row) for row in rows
                      if json.loads(row['metadata']).get('raw_hash') not in retained]
        # Trace owners before deleting registry rows: deletion can cascade to
        # archive manifests, which must remain available as ownership evidence.
        referenced = self.authority.protected_artifact_ids(
            tenant_id=self.tenant_id, artifacts=candidates, ignore_candidate_rows=True)
        garbage = [artifact for artifact in candidates if artifact.id not in referenced]
        self.connection.executemany('DELETE FROM agent_artifact WHERE id=?',
                                    [(artifact.id,) for artifact in garbage])
        self._garbage.extend(garbage)
        return len(self._garbage)

    def after_commit(self):
        # Physical deletion follows the authority commit. An interrupted cleanup
        # leaves an orphan that the existing artifact GC can safely discover.
        if not self._garbage:
            return
        # A new owner may have committed since collection. Keep writers out
        # between the batched ownership recheck and physical removal.
        with self.authority._transaction():
            referenced = self.authority.protected_artifact_ids(
                tenant_id=self.tenant_id, artifacts=self._garbage)
            for artifact in self._garbage:
                if artifact.id not in referenced:
                    try:
                        self.artifact_store.delete(artifact)
                    except Exception:
                        logging.getLogger(__name__).warning("Session artifact cleanup deferred to existing orphan GC: %s", artifact.id)

    def _snapshots(self, session_id=None, turn_id=None):
        query = ("SELECT snapshot,content_hash FROM agent_checkpoint WHERE tenant_id=? "
                 "AND json_extract(snapshot,'$.checkpoint_type')='SESSION_TURN' "
                 "AND json_extract(snapshot,'$.history_store')=?")
        args = [self.tenant_id, self.store_id]
        if session_id is not None:
            query += " AND task_id=? AND id=?"
            args.extend([self.task(session_id), self._id('shc', session_id + ':' + turn_id)])
        result = []
        for row in self.connection.execute(query, args):
            snapshot = json.loads(row['snapshot'])
            if content_hash_text(stable_json(snapshot)) != row['content_hash']:
                raise ValueError('session checkpoint checksum mismatch')
            result.append(snapshot['manifest'])
        return result
