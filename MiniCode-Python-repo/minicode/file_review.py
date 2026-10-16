from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path

from minicode.session import create_file_checkpoint
from minicode.tooling import ToolContext, ToolResult


def build_unified_diff(file_path: str, before: str, after: str) -> str:
    if before == after:
        return f"(no changes for {file_path})"
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="",
        n=3,
    )
    # Strip redundant separator lines (e.g. "=" lines) for compact display
    lines = [line for line in diff if not (line.startswith("=") and set(line.strip()) == {"="})]
    return "\n".join(lines)


def load_existing_file(target_path: str | Path) -> str:
    file_path = Path(target_path)
    if not file_path.exists():
        return ""
    return file_path.read_text(encoding="utf-8")


def apply_reviewed_file_change(
    context: ToolContext,
    file_path: str,
    target_path: str | Path,
    next_content: str,
) -> ToolResult:
    target = Path(target_path)
    previous_content = load_existing_file(target)
    if target.exists() and previous_content == next_content:
        return ToolResult(ok=True, output=f"No changes needed for {file_path}")

    diff = (f"Create empty file: {file_path}" if not target.exists() and next_content == ""
            else build_unified_diff(file_path, previous_content, next_content))
    if context.permissions is not None:
        context.permissions.ensure_edit(str(target), diff)

    if context.cancellation_event is not None and context.cancellation_event.is_set():
        return ToolResult(ok=False, output="Canceled: file edit canceled before writing", failureKind="canceled")

    create_file_checkpoint(
        context.session,
        file_path=str(target),
        existed=target.exists(),
        previous_content=previous_content,
    )
    from minicode.native_read_set import current_native_read_set

    reads = current_native_read_set()
    creation = reads.prepare_file_creation(target) if reads is not None and not target.exists() else {}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(next_content, encoding="utf-8")
    except OSError:
        if reads is not None and creation:
            reads.record_dynamic("file-creation:write-incomplete")
        raise
    if reads is not None and creation:
        reads.finish_file_creation(creation)
    revision = hashlib.sha256((os.path.normcase(str(target.resolve())) + "\0" + next_content).encode("utf-8")).hexdigest()
    return ToolResult(ok=True, output=f"Applied reviewed changes to {file_path}", contextWriteRevision=revision)
