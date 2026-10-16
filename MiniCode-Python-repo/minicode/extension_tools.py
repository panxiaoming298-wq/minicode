"""Isolated subprocess Tool provider for trusted MiniCode extensions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode.extensions import (
    ExtensionContribution,
    ExtensionRegistry,
    ExtensionValidationError,
    extension_secret_environment,
    resolve_secret_value,
)
from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.tooling import (
    ToolCapability,
    ToolContext,
    ToolDefinition,
    ToolMetadata,
    ToolResult,
)

EXTERNAL_TOOL_PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_INPUT_BYTES = 1_000_000
DEFAULT_MAX_OUTPUT_BYTES = 2_000_000

_SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "zsh",
}


@dataclass(frozen=True, slots=True)
class ExternalToolLoadError:
    extension_id: str
    tool_name: str
    error: str


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _effect(value: Any) -> ToolEffect:
    normalized = str(value or ToolEffect.PRIVILEGED.value).strip().lower()
    try:
        return ToolEffect(normalized)
    except ValueError as exc:
        supported = ", ".join(effect.value for effect in ToolEffect)
        raise ExtensionValidationError(
            [f"Unsupported external tool effect {normalized!r}; expected one of: {supported}."]
        ) from exc


def _replay_policy(value: Any, effect: ToolEffect) -> ReplayPolicy:
    if value in (None, ""):
        return ReplayPolicy.SAFE if effect is ToolEffect.READ_ONLY else ReplayPolicy.NEVER_AUTO_RETRY
    normalized = str(value).strip().lower()
    try:
        return ReplayPolicy(normalized)
    except ValueError as exc:
        supported = ", ".join(policy.value for policy in ReplayPolicy)
        raise ExtensionValidationError(
            [f"Unsupported replay policy {normalized!r}; expected one of: {supported}."]
        ) from exc


def _schema_validator(schema: Mapping[str, Any]):
    expected_type = str(schema.get("type") or "object")
    required = tuple(str(item) for item in schema.get("required", []) if str(item).strip())
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    allow_extra = schema.get("additionalProperties", True) is not False

    def validate(value: Any) -> Any:
        if expected_type == "object":
            if not isinstance(value, dict):
                raise ValueError("input must be an object")
            missing = [key for key in required if key not in value]
            if missing:
                raise ValueError("missing required field(s): " + ", ".join(missing))
            if not allow_extra:
                extras = sorted(set(value) - set(properties))
                if extras:
                    raise ValueError("unexpected field(s): " + ", ".join(extras))
            for key, descriptor in properties.items():
                if key not in value or not isinstance(descriptor, dict):
                    continue
                _validate_json_type(key, value[key], descriptor)
            return dict(value)
        _validate_json_type("input", value, {"type": expected_type})
        return value

    return validate


def _validate_json_type(name: str, value: Any, descriptor: Mapping[str, Any]) -> None:
    expected = descriptor.get("type")
    if isinstance(expected, list):
        expected_types = {str(item) for item in expected}
    elif expected:
        expected_types = {str(expected)}
    else:
        expected_types = set()
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected_types and not any(matches.get(item, False) for item in expected_types):
        raise ValueError(f"field {name!r} must have type {' or '.join(sorted(expected_types))}")
    if "enum" in descriptor and isinstance(descriptor["enum"], list) and value not in descriptor["enum"]:
        raise ValueError(f"field {name!r} must be one of {descriptor['enum']!r}")


def _minimal_environment() -> dict[str, str]:
    names = {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    return {name: value for name, value in os.environ.items() if name.upper() in names}


def _prepare_command(contribution: ExtensionContribution, descriptor: Mapping[str, Any]) -> list[str]:
    raw = descriptor.get("command")
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) and item.strip() for item in raw):
        raise ExtensionValidationError(["External tool command must be a non-empty string array."])
    command = [item.strip() for item in raw]
    first = command[0]
    extension_root = Path(contribution.extension.root)

    if first in {"python", "python3", "py"}:
        command[0] = sys.executable
    elif first in {"node", "node.exe"}:
        resolved = shutil.which(first)
        if not resolved:
            raise ExtensionValidationError([f"External tool executable was not found: {first}"])
        command[0] = resolved
    elif Path(first).is_absolute():
        executable = Path(first).resolve()
        if not executable.is_file():
            raise ExtensionValidationError([f"External tool executable does not exist: {first}"])
        command[0] = str(executable)
    elif any(separator in first for separator in ("/", "\\")) or Path(first).suffix.lower() in {
        ".py", ".js", ".cmd", ".bat", ".ps1", ".sh"
    }:
        executable = (extension_root / first).resolve()
        try:
            executable.relative_to(extension_root.resolve())
        except ValueError as exc:
            raise ExtensionValidationError([f"External tool command escapes its package: {first}"]) from exc
        if not executable.is_file() or executable.is_symlink():
            raise ExtensionValidationError([f"External tool command does not exist: {first}"])
        suffix = executable.suffix.lower()
        if suffix == ".py":
            command = [sys.executable, str(executable), *command[1:]]
        elif os.name == "nt" and suffix in {".cmd", ".bat"}:
            command = ["cmd.exe", "/d", "/s", "/c", str(executable), *command[1:]]
        elif os.name == "nt" and suffix == ".ps1":
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(executable), *command[1:]]
        else:
            command[0] = str(executable)
    else:
        resolved = shutil.which(first)
        if not resolved:
            raise ExtensionValidationError([f"External tool executable was not found: {first}"])
        command[0] = resolved

    executable_name = Path(command[0]).name.casefold()
    if (
        executable_name
        in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}
        and len(command) > 1
        and not command[1].startswith("-")
        and Path(command[1]).suffix.casefold() == ".py"
    ):
        script = (extension_root / command[1]).resolve()
        try:
            script.relative_to(extension_root.resolve())
        except ValueError as exc:
            raise ExtensionValidationError(
                [f"External tool script escapes its package: {command[1]}"]
            ) from exc
        if not script.is_file() or script.is_symlink():
            raise ExtensionValidationError(
                [f"External tool script does not exist: {command[1]}"]
            )
        command[1] = str(script)

    executable_name = Path(command[0]).name.casefold()
    if executable_name in _SHELL_EXECUTABLES and "shell:execute" not in contribution.extension.granted_permissions:
        raise ExtensionValidationError(
            ["Shell-based extension tools require the declared and granted 'shell:execute' permission."]
        )
    return command


def _external_tool_runner(
    contribution: ExtensionContribution,
    descriptor: Mapping[str, Any],
    *,
    command: list[str],
    effect: ToolEffect,
    secret_environ: Mapping[str, str],
):
    timeout_seconds = _bounded_float(
        descriptor.get("timeoutSeconds"),
        default=DEFAULT_TIMEOUT_SECONDS,
        minimum=0.1,
        maximum=MAX_TIMEOUT_SECONDS,
    )
    max_input_bytes = _bounded_int(
        descriptor.get("maxInputBytes"),
        default=DEFAULT_MAX_INPUT_BYTES,
        minimum=1_024,
        maximum=10_000_000,
    )
    max_output_bytes = _bounded_int(
        descriptor.get("maxOutputBytes"),
        default=DEFAULT_MAX_OUTPUT_BYTES,
        minimum=1_024,
        maximum=20_000_000,
    )

    raw_env = descriptor.get("env", {})
    if raw_env not in ({}, None) and not isinstance(raw_env, dict):
        raise ExtensionValidationError(["External tool 'env' must be an object."])
    resolved_env = resolve_secret_value(raw_env or {}, environ=secret_environ)
    inherit_env = "env:inherit" in contribution.extension.granted_permissions

    def run(input_data: Any, context: ToolContext) -> ToolResult:
        request = {
            "protocolVersion": EXTERNAL_TOOL_PROTOCOL_VERSION,
            "tool": contribution.name,
            "input": input_data,
            "context": {
                "cwd": context.cwd,
                "runId": context.run_id or "",
                "nodeId": context.node_id or "",
                "attemptId": context.attempt_id or "",
                "toolCallId": context.tool_call_id or "",
                "idempotencyKey": context.idempotency_key or "",
            },
        }
        request_bytes = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(request_bytes) > max_input_bytes:
            return ToolResult(
                ok=False,
                output=f"External tool input exceeds {max_input_bytes} bytes.",
            )

        env = os.environ.copy() if inherit_env else _minimal_environment()
        env.update({str(key): str(value) for key, value in dict(resolved_env).items()})
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = 0x08000000

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=contribution.extension.root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                **popen_kwargs,
            )
            try:
                process.communicate(input=request_bytes, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                return ToolResult(
                    ok=False,
                    output=f"External tool timed out after {timeout_seconds:g} seconds.",
                    timedOut=True,
                    ambiguousSideEffect=effect is not ToolEffect.READ_ONLY,
                )

            stdout_size = stdout_file.tell()
            stderr_size = stderr_file.tell()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(min(stdout_size, max_output_bytes + 1))
            stderr = stderr_file.read(min(stderr_size, max_output_bytes + 1))

        if stdout_size > max_output_bytes or stderr_size > max_output_bytes:
            return ToolResult(
                ok=False,
                output=f"External tool output exceeds {max_output_bytes} bytes.",
                ambiguousSideEffect=effect is not ToolEffect.READ_ONLY,
            )
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(
                ok=False,
                output=f"External tool exited with code {process.returncode}: {detail[:4_000]}",
                ambiguousSideEffect=effect is not ToolEffect.READ_ONLY,
            )
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ToolResult(ok=False, output=f"External tool returned invalid JSON: {exc}")
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            return ToolResult(
                ok=False,
                output="External tool response must be an object containing boolean 'ok'.",
            )
        output = response.get("output", "")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        return ToolResult(
            ok=bool(response["ok"]),
            output=output,
            timedOut=bool(response.get("timedOut", False)),
            ambiguousSideEffect=bool(response.get("ambiguousSideEffect", False)),
        )

    return run


def _tool_definition(
    contribution: ExtensionContribution,
    *,
    secret_environ: Mapping[str, str],
) -> ToolDefinition:
    descriptor = contribution.value
    if not isinstance(descriptor, dict):
        raise ExtensionValidationError(["External tool contribution must be an object."])
    if "process:spawn" not in contribution.extension.granted_permissions:
        raise ExtensionValidationError(
            ["External tools require the declared and granted 'process:spawn' permission."]
        )
    local_name = str(descriptor.get("name") or descriptor.get("id") or contribution.name).strip()
    if not local_name:
        raise ExtensionValidationError(["External tool name cannot be empty."])
    effect = _effect(descriptor.get("effect"))
    context_kind = descriptor.get("contextResultKind", "opaque")
    if context_kind not in ("opaque", "read_data") or (context_kind == "read_data" and effect is not ToolEffect.READ_ONLY):
        raise ExtensionValidationError(["contextResultKind must be opaque, or read_data on a read_only tool."])
    command = _prepare_command(contribution, descriptor)
    input_schema = descriptor.get("inputSchema", {"type": "object"})
    if not isinstance(input_schema, dict):
        raise ExtensionValidationError(["External tool inputSchema must be an object."])
    replay = _replay_policy(descriptor.get("replayPolicy"), effect)
    capabilities = {ToolCapability.REQUIRES_PERMISSION}
    if effect is ToolEffect.READ_ONLY:
        capabilities.add(ToolCapability.READ_ONLY)
    if bool(descriptor.get("concurrencySafe", effect is ToolEffect.READ_ONLY)):
        capabilities.add(ToolCapability.CONCURRENCY_SAFE)
    if bool(descriptor.get("destructive", False)):
        capabilities.add(ToolCapability.DESTRUCTIVE)

    extension_segment = contribution.extension.id.replace(".", "_").replace("-", "_")
    tool_segment = local_name.replace("-", "_").replace(".", "_")
    public_name = f"ext__{extension_segment}__{tool_segment}"
    description = str(descriptor.get("description") or f"Extension tool {local_name}.").strip()
    max_output_chars = _bounded_int(
        descriptor.get("maxOutputChars"),
        default=10_000,
        minimum=1_000,
        maximum=100_000,
    )
    metadata = ToolMetadata(
        name=public_name,
        description=description,
        capabilities=capabilities,
        input_schema=dict(input_schema),
        is_enabled=True,
        max_result_size_chars=max_output_chars,
        tags=["extension", contribution.extension.id, *[str(item) for item in descriptor.get("tags", [])]],
        effect=effect,
        replay_policy=replay,
        context_result_kind=context_kind,
    )
    return ToolDefinition(
        name=public_name,
        description=description,
        input_schema=dict(input_schema),
        validator=_schema_validator(input_schema),
        run=_external_tool_runner(
            contribution,
            descriptor,
            command=command,
            effect=effect,
            secret_environ=secret_environ,
        ),
        metadata=metadata,
    )


def create_extension_tools(cwd: str | Path) -> dict[str, Any]:
    """Build ToolDefinitions and bounded diagnostics for active extensions."""

    tools: list[ToolDefinition] = []
    errors: list[ExternalToolLoadError] = []
    names: set[str] = set()
    secret_environ = extension_secret_environment(cwd)
    for contribution in ExtensionRegistry(cwd).contributions("tools"):
        try:
            tool = _tool_definition(contribution, secret_environ=secret_environ)
            if tool.name in names:
                raise ExtensionValidationError([f"Duplicate extension tool name: {tool.name}"])
            names.add(tool.name)
            tools.append(tool)
        except Exception as exc:  # noqa: BLE001 - one extension cannot remove core tools
            errors.append(
                ExternalToolLoadError(
                    extension_id=contribution.extension.id,
                    tool_name=contribution.name,
                    error=str(exc)[:1_000],
                )
            )
    return {"tools": tools, "errors": errors}
