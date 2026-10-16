"""Full current candidate text for a live, explicitly bound primary Worker.

This is a read-only snapshot, not a tool receipt, a check, or permission to
submit. Unknown changes, aliases, binary/large files and races fail closed.
"""
from pathlib import Path
import hashlib

from minicode.path_safety import is_unsafe_stored_path
from minicode.workspace_provider import WorkspaceConflict, snapshot_revision, workspace_snapshot

CANDIDATE_BYTES_LIMIT = 32768


def capture_candidate(workspace, base_hashes, paths, *, current_revision):
    revision = current_revision()  # Worker revalidates lease, scope and reading gates.
    root = Path(workspace).resolve()
    current = workspace_snapshot(root)
    if snapshot_revision(current) != revision:
        raise WorkspaceConflict("candidate changed before reading its working snapshot")
    requested = {}
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            raise WorkspaceConflict("candidate snapshot needs native write paths")
        path = root / raw
        if is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True) or not path.is_file():
            raise WorkspaceConflict("candidate snapshot cannot read an unsafe or missing file")
        relative = path.resolve().relative_to(root).as_posix()
        requested[relative] = path
    changed = {name for name in current.keys() | base_hashes.keys() if current.get(name) != base_hashes.get(name)}
    if not changed or not changed <= requested.keys():
        raise WorkspaceConflict("candidate contains changes without observed native write paths")
    records, remaining = [], CANDIDATE_BYTES_LIMIT
    for name, path in sorted(requested.items()):
        with path.open("rb") as stream:
            raw = stream.read(remaining + 1)
        if len(raw) > remaining or hashlib.sha256(raw).hexdigest() != current.get(name):
            raise WorkspaceConflict("candidate working snapshot is oversized or changed")
        records.append({"path": name, "sha256": current[name], "size_bytes": len(raw), "content": raw.decode("utf-8")})
        remaining -= len(raw)
    if current_revision() != revision:
        raise WorkspaceConflict("candidate changed while reading its working snapshot")
    return {"source": "current_runtime_snapshot", "workspace_revision": revision,
        "changed_paths": sorted(changed), "files": records,
        "check_status": "not_run_for_current_revision", "counts_as_acceptance": False}
