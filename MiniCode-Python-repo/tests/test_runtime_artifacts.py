from __future__ import annotations

import os

import pytest

from minicode.runtime_artifacts import ArtifactIntegrityError, RuntimeArtifactStore


def test_large_artifact_round_trips_after_store_restart(tmp_path) -> None:
    root = tmp_path / "artifacts"
    content = "x" * 9_000 + "TAIL-SENTINEL"
    first = RuntimeArtifactStore(root)
    ref = first.put_text(content, run_id="run-1", node_id="node-1")

    second = RuntimeArtifactStore(root)
    assert second.get_text(ref) == content
    assert ref.size == len(content.encode("utf-8"))
    assert len(ref.content_hash) == 64


def test_artifact_hash_detects_tampering(tmp_path) -> None:
    store = RuntimeArtifactStore(tmp_path / "artifacts")
    ref = store.put_text("original", run_id="run-1", node_id="node-1")
    path = store.root / ref.store_key
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        store.get_text(ref)


def test_content_write_without_metadata_is_detected_as_orphan(tmp_path, monkeypatch) -> None:
    store = RuntimeArtifactStore(tmp_path / "artifacts")
    original = store._atomic_write
    calls = 0

    def fail_metadata(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("metadata commit interrupted")
        original(path, data)

    monkeypatch.setattr(store, "_atomic_write", fail_metadata)
    with pytest.raises(OSError, match="metadata commit interrupted"):
        store.put_text("complete content", run_id="run-1", node_id="node-1")
    assert len(store.orphaned_content()) == 1


def test_artifact_read_rejects_hardlink_alias_even_when_content_matches(tmp_path) -> None:
    store = RuntimeArtifactStore(tmp_path / "artifacts")
    ref = store.put_text("same bytes", run_id="run-1", node_id="node-1")
    content_path = store.root / ref.store_key
    outside = tmp_path / "outside.txt"
    outside.write_bytes(content_path.read_bytes())
    content_path.unlink()
    os.link(outside, content_path)

    with pytest.raises(ArtifactIntegrityError, match="aliases"):
        store.get_text(ref)
