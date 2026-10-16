from __future__ import annotations

import gzip
import os
import shutil
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

from minicode.path_safety import is_link_or_reparse
from minicode.tooling import ToolDefinition, ToolContext, ToolResult
from minicode.tools._validation import validate_fields
from minicode.workspace import resolve_tool_path


class _ArchiveWriteError(RuntimeError):
    """An archive operation failed after entering its filesystem write phase."""


@contextmanager
def _writing_archive():
    try:
        yield
    except Exception as error:
        raise _ArchiveWriteError(str(error)) from error


def _archive_error(prefix: str, error: Exception) -> ToolResult:
    uncertain = isinstance(error, _ArchiveWriteError)
    kind = ("permission_denied" if isinstance(error, PermissionError) else
            "invalid_input" if isinstance(error, ValueError) else
            "not_found" if isinstance(error, FileNotFoundError) else None)
    return ToolResult(ok=False, output=f"{prefix}: {error}" + (
        "\nOutput may be incomplete or overwritten; inspect it before another attempt." if uncertain else ""),
        ambiguousSideEffect=uncertain, failureKind=kind)


def _ensure_distinct_files(source: Path, destination: Path) -> None:
    if source == destination or (source.exists() and destination.exists() and source.samefile(destination)):
        raise ValueError("Source and destination must be different files")


def _archive_sources(context: ToolContext, source: Path, destination: Path) -> list[Path]:
    """Validate the complete input tree before opening an output archive.

    Do not follow directory aliases, include the output in its own archive, or
    allow a descendant file to bypass the normal workspace read boundary.
    """
    _ensure_distinct_files(source, destination)
    paths = [source]
    if not source.is_dir():
        return paths

    def unreadable_directory(error: OSError) -> None:
        # A partial archive must not be reported as a complete input snapshot.
        raise error

    for parent, directories, files in os.walk(source, followlinks=False, onerror=unreadable_directory):
        directories.sort()
        for name in [*directories, *sorted(files)]:
            item = Path(parent) / name
            if is_link_or_reparse(item):
                raise ValueError(f"Archive source contains an unsupported filesystem link: {item}")
            checked = resolve_tool_path(context, str(item), "read")
            if checked == destination:
                continue
            _ensure_distinct_files(checked, destination)
            paths.append(checked)
    return paths


def _resolve_archive_member(destination: Path, member_name: str) -> Path:
    normalized_name = member_name.replace("\\", "/")
    member_path = Path(normalized_name)
    if member_path.is_absolute() or any(part == ".." for part in member_path.parts):
        raise ValueError(f"Archive member escapes extraction destination: {member_name}")

    destination_root = destination.resolve()
    target = (destination_root / member_path).resolve()
    try:
        target.relative_to(destination_root)
    except ValueError as error:
        raise ValueError(f"Archive member escapes extraction destination: {member_name}") from error
    return target


def _safe_extract_zip(source: Path, destination: Path, context: ToolContext) -> None:
    with zipfile.ZipFile(source, "r") as zf:
        members = []
        for info in zf.infolist():
            target = _resolve_archive_member(destination, info.filename)
            target = resolve_tool_path(context, str(target), "write")
            _ensure_distinct_files(source, target)
            members.append((info, target))
        with _writing_archive():
            destination.mkdir(parents=True, exist_ok=True)
            for info, target in members:
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def _safe_extract_tar(source: Path, destination: Path, context: ToolContext) -> None:
    with tarfile.open(source, "r:*") as tar:
        members = []
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"Archive member uses unsupported link: {member.name}")
            target = _resolve_archive_member(destination, member.name)
            target = resolve_tool_path(context, str(target), "write")
            _ensure_distinct_files(source, target)
            members.append((member, target))
        with _writing_archive():
            destination.mkdir(parents=True, exist_ok=True)
            for member, target in members:
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    continue
                with src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


# ---------------------------------------------------------------------------
# Gzip Compress
# ---------------------------------------------------------------------------

def _validate_gzip_compress(input_data: dict) -> dict:
    validate_fields(input_data, required=("source", "destination"), source=str, destination=str)
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination is required")
    return {"source": source.strip(), "destination": destination.strip()}


def _run_gzip_compress(input_data: dict, context: ToolContext) -> ToolResult:
    source = resolve_tool_path(context, input_data["source"], "read")
    destination = resolve_tool_path(context, input_data["destination"], "write")
    
    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")
    
    try:
        _ensure_distinct_files(source, destination)
        if source.is_file():
            with open(source, "rb") as f_in:
                with _writing_archive(), gzip.open(destination, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            return ToolResult(ok=True, output=f"Compressed to {input_data['destination']}")
        else:
            return ToolResult(ok=False, output="Use tar_create for directories")
    except Exception as e:
        return _archive_error("Compression error", e)


gzip_compress_tool = ToolDefinition(
    name="gzip_compress",
    description="Compress a file using gzip.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {"type": "string", "description": "Source file (relative to workspace)"},
            "destination": {"type": "string", "description": "Output .gz file path"}
        },
        "required": ["source", "destination"]
    },
    validator=_validate_gzip_compress,
    run=_run_gzip_compress,
)


# ---------------------------------------------------------------------------
# Gzip Decompress
# ---------------------------------------------------------------------------

def _validate_gzip_decompress(input_data: dict) -> dict:
    validate_fields(input_data, required=("source", "destination"), source=str, destination=str)
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination is required")
    return {"source": source.strip(), "destination": destination.strip()}


def _run_gzip_decompress(input_data: dict, context: ToolContext) -> ToolResult:
    source = resolve_tool_path(context, input_data["source"], "read")
    destination = resolve_tool_path(context, input_data["destination"], "write")
    
    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")
    
    try:
        _ensure_distinct_files(source, destination)
        with gzip.open(source, "rb") as f_in:
            with _writing_archive(), open(destination, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        return ToolResult(ok=True, output=f"Decompressed to {input_data['destination']}")
    except Exception as e:
        return _archive_error("Decompression error", e)


gzip_decompress_tool = ToolDefinition(
    name="gzip_decompress",
    description="Decompress a .gz file.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {"type": "string", "description": "Source .gz file"},
            "destination": {"type": "string", "description": "Output file path"}
        },
        "required": ["source", "destination"]
    },
    validator=_validate_gzip_decompress,
    run=_run_gzip_decompress,
)


# ---------------------------------------------------------------------------
# Tar Archive
# ---------------------------------------------------------------------------

def _validate_tar_create(input_data: dict) -> dict:
    validate_fields(input_data, required=("source", "destination"), source=str, destination=str, mode=str)
    if input_data.get("mode", "gz") not in {"gz", "bz2", "xz", "none"}:
        raise ValueError("mode must be gz, bz2, xz or none")
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    mode = input_data.get("mode", "gz")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination is required")
    return {"source": source.strip(), "destination": destination.strip(), "mode": mode}


def _run_tar_create(input_data: dict, context: ToolContext) -> ToolResult:
    source = resolve_tool_path(context, input_data["source"], "read")
    destination = Path(input_data["destination"])
    mode = input_data.get("mode", "gz")
    
    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")
    
    try:
        # Determine mode
        if mode == "gz":
            tar_mode = "w:gz"
            ext = ".tar.gz"
        elif mode == "bz2":
            tar_mode = "w:bz2"
            ext = ".tar.bz2"
        elif mode == "xz":
            tar_mode = "w:xz"
            ext = ".tar.xz"
        else:
            tar_mode = "w"
            ext = ".tar"
        
        # Ensure destination has correct extension
        if not str(destination).endswith(ext):
            destination = Path(str(destination) + ext)
        # Check the final path, including the automatically added extension.
        destination = resolve_tool_path(context, str(destination), "write")
        sources = _archive_sources(context, source, destination)
        with _writing_archive(), tarfile.open(destination, tar_mode) as tar:
            for item in sources:
                tar.add(item, arcname=str(item.relative_to(source.parent)), recursive=False)
        
        return ToolResult(ok=True, output=f"Created {destination.name}")
    except Exception as e:
        return _archive_error("Archive error", e)


tar_create_tool = ToolDefinition(
    name="tar_create",
    description="Create tar archive (optionally compressed with gz, bz2, or xz).",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {"type": "string", "description": "File or directory to archive"},
            "destination": {"type": "string", "description": "Output archive path"},
            "mode": {"type": "string", "enum": ["gz", "bz2", "xz", "none"], "description": "Compression: gz, bz2, xz, or none"}
        },
        "required": ["source", "destination"]
    },
    validator=_validate_tar_create,
    run=_run_tar_create,
)


# ---------------------------------------------------------------------------
# Tar Extract
# ---------------------------------------------------------------------------

def _validate_tar_extract(input_data: dict) -> dict:
    validate_fields(input_data, required=("source",), source=str, destination=str)
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    return {"source": source.strip(), "destination": destination.strip() if destination else ""}


def _run_tar_extract(input_data: dict, context: ToolContext) -> ToolResult:
    source = resolve_tool_path(context, input_data["source"], "read")
    dest_dir = input_data.get("destination", "")
    
    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")
    
    try:
        if dest_dir:
            destination = resolve_tool_path(context, dest_dir, "write")
        else:
            # Extract to same directory as archive
            destination = resolve_tool_path(context, str(source.parent / source.stem), "write")
        
        _safe_extract_tar(source, destination, context)
        
        return ToolResult(ok=True, output=f"Extracted to {destination}")
    except Exception as e:
        return _archive_error("Extract error", e)


tar_extract_tool = ToolDefinition(
    name="tar_extract",
    description="Extract tar archive.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {"type": "string", "description": "Archive file to extract"},
            "destination": {"type": "string", "description": "Output directory (optional)"}
        },
        "required": ["source"]
    },
    validator=_validate_tar_extract,
    run=_run_tar_extract,
)


# ---------------------------------------------------------------------------
# Zip
# ---------------------------------------------------------------------------

def _validate_zip_create(input_data: dict) -> dict:
    validate_fields(input_data, required=("source", "destination"), source=str, destination=str)
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("destination is required")
    return {"source": source.strip(), "destination": destination.strip()}


def _run_zip_create(input_data: dict, context: ToolContext) -> ToolResult:
    source = resolve_tool_path(context, input_data["source"], "read")
    destination = Path(input_data["destination"])
    
    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")
    
    try:
        if not str(destination).endswith(".zip"):
            destination = Path(str(destination) + ".zip")
        destination = resolve_tool_path(context, str(destination), "write")
        sources = _archive_sources(context, source, destination)
        with _writing_archive(), zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sources:
                zf.write(item, item.relative_to(source.parent))
        
        return ToolResult(ok=True, output=f"Created {destination.name}")
    except Exception as e:
        return _archive_error("Zip error", e)


zip_create_tool = ToolDefinition(
    name="zip_create",
    description="Create ZIP archive.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {"type": "string", "description": "File or directory to archive"},
            "destination": {"type": "string", "description": "Output .zip path"}
        },
        "required": ["source", "destination"]
    },
    validator=_validate_zip_create,
    run=_run_zip_create,
)


# ---------------------------------------------------------------------------
# Zip Extract
# ---------------------------------------------------------------------------

def _validate_zip_extract(input_data: dict) -> dict:
    validate_fields(input_data, required=("source",), source=str, destination=str)
    source = input_data.get("source", "")
    destination = input_data.get("destination", "")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    return {"source": source.strip(), "destination": destination.strip() if destination else ""}


def _run_zip_extract(input_data: dict, context: ToolContext) -> ToolResult:
    source = resolve_tool_path(context, input_data["source"], "read")
    dest_dir = input_data.get("destination", "")
    
    if not source.exists():
        return ToolResult(ok=False, output=f"Source not found: {input_data['source']}")
    
    try:
        if dest_dir:
            destination = resolve_tool_path(context, dest_dir, "write")
        else:
            destination = resolve_tool_path(context, str(source.parent / source.stem), "write")
        
        _safe_extract_zip(source, destination, context)
        
        return ToolResult(ok=True, output=f"Extracted to {destination}")
    except Exception as e:
        return _archive_error("Extract error", e)


zip_extract_tool = ToolDefinition(
    name="zip_extract",
    description="Extract ZIP archive.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": {"type": "string", "description": "Archive file to extract"},
            "destination": {"type": "string", "description": "Output directory (optional)"}
        },
        "required": ["source"]
    },
    validator=_validate_zip_extract,
    run=_run_zip_extract,
)
