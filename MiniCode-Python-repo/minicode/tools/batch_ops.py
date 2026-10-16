from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from minicode.tooling import ToolDefinition, ToolContext, ToolResult
from minicode.tools._validation import validate_contract
from minicode.workspace import resolve_tool_path


BATCH_DELETE_INPUT = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "minLength": 1,
                 "description": "Path to delete (relative to workspace)"},
        "recursive": {"type": "boolean", "default": False,
                      "description": "Directories require boolean true. Omit or use false for files; never use a quoted string."},
    },
    "required": ["path"],
}


def _remove_copy_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _copy_to_staging(source: Path, staging: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, staging)
    else:
        shutil.copy2(source, staging)


def _replace_with_staged_copy(source: Path, destination: Path) -> None:
    """Copy fully before replacing destination, restoring it if the swap fails."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging = destination.parent / f".{destination.name}.copy-{token}"
    backup = destination.parent / f".{destination.name}.backup-{token}"
    moved_existing = False
    try:
        _copy_to_staging(source, staging)
        if destination.exists() or destination.is_symlink():
            destination.replace(backup)
            moved_existing = True
        try:
            staging.replace(destination)
        except Exception:
            if moved_existing and backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if moved_existing:
            _remove_copy_path(backup)
    finally:
        if staging.exists() or staging.is_symlink():
            _remove_copy_path(staging)


def _validate_batch_copy(input_data: dict) -> dict:
    """Validate input for batch_copy tool."""
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination is required")
    return {"source": source.strip(), "destination": destination.strip()}


def _run_batch_copy(input_data: dict, context: ToolContext) -> ToolResult:
    """Copy files or directories."""
    try:
        source = resolve_tool_path(context, input_data["source"], "read")
        destination = resolve_tool_path(context, input_data["destination"], "write")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))
    
    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")
    
    try:
        _replace_with_staged_copy(source, destination)
        kind = "directory" if source.is_dir() else "file"
        return ToolResult(ok=True, output=f"Copied {kind} to {input_data['destination']}")
    except Exception as e:
        return ToolResult(ok=False, output=f"Copy failed: {e}")


batch_copy_tool = ToolDefinition(
    name="batch_copy",
    description="Copy files or directories. Supports both files and directories.",
    input_schema={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source path (relative to workspace)"},
            "destination": {"type": "string", "description": "Destination path (relative to workspace)"}
        },
        "required": ["source", "destination"]
    },
    validator=_validate_batch_copy,
    run=_run_batch_copy,
)


# ---------------------------------------------------------------------------
# Batch Move Tool
# ---------------------------------------------------------------------------

def _validate_batch_move(input_data: dict) -> dict:
    """Validate input for batch_move tool."""
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination is required")
    return {"source": source.strip(), "destination": destination.strip()}


def _run_batch_move(input_data: dict, context: ToolContext) -> ToolResult:
    """Move files or directories."""
    try:
        source = resolve_tool_path(context, input_data["source"], "read")
        destination = resolve_tool_path(context, input_data["destination"], "write")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))
    
    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")
    
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return ToolResult(ok=True, output=f"Moved to {input_data['destination']}")
    except Exception as e:
        return ToolResult(ok=False, output=f"Move failed: {e}")


batch_move_tool = ToolDefinition(
    name="batch_move",
    description="Move files or directories to a new location.",
    input_schema={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source path (relative to workspace)"},
            "destination": {"type": "string", "description": "Destination path (relative to workspace)"}
        },
        "required": ["source", "destination"]
    },
    validator=_validate_batch_move,
    run=_run_batch_move,
)


# ---------------------------------------------------------------------------
# Batch Delete Tool
# ---------------------------------------------------------------------------

def _validate_batch_delete(input_data: dict) -> dict:
    """Validate input for batch_delete tool."""
    validate_contract(input_data, BATCH_DELETE_INPUT)
    path = input_data.get("path", "")
    if not path.strip():
        raise ValueError("$.path: must be a nonblank path")
    return {"path": path.strip(), "recursive": input_data.get("recursive", False)}


def _run_batch_delete(input_data: dict, context: ToolContext) -> ToolResult:
    """Delete files or directories."""
    try:
        target = resolve_tool_path(context, input_data["path"], "delete")
    except (PermissionError, RuntimeError) as error:
        return ToolResult(ok=False, output=str(error))
    recursive = input_data.get("recursive", False)
    
    if not target.exists():
        return ToolResult(ok=False, output=f"Path not found: {input_data['path']}")
    
    try:
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
                return ToolResult(ok=True, output=f"Deleted directory: {input_data['path']}")
            else:
                return ToolResult(ok=False, output="Use recursive=true to delete directories")
        else:
            target.unlink()
            return ToolResult(ok=True, output=f"Deleted file: {input_data['path']}")
    except Exception as e:
        return ToolResult(ok=False, output=f"Delete failed: {e}")


batch_delete_tool = ToolDefinition(
    name="batch_delete",
    description="Delete files or directories. Directories require recursive=true.",
    input_schema=BATCH_DELETE_INPUT,
    validator=_validate_batch_delete,
    run=_run_batch_delete,
)
