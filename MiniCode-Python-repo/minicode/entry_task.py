"""One-turn, explicitly trusted task-file snapshots for product entry points."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import re
from typing import Any

from minicode.headless_task import HeadlessTask, MAX_BYTES, load_headless_task, parse_headless_task

TRUST_NOTICE = "验收命令会在本机执行，临时工作区不是 OS 沙箱。仅确认你信任的命令；契约不授予 Agent 额外工具权限。"


def task_preview(task: HeadlessTask) -> dict[str, Any]:
    return {**task.summary(), **task.fields(), "warning": TRUST_NOTICE}


def public_task_file(value: Any) -> dict[str, Any] | None:
    if (not isinstance(value, dict) or not isinstance(value.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            or type(value.get("requiresWorkspaceWrite")) is not bool):
        return None
    counts = ("criteriaCount", "expectedOutputCount", "verificationCommandCount")
    if any(type(value.get(key)) is not int or not 0 <= value[key] <= 64 for key in counts):
        return None
    return {"sha256": value["sha256"], "requiresWorkspaceWrite": value["requiresWorkspaceWrite"], **{key: value[key] for key in counts}}


def selected_task(path: str, cwd: str, sha256: str, commands_trusted: bool) -> HeadlessTask:
    if commands_trusted is not True:
        raise ValueError("请先预览并确认信任验收命令。")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("缺少有效的契约预览摘要，请重新预览。")
    task = load_headless_task(path, cwd)
    if task.source_sha256 != sha256:
        raise ValueError("契约文件已变化，请重新预览并确认，不会执行旧预览之外的命令。")
    return task


def task_packet(task: HeadlessTask) -> dict[str, str]:
    if not task.source_bytes or hashlib.sha256(task.source_bytes).hexdigest() != task.source_sha256:
        raise ValueError("契约缺少已验证的原始快照。")
    return {"document": task.source_bytes.decode("utf-8"), "sha256": task.source_sha256}


def restore_task(packet: Any, cwd: str) -> HeadlessTask:
    if (not isinstance(packet, dict) or set(packet) != {"document", "sha256"}
            or not isinstance(packet["document"], str) or len(packet["document"]) > MAX_BYTES):
        raise ValueError("无效的任务契约快照。")
    try:
        task = parse_headless_task(packet["document"].encode("utf-8"), cwd)
    except UnicodeEncodeError as error:
        raise ValueError("无效的任务契约编码。") from error
    if task.source_sha256 != packet["sha256"]:
        raise ValueError("任务契约快照摘要不匹配。")
    return task


@contextmanager
def bind_task(runtime: dict[str, Any], task: HeadlessTask | None):
    """Restore only task-owned fields, including on failure; keep new traces."""
    if task is None:
        yield runtime
        return
    applied = task.apply(runtime)
    keys = set(task.fields()) | {"_headlessTaskFile"}
    previous = {key: runtime[key] for key in keys if key in runtime}
    previous_trace = runtime.get("lastRuntimeTrace")
    runtime.update({key: applied[key] for key in keys})
    try:
        yield runtime
    finally:
        trace = runtime.get("lastRuntimeTrace")
        if isinstance(trace, dict) and trace is not previous_trace:
            trace["task_file"] = task.summary()
        for key in keys:
            runtime.pop(key, None)
        runtime.update(previous)
