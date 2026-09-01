"""Tests for run_command output encoding (MINICODE_COMMAND_ENCODING).

On Chinese Windows (cp936/GBK OEM code page), legacy commands output GBK bytes.
The default UTF-8 decode garbles them, and a "try UTF-8 first" fallback can't
help (GBK bytes are frequently valid UTF-8 → wrong chars, no fallback). The
fix is an explicit override via MINICODE_COMMAND_ENCODING.
"""

from __future__ import annotations

import importlib

import pytest


def _reload(monkeypatch):
    import minicode.tools.run_command as rc
    return importlib.reload(rc)


def test_default_encoding_is_utf8(monkeypatch) -> None:
    monkeypatch.delenv("MINICODE_COMMAND_ENCODING", raising=False)
    rc = _reload(monkeypatch)
    assert rc._command_output_encoding() == "utf-8"
    assert rc._decode_command_output("中文".encode("utf-8")) == "中文"


def test_cp936_override_decodes_gbk(monkeypatch) -> None:
    """The case UTF-8-first can't handle: GBK bytes decode correctly under cp936."""
    monkeypatch.setenv("MINICODE_COMMAND_ENCODING", "cp936")
    rc = _reload(monkeypatch)
    assert rc._command_output_encoding() == "cp936"
    assert rc._decode_command_output("目录文件".encode("gbk")) == "目录文件"


def test_gbk_alias_works(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_COMMAND_ENCODING", "gbk")
    rc = _reload(monkeypatch)
    assert rc._decode_command_output("目录".encode("gbk")) == "目录"


def test_bad_encoding_name_falls_back_to_utf8(monkeypatch) -> None:
    """An invalid MINICODE_COMMAND_ENCODING must not crash execution."""
    monkeypatch.setenv("MINICODE_COMMAND_ENCODING", "not-a-real-codec")
    rc = _reload(monkeypatch)
    assert rc._decode_command_output("hello".encode()) == "hello"


def test_decode_passthrough_and_empty(monkeypatch) -> None:
    monkeypatch.delenv("MINICODE_COMMAND_ENCODING", raising=False)
    rc = _reload(monkeypatch)
    assert rc._decode_command_output(None) == ""
    assert rc._decode_command_output(b"") == ""
    assert rc._decode_command_output("already a str") == "already a str"


def test_blank_override_falls_back_to_utf8(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_COMMAND_ENCODING", "   ")
    rc = _reload(monkeypatch)
    assert rc._command_output_encoding() == "utf-8"
