"""External, immutable pure-function checks for two export policy interfaces.

No candidate imports or arbitrary commands. The same published AST boundary as
the pagination pilot is loaded from a trusted sibling, outside the workspace.
This is not a general-purpose Python/OS sandbox or a deployed export service.
"""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import stat
import sys


load_pure_function = runpy.run_path(str(Path(__file__).with_name("runtime_live_eval_checks.py")))["load_pure_function"]


def _load(workspace: Path, name: str):
    path = workspace / f"{name}.py"
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or path.resolve().parent != workspace.resolve() or info.st_size > 20_000):
        raise ValueError(f"{name} must be a direct, bounded regular file")
    return load_pure_function(path.read_text(encoding="utf-8"), name)


def check_workspace(workspace: Path) -> dict:
    window = _load(workspace, "batch_window")
    retry = _load(workspace, "retry_delay")
    counts = {"window": 0, "retry": 0, "composition": 0}
    failures: list[str] = []

    def equal(group, function, args, expected):
        counts[group] += 1
        try:
            result = function(*args)
            # bool is not accepted as an integer result, including nested fields.
            if result != expected or type(result) is not type(expected) or (
                isinstance(expected, dict) and any(type(result[key]) is not type(value) for key, value in expected.items())
            ):
                failures.append(f"{group} {args!r}: wrong result")
        except Exception as error:
            failures.append(f"{group} {args!r}: raised {type(error).__name__}")

    def invalid(group, function, args):
        counts[group] += 1
        try:
            function(*args)
        except ValueError:
            return
        except Exception as error:
            failures.append(f"{group} invalid input: raised {type(error).__name__}, expected ValueError")
        else:
            failures.append(f"{group} invalid input did not raise ValueError")

    for total in (0, 1, 7, 19):
        for cursor in (0, 1, 5, total, total + 3):
            for limit in (1, 2, 8, 32):
                start = min(cursor, total)
                stop = min(start + limit, total)
                equal("window", window, (total, cursor, limit),
                      {"start": start, "stop": stop, "next_cursor": stop if stop < total else None})
    for position in range(3):
        for value in (True, False, -1, 1.5, "2", None):
            args = [10, 0, 3]
            args[position] = value
            invalid("window", window, args)
    invalid("window", window, (10, 0, 0))

    for status in (200, 400, 408, 409, 429, 500, 501, 502, 503, 504, 599):
        for attempt in (1, 2, 3, 4):
            for max_attempts in (1, 3, 4):
                for base_ms in (0, 10):
                    for cap_ms in (0, 25):
                        for after in (None, 0, 50):
                            expected = None
                            if status in (408, 429, 500, 502, 503, 504) and attempt < max_attempts:
                                expected = min(cap_ms, max(attempt * base_ms, after or 0)
                                               if status in (429, 503) else attempt * base_ms)
                            equal("retry", retry, (status, attempt, max_attempts, base_ms, cap_ms, after), expected)
    for position in range(6):
        values = (True, False, -1, 1.5, "2") + ((None,) if position != 5 else ())
        for value in values:
            # Ineligible/exhausted inputs must still validate ALL arguments.
            args = [200, 3, 3, 10, 25, None]
            args[position] = value
            invalid("retry", retry, args)
    for args in ((99, 1, 3, 0, 0, None), (600, 1, 3, 0, 0, None),
                 (429, 0, 3, 0, 0, None), (429, 1, 0, 0, 0, None)):
        invalid("retry", retry, args)

    # A fixed caller composes the actual functions. It does not replace a model,
    # tool or network service and does not claim deployed-service coverage.
    for total in (0, 1, 5, 17):
        for limit in (1, 3, 8):
            counts["composition"] += 1
            try:
                cursor, covered = 0, []
                for _ in range(total + 1):
                    batch = window(total, cursor, limit)
                    if not (type(batch) is dict and set(batch) == {"start", "stop", "next_cursor"}
                        and type(batch["start"]) is int and type(batch["stop"]) is int
                        and 0 <= batch["start"] <= batch["stop"] <= total):
                        raise ValueError("invalid window bounds during composition")
                    if retry(429, 1, 3, 10, 25, 50) != 25 or retry(503, 3, 3, 10, 25, None) is not None:
                        raise ValueError("invalid retry policy during batch processing")
                    covered.extend(range(batch["start"], batch["stop"]))
                    cursor = batch["next_cursor"]
                    if cursor is None:
                        break
                if cursor is not None or covered != list(range(total)):
                    raise ValueError("batch walk lost, duplicated or failed to finish items")
            except Exception as error:
                failures.append(f"composition total={total} limit={limit}: {type(error).__name__}")
    return {"passed": not failures, "checks": sum(counts.values()), "groups": counts,
            "failure_count": len(failures), "failures": failures[:12]}


def main() -> int:
    try:
        result = check_workspace(Path(sys.argv[1]).resolve())
    except Exception as error:
        result = {"passed": False, "checks": 0, "failures": [f"{type(error).__name__}: {error}"]}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
