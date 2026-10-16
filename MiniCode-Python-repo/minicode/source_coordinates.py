"""Decode native file display coordinates without changing source characters."""
from __future__ import annotations


def read_file_body(output: str) -> str | None:
    """Recover the shown source window, including indentation and final newline.

    This is a presentation decoder, never a read receipt or authority check.
    Unknown/malformed numbering fails closed; old raw tool outputs still work.
    """
    header, marker, tail = output.partition("\nTRUNCATED: ")
    _, newline, body = tail.partition("\n")
    if not output.startswith("FILE: ") or not marker or not newline:
        return None
    fields = dict(line.split(": ", 1) for line in header.splitlines() if ": " in line)
    numbering = fields.get("LINE_NUMBERS")
    if numbering is None:
        return body
    if numbering != "display-only; offsets exclude prefixes":
        return None
    try:
        first = int(fields["START_LINE"])
    except (KeyError, ValueError):
        return None
    if first < 1:
        return None
    source = []
    for number, row in enumerate(body.splitlines(keepends=True), first):
        prefix = f"{number}: "
        if not row.startswith(prefix):
            return None
        source.append(row[len(prefix):])
    return "".join(source)
