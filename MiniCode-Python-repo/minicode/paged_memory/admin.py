"""Administrative CLI for local initialization, integrity, GC and restore drills."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .archive import EventArchiveManager
from .artifacts import build_artifact_store, build_encrypted_spool
from .chunking import artifact_chunk_records
from .config import PagedMemoryConfig
from .lifecycle import LifecycleManager
from .models import (
    AgentEvent,
    ArtifactStatus,
    EventType,
    MemoryRecord,
    MemoryType,
    SourceRange,
    content_hash_text,
)
from .policy import TokenBudgetManager
from .sqlite_store import SqliteMemoryStore


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _authority(config: PagedMemoryConfig) -> Any:
    config.validate()
    return SqliteMemoryStore(config.sqlite_path)


def _components(config: PagedMemoryConfig) -> tuple[Any, Any, Any]:
    return _authority(config), build_artifact_store(config), build_encrypted_spool(config)


def _cmd_init(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority = _authority(config)
    try:
        authority.initialize()
        _json(
            {
                "initialized": True,
                "backend": config.backend,
                "healthy": authority.health_check(),
            }
        )
    finally:
        authority.close()
    return 0


def _cmd_backfill(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    source_text = source.read_text(encoding="utf-8")
    from .markdown_export import is_generated_export
    if source.is_relative_to(Path(config.export_root).resolve()) or is_generated_export(source_text):
        raise ValueError('generated memory exports cannot be imported')
    source_hash = content_hash_text(source_text)
    data = json.loads(source_text)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise TypeError("legacy memory file has no entries list")
    authority = _authority(config)
    task_id = args.task_id or f"legacy-{data.get('scope', 'project')!s}"
    run_id = "run_legacy_backfill"
    counts = {"candidates": len(entries), "imported": 0, "skipped": 0, "failed": 0}
    try:
        authority.ensure_task(
            tenant_id=args.tenant_id,
            task_id=task_id,
            goal="Legacy memory backfill",
            workspace_id=config.workspace_id,
            user_id=config.user_id,
            cross_session_required=True,
            restart_recovery_required=True,
        )
        authority.create_run(
            tenant_id=args.tenant_id,
            task_id=task_id,
            run_id=run_id,
            mode="PAGED_MEMORY",
            model_name="migration",
        )
        for index, entry in enumerate(entries):
            if args.limit and index >= args.limit:
                break
            try:
                created_at = float(entry.get("created_at", 0) or 0)
                if args.since is not None and created_at < args.since:
                    counts["skipped"] += 1
                    continue
                if args.until is not None and created_at >= args.until:
                    counts["skipped"] += 1
                    continue
                content = str(entry.get("content", ""))
                if not content:
                    counts["skipped"] += 1
                    continue
                digest = content_hash_text(content)
                if args.dry_run:
                    counts["imported"] += 1
                    continue
                event = AgentEvent(
                    tenant_id=args.tenant_id,
                    task_id=task_id,
                    run_id=run_id,
                    event_type=EventType.MEMORY_IMPORTED,
                    content_text=content,
                    content_json={
                        "legacy_id": entry.get("id", ""),
                        "scope": entry.get("scope", data.get("scope", "")),
                        "category": entry.get("category", ""),
                        "source_file_hash": source_hash,
                    },
                    content_hash=digest,
                    idempotency_key=f"legacy:{source}:{entry.get('id', index)}:{digest}",
                    created_at=created_at,
                )
                memory = MemoryRecord(
                    tenant_id=args.tenant_id,
                    task_id=task_id,
                    run_id=run_id,
                    memory_type=MemoryType.FACT,
                    title=str(entry.get("category", "legacy memory"))[:256],
                    preview_text=content[:2_000],
                    importance=min(
                        1.0, 0.5 + float(entry.get("usage_count", 0)) * 0.02
                    ),
                    source_event_id=event.id,
                    source_range=SourceRange(unit="CHAR", start=0, end=len(content)),
                    content_hash=digest,
                    facets=[
                        str(entry.get("category", "")),
                        *[str(value) for value in entry.get("tags", [])],
                        *[str(value) for value in entry.get("domains", [])],
                    ],
                    created_at=float(entry.get("created_at", 0) or 0),
                    updated_at=float(entry.get("updated_at", 0) or 0),
                )
                stored_event, _ = authority.persist_event(
                    event, memory=memory
                )
                counts["imported"] += 1 if stored_event.id == event.id else 0
                counts["skipped"] += 1 if stored_event.id != event.id else 0
            except Exception:
                counts["failed"] += 1
                if not args.continue_on_error:
                    raise
        _json(
            {
                **counts,
                "dry_run": args.dry_run,
                "task_id": task_id,
                "source": str(source),
            }
        )
    finally:
        authority.close()
    return 0 if counts["failed"] == 0 else 2


def _cmd_backfill_artifact_chunks(
    config: PagedMemoryConfig, args: argparse.Namespace
) -> int:
    """Idempotently project historical text Artifacts into bounded search cards."""

    authority, artifacts, spool = _components(config)
    token_budget = TokenBudgetManager()
    report: dict[str, Any] = {
        "dry_run": not args.apply,
        "artifacts_scanned": 0,
        "text_artifacts": 0,
        "non_text_artifacts_skipped": 0,
        "inactive_artifacts_skipped": 0,
        "expected_chunks": 0,
        "existing_chunks": 0,
        "missing_chunks": 0,
        "inserted_chunks": 0,
        "errors": [],
    }
    try:
        artifact_records = authority.list_artifacts(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
        )
        memories = authority.all_memories(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
        )
        report["artifacts_scanned"] = len(artifact_records)
        by_source_event: dict[str, list[MemoryRecord]] = {}
        chunk_by_range: dict[tuple[str, str, int, int], MemoryRecord] = {}
        for memory in memories:
            by_source_event.setdefault(memory.source_event_id, []).append(memory)
            if memory.memory_type is MemoryType.ARTIFACT_CHUNK:
                chunk_by_range[
                    (
                        memory.source_event_id,
                        memory.artifact_id,
                        memory.source_range.start,
                        memory.source_range.end,
                    )
                ] = memory

        fallback_events: dict[str, AgentEvent] | None = None
        active_statuses = {
            ArtifactStatus.ACTIVE.value,
            ArtifactStatus.PENDING_UPLOAD.value,
        }
        chunk_size = max(1_024, min(7_000, config.raw_page_token_limit * 3))
        for artifact in artifact_records:
            status = (
                artifact.status.value
                if isinstance(artifact.status, ArtifactStatus)
                else str(artifact.status)
            )
            if status not in active_statuses:
                report["inactive_artifacts_skipped"] += 1
                continue
            if not artifact.media_type.casefold().startswith("text/"):
                report["non_text_artifacts_skipped"] += 1
                continue
            report["text_artifacts"] += 1
            try:
                event = None
                if artifact.source_event_id:
                    event = authority.get_event(
                        tenant_id=args.tenant_id,
                        task_id=args.task_id,
                        event_id=artifact.source_event_id,
                    )
                if event is None:
                    if fallback_events is None:
                        fallback_events = {
                            candidate.artifact_uri: candidate
                            for candidate in authority.events_in_range(
                                tenant_id=args.tenant_id,
                                task_id=args.task_id,
                                limit=100_000,
                            )
                            if candidate.artifact_uri
                        }
                    event = fallback_events.get(artifact.uri)
                if event is None:
                    raise ValueError("source event is unavailable")

                store = (
                    spool
                    if status == ArtifactStatus.PENDING_UPLOAD.value
                    else artifacts
                )
                if store is None:
                    raise RuntimeError("artifact store is unavailable")
                text = store.get_bytes(artifact).decode("utf-8", errors="strict")
                source_memories = by_source_event.get(event.id, [])
                primary = next(
                    (
                        memory
                        for memory in source_memories
                        if memory.memory_type is not MemoryType.ARTIFACT_CHUNK
                    ),
                    None,
                )
                raw_ok = event.content_json.get("ok")
                ok = (
                    raw_ok
                    if isinstance(raw_ok, bool)
                    else event.event_type is not EventType.TOOL_ERROR
                )
                tool_name = str(
                    event.content_json.get("tool_name")
                    or artifact.artifact_type.casefold()
                )
                candidates = artifact_chunk_records(
                    text=text,
                    event=event,
                    artifact=artifact,
                    tool_name=tool_name,
                    phase=primary.phase if primary is not None else "",
                    ok=ok,
                    estimate_tokens=token_budget.estimate_text,
                    max_bytes=chunk_size,
                    max_tokens=384,
                )
                report["expected_chunks"] += len(candidates)
                for chunk in candidates:
                    key = (
                        chunk.source_event_id,
                        chunk.artifact_id,
                        chunk.source_range.start,
                        chunk.source_range.end,
                    )
                    existing = chunk_by_range.get(key)
                    if existing is not None:
                        if (
                            existing.content_hash != chunk.content_hash
                            or existing.status != chunk.status
                        ):
                            raise ValueError("existing chunk conflicts with source bytes")
                        report["existing_chunks"] += 1
                        continue
                    report["missing_chunks"] += 1
                    if not args.apply:
                        continue
                    stored = authority.put_memory(chunk)
                    if stored.content_hash != chunk.content_hash:
                        raise ValueError("stored chunk conflicts with source bytes")
                    chunk_by_range[key] = stored
                    report["inserted_chunks"] += 1
            except Exception as exc:  # noqa: BLE001 - report each artifact independently.
                report["errors"].append(
                    {
                        "artifact_id": artifact.id,
                        "error": type(exc).__name__,
                    }
                )
        _json(report)
        return 0 if not report["errors"] else 5
    finally:
        for component in (spool, artifacts):
            close = getattr(component, "close", None)
            if callable(close):
                close()
        authority.close()


def _cmd_verify(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, artifacts, spool = _components(config)
    try:
        report = LifecycleManager(
            authority=authority,
            artifact_store=artifacts,
            spool_store=spool,
            config=config,
        ).reference_integrity_scan(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
            verify_payload_hashes=not args.skip_payload_hashes,
        )
        _json(report)
        return 0 if report["valid"] else 3
    finally:
        for component in (spool, artifacts):
            close = getattr(component, "close", None)
            if callable(close):
                close()
        authority.close()


def _cmd_backfill_tool_chunks(config, args):
    from .tool_chunk_backfill import backfill_tool_chunks
    authority, artifacts, spool = _components(config)
    try:
        report = backfill_tool_chunks(authority=authority, artifacts=artifacts, spool=spool,
            config=config, tenant_id=args.tenant_id, task_id=args.task_id, apply=args.apply,
            cursor=args.cursor, limit=args.limit, model_name=args.model,
            max_source_bytes=args.max_source_bytes, timeout=args.timeout_seconds)
        _json(report)
        return 5 if report["errors"] else 0
    finally:
        for component in (spool, artifacts, authority):
            close = getattr(component, "close", None)
            if callable(close):
                close()


def _cmd_gc(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, artifacts, spool = _components(config)
    try:
        report = LifecycleManager(
            authority=authority,
            artifact_store=artifacts,
            spool_store=spool,
            config=config,
        ).gc_orphans(
            tenant_id=args.tenant_id,
            dry_run=not args.apply,
            grace_hours=args.grace_hours,
        )
        _json(report)
        return 0 if not report["errors"] else 4
    finally:
        authority.close()


def _cmd_replay_artifacts(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    from .service import PagedMemoryRuntime

    recovery_config = replace(
        config,
        enabled=True,
        shadow_write=False,
        tenant_id=args.tenant_id,
    )
    recovery_config.validate()
    runtime = PagedMemoryRuntime(
        config=recovery_config,
        cwd=Path(args.cwd),
        task_id=args.task_id,
        goal="recover pending artifact uploads",
    )
    try:
        replay = runtime.replay_pending_artifacts()
        integrity = LifecycleManager(
            authority=runtime.authority,
            artifact_store=runtime.artifact_store,
            spool_store=runtime.spool_store,
            config=recovery_config,
        ).reference_integrity_scan(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
        )
        _json({"replay": replay, "integrity": integrity})
        return 0 if replay["failed"] == 0 and integrity["valid"] else 5
    finally:
        runtime.close()


def _cmd_backup(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, artifacts, spool = _components(config)
    try:
        manager = LifecycleManager(
            authority=authority,
            artifact_store=artifacts,
            spool_store=spool,
            config=config,
        )
        backup = manager.create_local_backup(destination=args.destination)
        _json(manager.verify_local_backup(backup))
    finally:
        authority.close()
    return 0


def _cmd_restore_drill(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, artifacts, spool = _components(config)
    try:
        report = LifecycleManager(
            authority=authority,
            artifact_store=artifacts,
            spool_store=spool,
            config=config,
        ).restore_drill(args.backup)
        _json(report)
    finally:
        authority.close()
    return 0


def _cmd_export_markdown(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    from .markdown_export import export_markdown
    _json(export_markdown(config, tenant_id=args.tenant_id or config.tenant_id,
                          task_id=args.task_id, workspace_id=args.workspace_id))
    return 0


def _cmd_restore(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, artifacts, spool = _components(config)
    try:
        target = LifecycleManager(authority=authority, artifact_store=artifacts, spool_store=spool,
                                  config=config).restore_local_backup(args.backup, target_directory=args.target)
        _json({'restored': True, 'directory': str(target)})
    finally:
        authority.close()
    return 0


def _cmd_retention(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, artifacts, spool = _components(config)
    try:
        manager = LifecycleManager(
            authority=authority,
            artifact_store=artifacts,
            spool_store=spool,
            config=config,
        )
        retention_until = None
        if args.days is not None:
            import time

            retention_until = time.time() + args.days * 86400
        policy = manager.set_task_retention(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
            retention_until=retention_until,
            legal_hold=args.legal_hold,
            set_by=args.actor,
            deletion_mode=args.deletion_mode,
        )
        _json(policy.__dict__)
    finally:
        authority.close()
    return 0


def _cmd_delete(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, artifacts, spool = _components(
        config
    )
    try:
        report = LifecycleManager(
            authority=authority,
            artifact_store=artifacts,
            spool_store=spool,
            config=config,
        ).delete_task(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
            authorized=args.authorized,
            actor=args.actor,
            reason=args.reason,
        )
        _json(report)
    finally:
        authority.close()
    return 0


def _cmd_deletion_audit(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority = _authority(config)
    try:
        audit = authority.get_deletion_audit(request_id=args.request_id)
        _json(audit or {"found": False, "request_id": args.request_id})
        return 0 if audit is not None else 6
    finally:
        authority.close()


def _archive_components(config: PagedMemoryConfig) -> tuple[Any, EventArchiveManager]:
    authority = _authority(config)
    manager = EventArchiveManager(
        authority=authority,
        artifact_store=build_artifact_store(config),
        retention_rules=config.retention_rules,
        max_uncompressed_bytes=config.archive_max_uncompressed_bytes,
    )
    return authority, manager


def _cmd_archive_events(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, manager = _archive_components(config)
    try:
        report = manager.archive_range(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
            after=args.after,
            before=args.before,
            event_types=tuple(args.event_type or ()),
            max_events=args.max_events or config.archive_max_events,
            backup_class=args.backup_class,
            dry_run=not args.apply,
        )
        _json(report)
    finally:
        authority.close()
    return 0


def _cmd_archive_due(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, manager = _archive_components(config)
    try:
        report = manager.archive_due(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
            now=args.now,
            task_status=args.task_status,
            max_events=args.max_events or config.archive_max_events,
            dry_run=not args.apply,
        )
        _json(report)
    finally:
        authority.close()
    return 0


def _cmd_list_archives(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority = _authority(config)
    try:
        manifests = authority.list_archive_manifests(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
            statuses=tuple(args.status or ()),
        )
        _json(
            {
                "count": len(manifests),
                "archives": [manifest.__dict__ for manifest in manifests],
            }
        )
    finally:
        authority.close()
    return 0


def _cmd_verify_archive(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, manager = _archive_components(config)
    try:
        _json(
            manager.verify_archive(
                tenant_id=args.tenant_id,
                task_id=args.task_id,
                archive_id=args.archive_id,
            )
        )
    finally:
        authority.close()
    return 0


def _cmd_read_archive(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, manager = _archive_components(config)
    try:
        events = manager.read_archive_events(
            tenant_id=args.tenant_id,
            task_id=args.task_id,
            archive_id=args.archive_id,
            offset=args.offset,
            limit=args.limit,
        )
        _json({"count": len(events), "events": events})
    finally:
        authority.close()
    return 0


def _cmd_restore_archive(config: PagedMemoryConfig, args: argparse.Namespace) -> int:
    authority, manager = _archive_components(config)
    try:
        _json(
            manager.restore_archive(
                tenant_id=args.tenant_id,
                task_id=args.task_id,
                archive_id=args.archive_id,
                dry_run=not args.apply,
            )
        )
    finally:
        authority.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minicode-memory")
    parser.add_argument(
        "--cwd", default=".", help="workspace used to resolve local memory paths"
    )
    parser.add_argument(
        "--use-settings",
        action="store_true",
        help="load native MiniCode user settings; environment overrides still apply",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")

    setup = subparsers.add_parser("configure-local")
    setup.add_argument("--data-root", required=True, help="absolute data directory; no remote data migration")

    backfill = subparsers.add_parser("backfill")
    backfill.add_argument("--source", default=".mini-code-memory/memory.json")
    backfill.add_argument("--tenant-id", default="local")
    backfill.add_argument("--task-id", default="")
    backfill.add_argument("--limit", type=int, default=0)
    backfill.add_argument(
        "--since", type=float, default=None, help="inclusive created_at epoch"
    )
    backfill.add_argument(
        "--until", type=float, default=None, help="exclusive created_at epoch"
    )
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--continue-on-error", action="store_true")

    artifact_chunks = subparsers.add_parser("backfill-artifact-chunks")
    artifact_chunks.add_argument("--tenant-id", required=True)
    artifact_chunks.add_argument("--task-id", required=True)
    artifact_chunks.add_argument(
        "--apply",
        action="store_true",
        help="write missing chunks; default is dry-run",
    )

    tool_chunks = subparsers.add_parser("backfill-tool-chunks", help="resume bounded inline/artifact search-index repair")
    tool_chunks.add_argument("--tenant-id", required=True)
    tool_chunks.add_argument("--task-id", required=True)
    tool_chunks.add_argument("--apply", action="store_true", help="write missing derived cards; default is dry-run")
    tool_chunks.add_argument("--limit", type=int, default=25, help="maximum source events in this batch (1-100)")
    tool_chunks.add_argument("--cursor", default="", help="next_cursor from a previous batch with the same mode/scope")
    tool_chunks.add_argument("--model", default="", help="model used for capture-time chunk token estimates")
    tool_chunks.add_argument("--max-source-bytes", type=int, default=8 * 1024 * 1024,
                             help="per-source byte budget; increase explicitly for large originals (max 64 MiB)")
    tool_chunks.add_argument("--timeout-seconds", type=float, default=10.0,
                             help="batch deadline, checked between sources and database operations (max 60)")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--tenant-id", required=True)
    verify.add_argument("--task-id", required=True)
    verify.add_argument("--skip-payload-hashes", action="store_true")

    gc = subparsers.add_parser("gc-artifacts")
    gc.add_argument("--tenant-id", required=True)
    gc.add_argument("--grace-hours", type=int, default=None)
    gc.add_argument(
        "--apply", action="store_true", help="delete candidates; default is dry-run"
    )

    replay_artifacts = subparsers.add_parser("replay-artifacts")
    replay_artifacts.add_argument("--tenant-id", required=True)
    replay_artifacts.add_argument("--task-id", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--destination", default=None)

    drill = subparsers.add_parser("restore-drill")
    drill.add_argument("backup")

    restore = subparsers.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("--target", required=True, help="new or empty directory; never overwrites live data")

    export = subparsers.add_parser("export-markdown")
    export.add_argument("--tenant-id", default=None)
    export.add_argument("--task-id", default=None)
    export.add_argument("--workspace-id", default=None)

    retention = subparsers.add_parser("set-retention")
    retention.add_argument("--tenant-id", required=True)
    retention.add_argument("--task-id", required=True)
    retention.add_argument("--days", type=int, default=None)
    retention.add_argument("--legal-hold", action="store_true")
    retention.add_argument(
        "--deletion-mode", choices=("HARD_DELETE", "TOMBSTONE"), default="HARD_DELETE"
    )
    retention.add_argument("--actor", required=True)

    delete = subparsers.add_parser("delete-task")
    delete.add_argument("--tenant-id", required=True)
    delete.add_argument("--task-id", required=True)
    delete.add_argument("--authorized", action="store_true")
    delete.add_argument("--actor", required=True)
    delete.add_argument("--reason", required=True)

    deletion_audit = subparsers.add_parser("deletion-audit")
    deletion_audit.add_argument("--request-id", required=True)

    archive_events = subparsers.add_parser("archive-events")
    archive_events.add_argument("--tenant-id", required=True)
    archive_events.add_argument("--task-id", required=True)
    archive_events.add_argument("--after", type=float, default=None)
    archive_events.add_argument("--before", type=float, default=None)
    archive_events.add_argument("--event-type", action="append", default=[])
    archive_events.add_argument("--max-events", type=int, default=None)
    archive_events.add_argument("--backup-class", default="STANDARD")
    archive_events.add_argument(
        "--apply", action="store_true", help="create archive; default is dry-run"
    )

    archive_due = subparsers.add_parser("archive-due")
    archive_due.add_argument("--tenant-id", required=True)
    archive_due.add_argument("--task-id", required=True)
    archive_due.add_argument("--now", type=float, default=None)
    archive_due.add_argument("--task-status", default="")
    archive_due.add_argument("--max-events", type=int, default=None)
    archive_due.add_argument(
        "--apply", action="store_true", help="create due archives; default is dry-run"
    )

    list_archives = subparsers.add_parser("list-archives")
    list_archives.add_argument("--tenant-id", required=True)
    list_archives.add_argument("--task-id", required=True)
    list_archives.add_argument("--status", action="append", default=[])

    verify_archive = subparsers.add_parser("verify-archive")
    verify_archive.add_argument("--tenant-id", required=True)
    verify_archive.add_argument("--task-id", required=True)
    verify_archive.add_argument("--archive-id", required=True)

    read_archive = subparsers.add_parser("read-archive")
    read_archive.add_argument("--tenant-id", required=True)
    read_archive.add_argument("--task-id", required=True)
    read_archive.add_argument("--archive-id", required=True)
    read_archive.add_argument("--offset", type=int, default=0)
    read_archive.add_argument("--limit", type=int, default=100)

    restore_archive = subparsers.add_parser("restore-archive")
    restore_archive.add_argument("--tenant-id", required=True)
    restore_archive.add_argument("--task-id", required=True)
    restore_archive.add_argument("--archive-id", required=True)
    restore_archive.add_argument(
        "--apply",
        action="store_true",
        help="restore missing events; default is dry-run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'configure-local':
        from .local_setup import configure_local_storage
        try:
            _json(configure_local_storage(args.data_root))
            return 0
        except Exception as exc:
            print(f'minicode-memory: {type(exc).__name__}: {exc}', file=sys.stderr)
            return 1
    cwd = Path(args.cwd).resolve()
    runtime = None
    if args.use_settings:
        from minicode.config import load_effective_settings

        runtime = load_effective_settings(cwd)
    config = PagedMemoryConfig.from_sources(runtime=runtime, cwd=cwd)
    commands = {
        "init-db": _cmd_init,
        "backfill": _cmd_backfill,
        "backfill-artifact-chunks": _cmd_backfill_artifact_chunks,
        "backfill-tool-chunks": _cmd_backfill_tool_chunks,
        "verify": _cmd_verify,
        "gc-artifacts": _cmd_gc,
        "replay-artifacts": _cmd_replay_artifacts,
        "backup": _cmd_backup,
        "restore-drill": _cmd_restore_drill,
        "restore": _cmd_restore,
        "export-markdown": _cmd_export_markdown,
        "set-retention": _cmd_retention,
        "delete-task": _cmd_delete,
        "deletion-audit": _cmd_deletion_audit,
        "archive-events": _cmd_archive_events,
        "archive-due": _cmd_archive_due,
        "list-archives": _cmd_list_archives,
        "verify-archive": _cmd_verify_archive,
        "read-archive": _cmd_read_archive,
        "restore-archive": _cmd_restore_archive,
    }
    try:
        return commands[args.command](config, args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders a stable exit code.
        print(f"minicode-memory: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
