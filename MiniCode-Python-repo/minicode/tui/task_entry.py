"""Explicit task-file preview and confirmation for the next TUI agent turn."""
from __future__ import annotations

import json
from pathlib import Path

from minicode.entry_task import TRUST_NOTICE, restore_task, task_packet
from minicode.headless_task import load_headless_task


def _scope(args, state):
    return (str(Path(args.cwd).resolve()), state.session.session_id if state.session else "")


def _clear(state):
    state.pending_task, state.task_confirmed, state.task_scope = None, False, None


def handle_task_command(args, state, text: str) -> str | None:
    if not (text == "/task-file" or text.startswith("/task-file ") or text == "/task-confirm"):
        return None
    if state.pending_task is not None and state.task_scope != _scope(args, state):
        _clear(state)
    if text == "/task-confirm":
        if state.pending_task is None:
            return "没有待确认契约。先用 /task-file <路径> 预览。"
        state.task_confirmed = True
        return f"已确认信任验收命令。下一条 Agent 任务使用契约 {state.pending_task.source_sha256}，仅此一轮生效；不保存为全局设置。"
    value = text[len("/task-file"):].strip()
    if value == "clear":
        _clear(state)
        return "已移除下一轮任务契约。"
    if value and value != "show":
        _clear(state)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        try:
            state.pending_task = load_headless_task(value, args.cwd)
        except (ValueError, OSError) as error:
            return f"契约未加载：{error}" if isinstance(error, ValueError) else "契约未加载：无法读取文件，请检查路径与权限。"
        state.task_scope = _scope(args, state)
    if state.pending_task is None:
        return "使用 /task-file <本机 JSON 路径> 预览；/task-confirm 确认命令；/task-file show 查看；/task-file clear 移除。仅作用于下一条 Agent 任务。"
    task = state.pending_task
    return (f"任务契约 {task.source_sha256}\n{json.dumps(task.fields(), ensure_ascii=False, indent=2)}\n{TRUST_NOTICE}\n"
            "发送使用此冻结快照，不自动读取之后的文件变更。\n" + ("已确认。" if state.task_confirmed else "请审阅以上命令，信任后输入 /task-confirm；尚未启动任务。"))


def take_task(args, state):
    if state.pending_task is None:
        return None
    if state.task_scope != _scope(args, state):
        _clear(state)
        raise ValueError("会话或工作区已变化，请重新选择并确认任务契约。")
    if not state.task_confirmed:
        raise ValueError("任务契约尚未确认。请先 /task-confirm，或 /task-file clear 移除；本条任务尚未启动。")
    task = restore_task(task_packet(state.pending_task), args.cwd)
    task.apply(args.runtime or {})  # Reject explicitly disabled Runtime before consuming.
    _clear(state)
    return task
