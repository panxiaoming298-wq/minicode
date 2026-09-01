"""Tests for minicode.hooks — event-driven hook system."""

from __future__ import annotations

import pytest

from minicode.hooks import (
    HookContext,
    HookEvent,
    HookManager,
    HookRegistration,
    create_logging_hook,
    fire_hook_sync,
    get_hook_manager,
    register_hook,
)


# ---------------------------------------------------------------------------
# HookContext
# ---------------------------------------------------------------------------

class TestHookContext:
    def test_defaults(self) -> None:
        ctx = HookContext(event=HookEvent.PRE_TOOL_USE)
        assert ctx.event == HookEvent.PRE_TOOL_USE
        assert ctx.tool_name is None
        assert ctx.tool_input is None
        assert ctx.tool_output is None
        assert ctx.is_error is False
        assert ctx.session_id is None
        assert ctx.user_input is None
        assert ctx.assistant_output is None

    def test_tool_properties(self) -> None:
        ctx = HookContext(
            event=HookEvent.PRE_TOOL_USE,
            data={"tool_name": "write_file", "tool_input": {"path": "/x"}},
        )
        assert ctx.tool_name == "write_file"
        assert ctx.tool_input == {"path": "/x"}

    def test_error_context(self) -> None:
        ctx = HookContext(
            event=HookEvent.POST_TOOL_USE,
            data={"tool_output": "error msg", "is_error": True},
        )
        assert ctx.tool_output == "error msg"
        assert ctx.is_error is True

    def test_session_properties(self) -> None:
        ctx = HookContext(
            event=HookEvent.SESSION_SAVE,
            data={"session_id": "sess-001"},
        )
        assert ctx.session_id == "sess-001"

    def test_user_properties(self) -> None:
        ctx = HookContext(
            event=HookEvent.USER_INPUT,
            data={"user_input": "hello"},
        )
        assert ctx.user_input == "hello"

    def test_assistant_properties(self) -> None:
        ctx = HookContext(
            event=HookEvent.ASSISTANT_OUTPUT,
            data={"assistant_output": "world"},
        )
        assert ctx.assistant_output == "world"


# ---------------------------------------------------------------------------
# HookEvent enum
# ---------------------------------------------------------------------------

class TestHookEvent:
    def test_all_events_exist(self) -> None:
        expected = {
            "pre_tool_use", "post_tool_use",
            "agent_start", "agent_stop", "subagent_start", "subagent_stop",
            "session_save", "session_resume",
            "user_input", "assistant_output",
            "startup", "shutdown",
        }
        assert {e.value for e in HookEvent} == expected


# ---------------------------------------------------------------------------
# HookRegistration
# ---------------------------------------------------------------------------

class TestHookRegistration:
    def test_defaults(self) -> None:
        reg = HookRegistration(event=HookEvent.STARTUP, handler=lambda _: None)
        assert reg.event == HookEvent.STARTUP
        assert reg.enabled is True
        assert reg.is_async is False
        assert reg.description == ""
        assert reg.call_count == 0
        assert reg.last_called is None
        assert reg.total_duration_ms == 0
        assert reg.failure_count == 0
        assert reg.last_error == ""
        assert reg.last_status == "idle"

    def test_async_detection(self) -> None:
        """HookManager.register automatically detects async handlers."""
        mgr = HookManager()

        async def async_handler(_ctx: HookContext) -> str:
            return "async"

        mgr.register(HookEvent.SHUTDOWN, async_handler)
        reg = mgr._hooks[HookEvent.SHUTDOWN][0]
        assert reg.is_async is True


# ---------------------------------------------------------------------------
# HookManager
# ---------------------------------------------------------------------------

class TestHookManager:
    @pytest.fixture
    def mgr(self) -> HookManager:
        return HookManager()

    def test_register_sync_handler(self, mgr: HookManager) -> None:
        handler = lambda ctx: "done"
        unreg = mgr.register(HookEvent.STARTUP, handler, "test hook")
        hooks = mgr._hooks[HookEvent.STARTUP]
        assert len(hooks) == 1
        assert hooks[0].description == "test hook"
        assert hooks[0].enabled is True
        assert callable(unreg)

    def test_unregister(self, mgr: HookManager) -> None:
        unreg = mgr.register(HookEvent.SHUTDOWN, lambda _: None)
        assert len(mgr._hooks[HookEvent.SHUTDOWN]) == 1
        unreg()
        assert len(mgr._hooks[HookEvent.SHUTDOWN]) == 0

    def test_unregister_idempotent(self, mgr: HookManager) -> None:
        unreg = mgr.register(HookEvent.SHUTDOWN, lambda _: None)
        unreg()
        unreg()  # Should not raise
        assert len(mgr._hooks[HookEvent.SHUTDOWN]) == 0

    def test_fire_sync_calls_handler(self, mgr: HookManager) -> None:
        called: list[HookContext | None] = []

        def handler(ctx: HookContext) -> str:
            called.append(ctx)
            return "result"

        mgr.register(HookEvent.STARTUP, handler)
        results = mgr.fire_sync(HookEvent.STARTUP, key="val")

        assert len(called) == 1
        assert called[0].data.get("key") == "val"
        assert results == ["result"]

    def test_fire_sync_multiple_handlers(self, mgr: HookManager) -> None:
        results: list[str] = []

        def h1(_ctx: HookContext) -> str:
            results.append("h1")
            return "r1"

        def h2(_ctx: HookContext) -> str:
            results.append("h2")
            return "r2"

        mgr.register(HookEvent.USER_INPUT, h1)
        mgr.register(HookEvent.USER_INPUT, h2)
        output = mgr.fire_sync(HookEvent.USER_INPUT)

        assert results == ["h1", "h2"]
        assert output == ["r1", "r2"]

    def test_fire_sync_skips_disabled(self, mgr: HookManager) -> None:
        called: list[str] = []

        def h1(_ctx: HookContext) -> str:
            called.append("h1")
            return ""

        def h2(_ctx: HookContext) -> str:
            called.append("h2")
            return ""

        mgr.register(HookEvent.PRE_TOOL_USE, h1)
        mgr.register(HookEvent.PRE_TOOL_USE, h2)
        mgr._hooks[HookEvent.PRE_TOOL_USE][1].enabled = False
        mgr.fire_sync(HookEvent.PRE_TOOL_USE)

        assert called == ["h1"]

    def test_fire_sync_skips_async_handlers(self, mgr: HookManager) -> None:
        called: list[str] = []

        async def async_h(_ctx: HookContext) -> str:
            called.append("async")
            return ""

        def sync_h(_ctx: HookContext) -> str:
            called.append("sync")
            return ""

        mgr.register(HookEvent.AGENT_START, async_h)
        mgr.register(HookEvent.AGENT_START, sync_h)
        mgr.fire_sync(HookEvent.AGENT_START)

        assert called == ["sync"]

    def test_fire_sync_handles_handler_errors(self, mgr: HookManager) -> None:
        called: list[str] = []

        def failing(_ctx: HookContext) -> None:
            called.append("fail")
            raise RuntimeError("boom")

        def normal(_ctx: HookContext) -> str:
            called.append("normal")
            return "ok"

        mgr.register(HookEvent.AGENT_STOP, failing)
        mgr.register(HookEvent.AGENT_STOP, normal)
        results = mgr.fire_sync(HookEvent.AGENT_STOP)

        assert called == ["fail", "normal"]
        assert results == ["Hook error: boom", "ok"]
        # Verify error tracking
        stats = mgr.get_hook_stats(HookEvent.AGENT_STOP)
        assert stats["failure_count"] == 1

    def test_enable_disable(self, mgr: HookManager) -> None:
        called = False

        def h(_ctx: HookContext) -> str:
            nonlocal called
            called = True
            return ""

        mgr.register(HookEvent.STARTUP, h)
        mgr.disable()
        mgr.fire_sync(HookEvent.STARTUP)
        assert called is False

        mgr.enable()
        mgr.fire_sync(HookEvent.STARTUP)
        assert called is True

    def test_get_hook_stats_all_events(self, mgr: HookManager) -> None:
        mgr.register(HookEvent.STARTUP, lambda _: None)
        mgr.register(HookEvent.SHUTDOWN, lambda _: None)
        mgr.fire_sync(HookEvent.STARTUP)

        stats = mgr.get_hook_stats()
        assert stats["total_hooks"] == 2
        assert stats["enabled_hooks"] == 2
        assert stats["total_calls"] == 1

    def test_get_hook_stats_specific_event(self, mgr: HookManager) -> None:
        mgr.register(HookEvent.STARTUP, lambda _: None, "start")
        mgr.register(HookEvent.SHUTDOWN, lambda _: None, "stop")

        startup_stats = mgr.get_hook_stats(HookEvent.STARTUP)
        assert startup_stats["total_hooks"] == 1

    def test_format_hook_status(self, mgr: HookManager) -> None:
        mgr.register(HookEvent.STARTUP, lambda _: None, "startup hook")
        output = mgr.format_hook_status()
        assert "startup hook" in output
        assert "Total hooks:" in output
        assert "Enabled:" in output

    def test_fire_sync_method_update_metadata(self, mgr: HookManager) -> None:
        mgr.register(HookEvent.STARTUP, lambda _: None, "test")
        mgr.fire_sync(HookEvent.STARTUP)

        reg = mgr._hooks[HookEvent.STARTUP][0]
        assert reg.call_count == 1
        assert reg.last_called is not None
        assert reg.last_status == "success"
        assert reg.total_duration_ms >= 0

    def test_fire_sync_error_updates_metadata(self, mgr: HookManager) -> None:
        def bad(_ctx: HookContext) -> None:
            raise ValueError("bad")

        mgr.register(HookEvent.SHUTDOWN, bad)
        mgr.fire_sync(HookEvent.SHUTDOWN)

        reg = mgr._hooks[HookEvent.SHUTDOWN][0]
        assert reg.failure_count == 1
        assert reg.last_status == "error"
        assert reg.last_error == "bad"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestHookSingleton:
    def test_get_hook_manager_returns_same_instance(self) -> None:
        m1 = get_hook_manager()
        m2 = get_hook_manager()
        assert m1 is m2

    def test_register_hook_convenience(self) -> None:
        unreg = register_hook(HookEvent.STARTUP, lambda _: None)
        mgr = get_hook_manager()
        assert len(mgr._hooks[HookEvent.STARTUP]) == 1
        unreg()
        assert len(mgr._hooks[HookEvent.STARTUP]) == 0

    def test_fire_hook_sync_convenience(self) -> None:
        called = False

        def h(_ctx: HookContext) -> str:
            nonlocal called
            called = True
            return "done"

        unreg = register_hook(HookEvent.AGENT_START, h)
        try:
            results = fire_hook_sync(HookEvent.AGENT_START, msg="test")
            assert called is True
            assert results == ["done"]
        finally:
            unreg()


# ---------------------------------------------------------------------------
# create_logging_hook
# ---------------------------------------------------------------------------

class TestCreateLoggingHook:
    def test_logging_hook_writes_to_file(self, tmp_path) -> None:
        log_file = tmp_path / "hooks.log"
        hook = create_logging_hook(log_file)

        ctx = HookContext(event=HookEvent.STARTUP, data={"tool_name": "test"})
        hook(ctx)

        content = log_file.read_text()
        assert "startup" in content
        assert "tool=test" in content

    def test_logging_hook_creates_parent_dir(self, tmp_path) -> None:
        log_file = tmp_path / "sub" / "deep" / "hooks.log"
        hook = create_logging_hook(log_file)
        ctx = HookContext(event=HookEvent.SHUTDOWN)
        hook(ctx)

        assert log_file.exists()

    def test_logging_hook_no_file_does_not_crash(self) -> None:
        hook = create_logging_hook(None)
        ctx = HookContext(event=HookEvent.STARTUP)
        # Should not raise
        hook(ctx)

    def test_logging_hook_appends_to_file(self, tmp_path) -> None:
        log_file = tmp_path / "hooks.log"
        log_file.write_text("existing\n")

        hook = create_logging_hook(log_file)
        ctx = HookContext(event=HookEvent.STARTUP)
        hook(ctx)

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert lines[0] == "existing"
