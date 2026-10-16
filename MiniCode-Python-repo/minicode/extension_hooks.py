"""Lifecycle hook contributions provided by trusted MiniCode extensions."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode.extension_tools import (
    _bounded_float,
    _bounded_int,
    _minimal_environment,
    _prepare_command,
)
from minicode.extensions import (
    ExtensionContribution,
    ExtensionRegistry,
    ExtensionValidationError,
    extension_secret_environment,
    resolve_secret_value,
)
from minicode.hooks import HookContext, HookEvent, HookManager, get_hook_manager

DEFAULT_HOOK_TIMEOUT_SECONDS = 10.0
MAX_HOOK_TIMEOUT_SECONDS = 120.0
DEFAULT_HOOK_PAYLOAD_BYTES = 1_000_000
DEFAULT_HOOK_OUTPUT_BYTES = 256_000


@dataclass(frozen=True, slots=True)
class ExtensionHookLoadError:
    extension_id: str
    hook_name: str
    error: str


@dataclass(slots=True)
class _ActiveHooks:
    count: int
    unregister: list[Any]


_activation_lock = threading.Lock()
_active_by_workspace: dict[tuple[int, str], _ActiveHooks] = {}


def _hook_event(value: Any) -> HookEvent:
    normalized = str(value or "").strip().lower()
    try:
        return HookEvent(normalized)
    except ValueError as exc:
        supported = ", ".join(event.value for event in HookEvent)
        raise ExtensionValidationError(
            [f"Unsupported extension hook event {normalized!r}; expected one of: {supported}."]
        ) from exc


def _matches(ctx: HookContext, descriptor: Mapping[str, Any]) -> bool:
    match = descriptor.get("match", {})
    if not isinstance(match, dict):
        return False
    tools = match.get("tools", [])
    if (
        isinstance(tools, list)
        and tools
        and str(ctx.tool_name or "") not in {str(item) for item in tools}
    ):
        return False
    return not (bool(match.get("errorsOnly", False)) and not ctx.is_error)


def _hook_handler(
    contribution: ExtensionContribution,
    descriptor: Mapping[str, Any],
    *,
    secret_environ: Mapping[str, str],
):
    if "process:spawn" not in contribution.extension.granted_permissions:
        raise ExtensionValidationError(
            ["Extension hooks require the declared and granted 'process:spawn' permission."]
        )
    command = _prepare_command(contribution, descriptor)
    timeout_seconds = _bounded_float(
        descriptor.get("timeoutSeconds"),
        default=DEFAULT_HOOK_TIMEOUT_SECONDS,
        minimum=0.1,
        maximum=MAX_HOOK_TIMEOUT_SECONDS,
    )
    max_payload_bytes = _bounded_int(
        descriptor.get("maxPayloadBytes"),
        default=DEFAULT_HOOK_PAYLOAD_BYTES,
        minimum=1_024,
        maximum=10_000_000,
    )
    max_output_bytes = _bounded_int(
        descriptor.get("maxOutputBytes"),
        default=DEFAULT_HOOK_OUTPUT_BYTES,
        minimum=1_024,
        maximum=2_000_000,
    )
    raw_env = descriptor.get("env", {})
    if raw_env not in ({}, None) and not isinstance(raw_env, dict):
        raise ExtensionValidationError(["Extension hook 'env' must be an object."])
    resolved_env = resolve_secret_value(raw_env or {}, environ=secret_environ)
    inherit_env = "env:inherit" in contribution.extension.granted_permissions

    def handler(ctx: HookContext) -> str:
        if not _matches(ctx, descriptor):
            return "skipped"
        request = {
            "protocolVersion": 1,
            "hook": contribution.name,
            "event": ctx.event.value,
            "timestamp": ctx.timestamp,
            "data": ctx.data,
            "metadata": ctx.metadata,
        }
        payload = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(payload) > max_payload_bytes:
            raise RuntimeError(
                f"Extension hook payload exceeds {max_payload_bytes} bytes."
            )
        env = os.environ.copy() if inherit_env else _minimal_environment()
        env.update({str(key): str(value) for key, value in dict(resolved_env).items()})
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = 0x08000000
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.run(
                    command,
                    cwd=contribution.extension.root,
                    env=env,
                    input=payload,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout_seconds,
                    check=False,
                    **popen_kwargs,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"Extension hook timed out after {timeout_seconds:g} seconds."
                ) from exc
            stdout_size = stdout_file.tell()
            stderr_size = stderr_file.tell()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(min(stdout_size, max_output_bytes + 1))
            stderr = stderr_file.read(min(stderr_size, max_output_bytes + 1))
        if stdout_size > max_output_bytes or stderr_size > max_output_bytes:
            raise RuntimeError(
                f"Extension hook output exceeds {max_output_bytes} bytes."
            )
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Extension hook exited with code {process.returncode}: {detail[:2_000]}"
            )
        return stdout.decode("utf-8", errors="replace").strip()

    return handler


def _load_extension_hooks(
    cwd: str | Path,
    manager: HookManager,
) -> tuple[list[Any], list[ExtensionHookLoadError]]:
    unregister: list[Any] = []
    errors: list[ExtensionHookLoadError] = []
    secret_environ = extension_secret_environment(cwd)
    for contribution in ExtensionRegistry(cwd).contributions("hooks"):
        try:
            descriptor = contribution.value
            if not isinstance(descriptor, dict):
                raise ExtensionValidationError(
                    ["Extension hook contribution must be an object."]
                )
            event = _hook_event(descriptor.get("event"))
            handler = _hook_handler(
                contribution,
                descriptor,
                secret_environ=secret_environ,
            )
            description = str(
                descriptor.get("description")
                or f"{contribution.extension.id}:{contribution.name}"
            ).strip()
            unregister.append(manager.register(event, handler, description))
        except Exception as exc:  # noqa: BLE001 - isolate one broken hook
            errors.append(
                ExtensionHookLoadError(
                    extension_id=contribution.extension.id,
                    hook_name=contribution.name,
                    error=str(exc)[:1_000],
                )
            )
    return unregister, errors


def activate_extension_hooks(
    cwd: str | Path,
    *,
    manager: HookManager | None = None,
) -> dict[str, Any]:
    """Register active extension hooks once per workspace with ref-counted cleanup."""

    hook_manager = manager or get_hook_manager()
    workspace_key = (id(hook_manager), str(Path(cwd).resolve()).casefold())
    should_fire_startup = False
    with _activation_lock:
        active = _active_by_workspace.get(workspace_key)
        if active is not None:
            active.count += 1
            errors: list[ExtensionHookLoadError] = []
            registered_count = len(active.unregister)
        else:
            unregister, errors = _load_extension_hooks(cwd, hook_manager)
            active = _ActiveHooks(count=1, unregister=unregister)
            _active_by_workspace[workspace_key] = active
            registered_count = len(unregister)
            should_fire_startup = bool(unregister)
    if should_fire_startup:
        hook_manager.fire_sync(HookEvent.STARTUP, cwd=str(Path(cwd).resolve()))

    released = False

    def dispose() -> None:
        nonlocal released
        if released:
            return
        released = True
        callbacks: list[Any] = []
        with _activation_lock:
            current = _active_by_workspace.get(workspace_key)
            if current is None:
                return
            current.count -= 1
            if current.count <= 0:
                callbacks = list(current.unregister)
                _active_by_workspace.pop(workspace_key, None)
        if callbacks:
            hook_manager.fire_sync(HookEvent.SHUTDOWN, cwd=str(Path(cwd).resolve()))
            for unregister_callback in reversed(callbacks):
                unregister_callback()

    return {
        "count": registered_count,
        "errors": errors,
        "dispose": dispose,
    }
