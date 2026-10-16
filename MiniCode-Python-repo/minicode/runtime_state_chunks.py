"""Content-verified pages for large Runtime snapshots.

The small JSON manifest is the only commit point. Pages are durable before it
is replaced. Readers and reclamation share the state store's commit lock.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from minicode.path_safety import is_unsafe_stored_path

FILE_SCHEMA = "minicode-runtime-file-v2"
MIN_STATE_BYTES = 65_536
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _same(left, right):
    # Python equality alone treats True, 1, and 1.0 as interchangeable. Their
    # JSON types are part of the persisted contract and must not be cached away.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class StateChunks:
    def __init__(self, state_path):
        self.path = Path(state_path)
        self.root = self.path.with_name(self.path.name + ".parts")
        self.cache = {}
        self.written_bytes = 0
        self.validated = set()

    def _safe(self, path):
        if is_unsafe_stored_path(path, boundary=self.path.parent, reject_hardlinks=True):
            raise ValueError("Runtime state page escapes or aliases its store")

    def _owner(self, run_id, *, create=False):
        self._safe(self.root)
        marker = self.root / "owner.json"
        expected = {"schema_version": "minicode-runtime-pages-v1", "state_file": self.path.name, "run_id": run_id}
        if create and not self.root.exists():
            self.root.mkdir()
            with marker.open("xb") as handle:
                handle.write(encode(expected))
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(self.root)
        metadata = marker.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or json.loads(marker.read_bytes()) != expected):
            raise ValueError("Runtime state pages have no matching owner")

    def _read_page(self, digest, size=None):
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise ValueError("invalid Runtime state page hash")
        path = self.root / (digest + ".json")
        # _owner validates the directory chain once for this observation. The
        # fixed hash filename has no path components; inspect its leaf without
        # resolving that same long directory chain again for every page.
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or (size is not None and metadata.st_size != size)):
            raise ValueError("Runtime state page has the wrong type or size")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Runtime state page failed integrity validation")
        return raw

    def _write_page(self, raw):
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / (digest + ".json")
        self._safe(path)
        if path.exists():
            self._read_page(digest, len(raw))
        else:
            temporary = self.root / ("." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                self.written_bytes += len(raw)
            finally:
                temporary.unlink(missing_ok=True)
        return {"kind": "page", "sha256": digest, "size": len(raw)}

    def pack(self, payload):
        self.written_bytes = 0
        self._owner(payload["run_id"], create=True)
        pending = {}

        def leaf(slot, value):
            previous = self.cache.get(slot)
            if previous is not None and _same(previous[0], value):
                node = previous[1]
                if node["kind"] == "page" and node["sha256"] not in self.validated:
                    # A previous failed commit can leave an unreferenced page
                    # in the encoding cache. It has no live-manifest receipt.
                    self._read_page(node["sha256"], node["size"])
                pending[slot] = previous
                return previous[1]
            raw = encode(value)
            node = ({"kind": "value", "value": json.loads(raw)} if len(raw) < 8_192
                    else self._write_page(raw))
            pending[slot] = (json.loads(raw), node)
            return node

        def contains_large_collection(value):
            if isinstance(value, list):
                return len(value) > 64
            if isinstance(value, dict):
                return len(value) > 16 or any(contains_large_collection(item) for item in value.values())
            return False

        def pack_value(slot, value, depth=0):
            if isinstance(value, list) and len(value) > 64:
                return {"kind": "list", "groups": [leaf((*slot, index), value[index:index + 64])
                    for index in range(0, len(value), 64)]}
            if isinstance(value, dict) and len(value) > 16 and depth < 64:
                # Hash buckets avoid shifting every page when a random command
                # or node ID is inserted before existing dictionary keys.
                groups = [{} for _ in range(16)]
                for name, item in value.items():
                    bucket = int(hashlib.sha256(name.encode("utf-8")).hexdigest()[depth], 16)
                    groups[bucket][name] = item
                return {"kind": "map", "groups": [
                    pack_value((*slot, index), group, depth + 1) if len(group) > 64 or any(
                        contains_large_collection(item) for item in group.values()) else leaf((*slot, index), group)
                    for index, group in enumerate(groups) if group]}
            if isinstance(value, dict) and any(contains_large_collection(item) for item in value.values()):
                # Ledger.calls, node attempts and checkpoints grow inside small
                # parent objects too. A small wrapper must not force those
                # histories back into one rewritten page.
                return {"kind": "object", "fields": {name: pack_value((*slot, name), item)
                    for name, item in value.items()}}
            return leaf(slot, value)

        fields = {key: pack_value((key,), value) for key, value in payload.items()}
        manifest = {"schema_version": FILE_SCHEMA, "run_id": payload["run_id"],
                    "state_revision": payload["state_revision"], "fields": fields}
        manifest["manifest_sha256"] = hashlib.sha256(encode(manifest)).hexdigest()
        sync_directory(self.root)
        self.cache = pending
        return manifest

    @staticmethod
    def references(manifest):
        if (set(manifest) != {"schema_version", "run_id", "state_revision", "fields", "manifest_sha256"}
                or manifest.get("schema_version") != FILE_SCHEMA or not isinstance(manifest["run_id"], str)
                or type(manifest["state_revision"]) is not int or manifest["state_revision"] < 0
                or not isinstance(manifest["fields"], dict)):
            raise ValueError("invalid Runtime state manifest")
        body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        if hashlib.sha256(encode(body)).hexdigest() != manifest["manifest_sha256"]:
            raise ValueError("Runtime state manifest failed integrity validation")
        refs = {}

        def visit(node):
            if not isinstance(node, dict):
                raise ValueError("invalid Runtime state page descriptor")
            kind = node.get("kind")
            if kind == "page" and set(node) == {"kind", "sha256", "size"}:
                digest, size = node["sha256"], node["size"]
                if not isinstance(digest, str) or not _HASH.fullmatch(digest) or type(size) is not int or size < 0:
                    raise ValueError("invalid Runtime state page reference")
                if digest in refs and refs[digest] != size:
                    raise ValueError("conflicting Runtime state page sizes")
                refs[digest] = size
            elif kind == "value" and set(node) == {"kind", "value"}:
                pass
            elif kind in {"map", "list"} and set(node) == {"kind", "groups"} and isinstance(node["groups"], list):
                for child in node["groups"]:
                    visit(child)
            elif kind == "object" and set(node) == {"kind", "fields"} and isinstance(node["fields"], dict):
                for child in node["fields"].values():
                    visit(child)
            else:
                raise ValueError("invalid Runtime state page descriptor")
        for node in manifest["fields"].values():
            visit(node)
        return refs

    def validate(self, manifest):
        self.validated = set()
        refs = self.references(manifest)
        self._owner(manifest["run_id"])
        # Fresh bytes on every save/load, including unchanged pages. The cache
        # only saves serialization work; mtime is never an integrity receipt.
        pages = {digest: self._read_page(digest, size) for digest, size in refs.items()}
        self.validated = set(refs)
        return pages

    def read(self, manifest):
        pages = self.validate(manifest)

        def expand(node):
            if node["kind"] == "object":
                return {name: expand(child) for name, child in node["fields"].items()}
            if node["kind"] in {"map", "list"}:
                groups = [expand(child) for child in node["groups"]]
                expected = dict if node["kind"] == "map" else list
                if any(not isinstance(group, expected) for group in groups):
                    raise ValueError("Runtime state page has the wrong container type")
                if expected is dict:
                    value = {}
                    for group in groups:
                        if value.keys() & group.keys():
                            raise ValueError("duplicate Runtime state map key")
                        value.update(group)
                else:
                    value = [item for group in groups for item in group]
                return value
            return json.loads(pages[node["sha256"]]) if node["kind"] == "page" else node["value"]

        payload = {key: expand(node) for key, node in manifest["fields"].items()}
        if (payload.get("run_id") != manifest["run_id"] or payload.get("state_revision") != manifest["state_revision"]
                or not re.fullmatch(r"minicode-runtime-state-v(?:[3-9]|1[0-9]|20)", str(payload.get("schema_version")))):
            raise ValueError("Runtime state pages do not match their manifest")
        return payload

    def prune(self, run_id, retained):
        """Called under the commit lock, after the new manifest is durable."""
        self._owner(run_id)
        for path in self.root.iterdir():
            if path.suffix == ".json" and _HASH.fullmatch(path.stem) and path.stem not in retained:
                self._read_page(path.stem)
                path.unlink()

    def inventory(self, run_id):
        self._safe(self.root)
        if not self.root.exists():
            return []
        paths = list(self.root.iterdir())
        if not paths:
            return []  # Explicit deletion may have stopped after removing its marker.
        self._owner(run_id)
        for path in paths:
            if path.name == "owner.json":
                continue
            if path.suffix != ".json" or not _HASH.fullmatch(path.stem):
                raise ValueError("Runtime state pages contain an unrecognized file")
            self._read_page(path.stem)
        return paths

    def remove(self, run_id):
        """The existing explicit run collector owns deletion, never age alone."""
        paths = self.inventory(run_id)
        if not paths:
            if self.root.exists():
                self.root.rmdir()
            return
        for path in paths:
            if path.name != "owner.json":
                path.unlink()
        (self.root / "owner.json").unlink()
        self.root.rmdir()
