"""Filename discovery with bounded traversal, output and cancellation."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import stat
import time

from minicode.tooling import ToolResult
from minicode.tools._file_search import SKIP_DIRS


def find_names(root: Path, options, cancellation=None):
    patterns = options.get("pattern", "*")
    patterns = [patterns] if isinstance(patterns, str) else patterns
    deadline = time.monotonic() + options.get("timeout_seconds", 5)
    cap = options.get("max_entries", 20000)
    result_cap = options.get("max_results", 200)
    depth = options.get("max_depth", 6) if options.get("recursive", False) else 0
    pending = [(root, 0)]
    found, incomplete, visited, skipped_links, errors = [], set(), 0, 0, []
    stop = None
    excluded = {os.path.normcase(name) for name in SKIP_DIRS}
    output_size = len(str(root)) + 2000
    while pending and stop is None:
        if cancellation is not None and cancellation.is_set():
            stop = "canceled"
            break
        if time.monotonic() >= deadline:
            stop = "time_limit"
            break
        directory, level = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancellation is not None and cancellation.is_set():
                        stop = "canceled"
                    elif time.monotonic() >= deadline:
                        stop = "time_limit"
                    elif visited >= cap:
                        stop = "entry_limit"
                    if stop:
                        break
                    visited += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                            skipped_links += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if os.path.normcase(entry.name) in excluded or entry.name.startswith("."):
                                continue
                            if level < depth:
                                pending.append((Path(entry.path), level + 1))
                            elif options.get("recursive", False):
                                incomplete.add("depth_limit")
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        relative = Path(entry.path).relative_to(root).as_posix()
                        if any(fnmatch.fnmatch(entry.name, pattern) or fnmatch.fnmatch(relative, pattern) for pattern in patterns):
                            item = {"path": relative, "size": info.st_size, "modified_at": info.st_mtime}
                            output_size += len(json.dumps(item, ensure_ascii=False)) + 2
                            if output_size > 50000:
                                stop = "output_limit"
                                break
                            found.append(item)
                            if len(found) >= result_cap:
                                stop = "result_limit"
                                break
                    except OSError as error:
                        incomplete.add("unreadable_entry")
                        if len(errors) < 5:
                            errors.append({"path": entry.name, "error": type(error).__name__})
        except OSError as error:
            incomplete.add("unreadable_directory")
            if len(errors) < 5:
                errors.append({"path": directory.relative_to(root).as_posix(), "error": type(error).__name__})
    if stop:
        incomplete.add(stop)
    payload = {"root": str(root), "files": sorted(found, key=lambda item: item["path"]),
        "complete": not incomplete, "limits_reached": sorted(incomplete), "entries_visited": visited,
        "scope": {"recursive": bool(options.get("recursive")), "max_depth": depth,
                  "patterns": patterns, "excluded_directories": sorted(SKIP_DIRS), "hidden_directories": "excluded"},
        "skipped_links": skipped_links, "errors": errors,
        "next_action": "Narrow the directory or pattern and search the missing scope; partial results cannot prove absence." if incomplete else "Search completed within the declared scope and exclusions."}
    return ToolResult(ok=stop != "canceled", output=json.dumps(payload, ensure_ascii=False),
                      failureKind="canceled" if stop == "canceled" else None)
