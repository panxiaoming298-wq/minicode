from __future__ import annotations

import json
import time

from minicode.config import MINI_CODE_DIR, MINI_CODE_HISTORY_PATH


# Simple TTL cache: stores (timestamp, value)
_history_cache: tuple[float, list[str]] | None = None
_history_cache_ttl: float = 5.0  # seconds


def load_history_entries() -> list[str]:
    global _history_cache
    now = time.time()
    if _history_cache is not None:
        cached_at, cached_entries = _history_cache
        if now - cached_at < _history_cache_ttl:
            return cached_entries.copy()

    if not MINI_CODE_HISTORY_PATH.exists():
        _history_cache = (now, [])
        return []
    try:
        parsed = json.loads(MINI_CODE_HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _history_cache = (now, [])
        return []
    entries = parsed.get("entries", [])
    result = [str(entry) for entry in entries] if isinstance(entries, list) else []
    _history_cache = (now, result)
    return result.copy()


def save_history_entries(entries: list[str]) -> None:
    global _history_cache
    MINI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    MINI_CODE_HISTORY_PATH.write_text(
        json.dumps({"entries": entries[-200:]}, indent=2) + "\n",
        encoding="utf-8",
    )
    _history_cache = (time.time(), entries[-200:].copy())

