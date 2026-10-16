"""Bounded retrieval queries; never rewrites authoritative user messages."""

import re
from collections.abc import Sequence


_SPACE = re.compile(r"\s+")
_SIGNAL = re.compile(
    r"`[^`\n]{1,96}`|(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]{0,63}_[A-Za-z0-9_]{1,96}"
    r"|(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{1,96}\.(?:py|tsx?|jsx?|json|ya?ml|toml|sql|md)\b"
    r"|\bv?\d{1,8}\.\d{1,8}(?:\.\d{1,8})?\b"
    r"|不是|不得|禁止|不要|除非|仅当|不再|\b(?:not|never|unless|except)\b",
    re.IGNORECASE,
)
_BOUNDARIES = "。！？;；\n"
_SCAN_CHARS = 16384


def _head_tail(text: str, size: int) -> str:
    text = _SPACE.sub(" ", text).strip()
    if size <= 3:
        return text[:max(0, size)]
    if len(text) <= size:
        return text
    head = (size - 3) * 2 // 3
    return text[:head] + " … " + text[-(size - 3 - head) :]


def _question(text: str, size: int) -> str:
    """Keep bounded literal evidence from the middle, including its qualifiers."""
    text = _SPACE.sub(" ", text).strip()
    if len(text) <= size:
        return text
    # Search the otherwise omitted middle, with a bounded scan even for huge
    # pasted logs. The normal head/tail always retains the latest correction.
    start, end = size * 2 // 3, len(text) - size // 3
    if end - start > _SCAN_CHARS:
        start += (end - start - _SCAN_CHARS) // 2
        end = start + _SCAN_CHARS
    middle = text[start:end]
    excerpts = []
    remaining = min(120, size // 2)
    for index, match in enumerate(_SIGNAL.finditer(middle)):
        if index >= 24 or remaining < 24:
            break
        left, right = start + match.start(), start + match.end()
        if right - left > remaining:
            continue
        begin = max(0, left - 24)
        for boundary in _BOUNDARIES:
            begin = max(begin, text.rfind(boundary, begin, left) + 1)
        finish = min(len(text), right + 48, begin + remaining)
        if finish < right:
            begin = max(begin, right - remaining)
            finish = right
        for boundary in _BOUNDARIES:
            position = text.find(boundary, right, finish)
            if position >= 0:
                finish = min(finish, position + 1)
        excerpt = text[begin:finish].strip()
        if excerpt and not any(excerpt in old or text[left:right] in old for old in excerpts):
            excerpts.append(excerpt)
            remaining -= len(excerpt) + 1
    if not excerpts:
        return _head_tail(text, size)
    evidence = " ".join(excerpts)
    return _head_tail(text, size - len(evidence) - 1) + "\n" + evidence


def retrieval_query(user_input: str, *, goal: str = "", phase: str = "",
                    focus: Sequence[str] = ()) -> str:
    """Bound local recall to 400 characters without rewriting user messages.

    The caller supplies at most two hints from an already verified current note.
    Hints are query data, never instructions or evidence. No model or storage
    call is needed; source messages and required dependencies remain complete.
    """
    if not user_input.strip() and not focus:
        return _question(goal or phase, 400)
    hint = _question(" ".join(focus[:2]), 80) if focus else ""
    question = _question(user_input or goal, 272 if hint else 300)
    stage = _head_tail(phase, 16)
    remaining = 400 - sum(map(len, (question, hint, stage))) - 3
    background = _head_tail(goal, min(80, remaining)) if user_input.strip() else ""
    return "\n".join(part for part in (question, hint, background, stage) if part)
