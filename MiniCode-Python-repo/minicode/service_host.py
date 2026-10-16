"""Launch gate for an application-owned service or command (stdlib only).

The supervisor assigns this process to its ownership boundary before sending
the command. On Windows this prevents children escaping job registration.
"""
from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    line = sys.stdin.buffer.readline(1_048_577)
    if not line or len(line) > 1_048_576:
        return 2
    payload = json.loads(line)
    child = subprocess.Popen(payload["argv"], cwd=payload["cwd"], stdin=subprocess.DEVNULL)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
