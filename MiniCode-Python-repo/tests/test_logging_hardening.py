"""Tests for Issue #5 — logging system hardening and structured logging.

Covers:
- rotation strategy documented as size-only (no dead TimedRotating constants)
- StructuredFormatter emits JSON with structured extras
- structured_logging_requested honors CLI flag + MINI_CODE_LOG_STRUCTURED env
- ToolRegistry.execute logs tool execution on success AND on crash (issue #5)
- log_permission_check / log_session_event emit records
- main.py exposes the --structured-logs flag
"""

from __future__ import annotations

import json
import logging

import pytest

from minicode.logging_config import (
    StructuredFormatter,
    log_permission_check,
    log_session_event,
    log_tool_execution,
    setup_logging,
    structured_logging_requested,
)
from minicode.tooling import ToolDefinition, ToolRegistry, ToolResult


# ---------------------------------------------------------------------------
# Rotation strategy + formatter
# ---------------------------------------------------------------------------


def test_rotation_is_size_only_no_dead_timed_constants() -> None:
    import minicode.logging_config as lc

    # The dead "also rotate at midnight" constants must be gone.
    assert not hasattr(lc, "LOG_ROTATION_WHEN")
    assert not hasattr(lc, "LOG_ROTATION_INTERVAL")
    # Docstring must no longer claim time-based rotation.
    assert "按大小 + 按时间" not in lc.__doc__


def test_setup_logging_uses_rotating_file_handler(tmp_path, monkeypatch) -> None:
    import logging.handlers as handlers

    monkeypatch.setattr(lc := __import__("minicode.logging_config", fromlist=["LOG_FILE"]), "LOG_FILE", tmp_path / "t.log")
    setup_logging(level="DEBUG", log_to_console=False, structured=False)
    root = logging.getLogger("minicode")
    file_handlers = [h for h in root.handlers if isinstance(h, handlers.RotatingFileHandler)]
    assert file_handlers, "expected a RotatingFileHandler (size-based rotation)"


def test_structured_formatter_emits_json_with_extras() -> None:
    formatter = StructuredFormatter()
    record = logging.LogRecord(
        name="minicode.tools",
        level=logging.WARNING,
        pathname="tooling.py",
        lineno=1,
        msg="Tool %s failed",
        args=("echo",),
        exc_info=None,
    )
    record.tool_name = "echo"
    record.duration_ms = 12.0
    record.error_category = "tool_failure"
    line = formatter.format(record)
    parsed = json.loads(line)
    assert parsed["level"] == "WARNING"
    assert parsed["module"] == "minicode.tools"
    assert parsed["tool_name"] == "echo"
    assert parsed["error_category"] == "tool_failure"
    assert "failed" in parsed["msg"]


# ---------------------------------------------------------------------------
# Structured-logging flag / env
# ---------------------------------------------------------------------------


def test_structured_logging_requested_cli_flag(monkeypatch) -> None:
    monkeypatch.delenv("MINI_CODE_LOG_STRUCTURED", raising=False)
    assert structured_logging_requested(cli_flag=True) is True
    assert structured_logging_requested(cli_flag=False) is False


def test_structured_logging_requested_env(monkeypatch) -> None:
    for val in ("true", "1", "yes", "ON"):
        monkeypatch.setenv("MINI_CODE_LOG_STRUCTURED", val)
        assert structured_logging_requested() is True
    for val in ("", "0", "no", "false"):
        monkeypatch.setenv("MINI_CODE_LOG_STRUCTURED", val)
        assert structured_logging_requested() is False


# ---------------------------------------------------------------------------
# Tool execution logging (issue #5: tool crashes must reach the log file)
# ---------------------------------------------------------------------------


def _registry_with(tool) -> ToolRegistry:
    return ToolRegistry([tool])


def test_tool_execute_logs_success(caplog) -> None:
    def run_ok(input_data, _context):
        return ToolResult(ok=True, output="done")

    reg = _registry_with(
        ToolDefinition(name="echo", description="d", input_schema={"type": "object"}, validator=lambda v: v, run=run_ok)
    )
    with caplog.at_level(logging.DEBUG, logger="minicode.tools"):
        result = reg.execute("echo", {}, context=None)
    assert result.ok is True
    assert any("echo" in r.getMessage() and "successfully" in r.getMessage() for r in caplog.records)


def test_tool_execute_logs_crash(caplog) -> None:
    def run_boom(input_data, _context):
        raise RuntimeError("kaboom")

    reg = _registry_with(
        ToolDefinition(name="boom", description="d", input_schema={"type": "object"}, validator=lambda v: v, run=run_boom)
    )
    with caplog.at_level(logging.WARNING, logger="minicode.tools"):
        result = reg.execute("boom", {}, context=None)
    assert result.ok is False
    # log_tool_execution warns on failure, and logger.exception emits ERROR
    msgs = [r.getMessage() for r in caplog.records]
    assert any("boom" in m and "failed" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# Permission / session structured helpers emit records
# ---------------------------------------------------------------------------


def test_log_permission_check_emits(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="minicode.permissions"):
        log_permission_check("edit_file", "/tmp/x", granted=False)
    assert any("Permission denied" in r.getMessage() for r in caplog.records)


def test_log_session_event_emits(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="minicode.session"):
        log_session_event("save", details="id=abc")
    assert any("Session save" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_main_argparse_has_structured_logs_flag() -> None:
    import importlib

    import minicode.main as main_module

    # The flag name is registered in build_arg_parser / main's argparse; verify
    # by inspecting the parser construction via the module source (stable contract).
    src = importlib.util.find_spec("minicode.main").origin
    text = open(src, encoding="utf-8").read() if src else ""
    assert "--structured-logs" in text
    assert "MINI_CODE_LOG_STRUCTURED" in text
