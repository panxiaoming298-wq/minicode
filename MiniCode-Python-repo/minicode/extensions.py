"""Unified, local-first extension packaging for MiniCode.

An extension package is the distribution unit.  Skills, MCP servers, model
provider presets, tools, hooks, commands, agents, and UI descriptors are typed
contributions within that package.  Discovery never imports Python code and
activation never treats installation as trust.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import minicode.config as config_module

EXTENSION_API_VERSION = "minicode.dev/v1"
EXTENSION_LOCK_VERSION = 1
EXTENSION_STATE_VERSION = 1

_EXTENSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?(?:\.(0|[1-9][0-9]*))?(?:[-+][A-Za-z0-9.-]+)?$")

CONTRIBUTION_KINDS = frozenset(
    {
        "skills",
        "mcpServers",
        "providers",
        "tools",
        "hooks",
        "commands",
        "agents",
        "ui",
    }
)
_LIST_CONTRIBUTIONS = frozenset({"skills", "tools", "hooks", "commands", "agents", "ui"})
_MAPPING_CONTRIBUTIONS = frozenset({"mcpServers", "providers"})
_RESERVED_PROVIDER_IDS = frozenset(
    {"anthropic", "openai", "deepseek", "bailian", "openrouter", "custom", "mock"}
)


class ExtensionError(RuntimeError):
    """Base error for extension packaging and activation."""


class ExtensionValidationError(ExtensionError):
    """Raised when a bundle cannot satisfy the manifest contract."""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(str(error) for error in errors if str(error).strip())
        super().__init__("; ".join(self.errors) or "Invalid extension package.")


class ExtensionTrustError(ExtensionError):
    """Raised when an executable contribution has not been trusted."""


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    """Normalized extension package manifest plus local activation state."""

    id: str
    name: str
    scope: str
    path: str
    root: str
    version: str = ""
    api_version: str = EXTENSION_API_VERSION
    description: str = ""
    publisher: str = ""
    enabled: bool = True
    trusted: bool = False
    granted_permissions: tuple[str, ...] = ()
    entrypoint: str = ""
    permissions: tuple[str, ...] = ()
    activation_events: tuple[str, ...] = ()
    dependencies: dict[str, str] = field(default_factory=dict)
    contributes: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    integrity: str = ""
    declared_integrity: str = ""
    legacy: bool = False
    validation_errors: tuple[str, ...] = ()
    health: str = "ready"
    health_detail: str = ""
    shadowed_by: str = ""

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.id}"

    @property
    def contribution_kinds(self) -> tuple[str, ...]:
        return tuple(
            kind for kind in sorted(CONTRIBUTION_KINDS)
            if self.contributes.get(kind)
        )

    @property
    def requires_trust(self) -> bool:
        return bool(self.entrypoint or self.contribution_kinds)

    @property
    def is_active(self) -> bool:
        return self.health == "ready" and self.enabled


@dataclass(frozen=True, slots=True)
class ExtensionContribution:
    extension: ExtensionManifest
    kind: str
    name: str
    value: Any


@dataclass(frozen=True, slots=True)
class ExtensionHealthCheck:
    name: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class ExtensionRuntimeDiagnostic:
    extension_id: str
    contribution_kind: str
    contribution_name: str
    status: str
    detail: str


def extension_root(scope: str, cwd: str | Path) -> Path:
    normalized = _normalize_scope(scope)
    if normalized == "project":
        return config_module.project_extensions_dir(cwd)
    return config_module.MINI_CODE_EXTENSIONS_DIR


def extension_state_path(scope: str, cwd: str | Path) -> Path:
    return extension_root(scope, cwd).parent / "extensions-state.json"


def extension_lock_path(scope: str, cwd: str | Path) -> Path:
    return extension_root(scope, cwd).parent / "extensions.lock"


def extension_search_roots(cwd: str | Path) -> list[tuple[str, Path]]:
    return [
        ("global", extension_root("global", cwd)),
        ("project", extension_root("project", cwd)),
    ]


def _normalize_scope(scope: str) -> str:
    value = str(scope or "").strip().lower()
    if value == "user":
        value = "global"
    if value not in {"global", "project"}:
        raise ValueError("Extension scope must be 'global' or 'project'.")
    return value


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtensionValidationError([f"Cannot read JSON file {path}: {exc}"]) from exc
    if not isinstance(payload, dict):
        raise ExtensionValidationError([f"JSON file {path} must contain an object."])
    return payload


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_state(scope: str, cwd: str | Path) -> dict[str, Any]:
    path = extension_state_path(scope, cwd)
    try:
        payload = _read_json_object(path)
    except ExtensionValidationError:
        return {"stateVersion": EXTENSION_STATE_VERSION, "extensions": {}}
    extensions = payload.get("extensions", {})
    if not isinstance(extensions, dict):
        extensions = {}
    return {
        "stateVersion": EXTENSION_STATE_VERSION,
        "extensions": dict(extensions),
    }


def _write_state(scope: str, cwd: str | Path, payload: Mapping[str, Any]) -> None:
    extensions = payload.get("extensions", {})
    _atomic_write_json(
        extension_state_path(scope, cwd),
        {
            "stateVersion": EXTENSION_STATE_VERSION,
            "extensions": dict(extensions) if isinstance(extensions, Mapping) else {},
        },
    )


def _read_lock(scope: str, cwd: str | Path) -> dict[str, Any]:
    path = extension_lock_path(scope, cwd)
    try:
        payload = _read_json_object(path)
    except ExtensionValidationError:
        return {"lockVersion": EXTENSION_LOCK_VERSION, "extensions": {}}
    extensions = payload.get("extensions", {})
    if not isinstance(extensions, dict):
        extensions = {}
    return {"lockVersion": EXTENSION_LOCK_VERSION, "extensions": dict(extensions)}


def _write_lock(scope: str, cwd: str | Path, payload: Mapping[str, Any]) -> None:
    extensions = payload.get("extensions", {})
    _atomic_write_json(
        extension_lock_path(scope, cwd),
        {
            "lockVersion": EXTENSION_LOCK_VERSION,
            "extensions": dict(extensions) if isinstance(extensions, Mapping) else {},
        },
    )


def _coerce_string_list(value: Any, *, field_name: str, errors: list[str]) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        errors.append(f"'{field_name}' must be an array of strings.")
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text:
            errors.append(f"'{field_name}' cannot contain empty values.")
            continue
        if text not in seen:
            result.append(text)
            seen.add(text)
    return tuple(result)


def _normalize_contributes(raw: Any, errors: list[str]) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        errors.append("'contributes' must be an object.")
        return {}
    unknown = sorted(set(raw) - CONTRIBUTION_KINDS)
    if unknown:
        errors.append("Unsupported contribution kinds: " + ", ".join(unknown))
    normalized: dict[str, Any] = {}
    for kind in _LIST_CONTRIBUTIONS:
        value = raw.get(kind, [])
        if value in (None, ""):
            value = []
        if not isinstance(value, list):
            errors.append(f"'contributes.{kind}' must be an array.")
            continue
        normalized[kind] = list(value)
    for kind in _MAPPING_CONTRIBUTIONS:
        value = raw.get(kind, {})
        if value in (None, ""):
            value = {}
        if not isinstance(value, dict):
            errors.append(f"'contributes.{kind}' must be an object.")
            continue
        normalized[kind] = dict(value)
    return {key: value for key, value in normalized.items() if value}


def _normalize_dependencies(raw: Any, errors: list[str]) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        errors.append("'dependencies' must be an object mapping extension IDs to versions.")
        return {}
    result: dict[str, str] = {}
    for raw_id, raw_constraint in raw.items():
        extension_id = str(raw_id or "").strip()
        constraint = str(raw_constraint or "*").strip() or "*"
        if not _EXTENSION_ID_RE.fullmatch(extension_id):
            errors.append(f"Invalid dependency extension ID: {extension_id!r}.")
            continue
        result[extension_id] = constraint
    return result


def _is_secret_reference(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"secretRef"}
        or isinstance(value, str)
        and value.startswith("secret://env/")
    )


def _sensitive_field_name(value: Any) -> bool:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    tokens = {
        token
        for token in re.split(r"[^A-Za-z0-9]+", separated.upper())
        if token
    }
    return bool(tokens & {"AUTH", "AUTHORIZATION", "CREDENTIAL", "KEY", "PASSWORD", "SECRET", "TOKEN"})


def _validate_secret_references(value: Any, errors: list[str], *, label: str = "contributes") -> None:
    if isinstance(value, dict):
        if set(value) == {"secretRef"}:
            reference = str(value.get("secretRef") or "").strip()
            if not _SECRET_NAME_RE.fullmatch(reference):
                errors.append(f"{label} has an invalid environment secret reference: {reference!r}.")
            return
        for name, item in value.items():
            _validate_secret_references(item, errors, label=f"{label}.{name}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_secret_references(item, errors, label=f"{label}[{index}]")
        return
    if isinstance(value, str) and value.startswith("secret://env/"):
        reference = value[len("secret://env/") :].strip()
        if not _SECRET_NAME_RE.fullmatch(reference):
            errors.append(f"{label} has an invalid environment secret reference: {reference!r}.")


def _safe_bundle_path(root: Path, raw_path: str, *, label: str) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise ExtensionValidationError([f"{label} path cannot be empty."])
    if value == ".":
        if root.is_symlink():
            raise ExtensionValidationError([f"{label} path cannot use a symbolic-link root."])
        return root
    relative = Path(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ExtensionValidationError([f"{label} path must be a safe relative path: {value!r}."])
    candidate = root.joinpath(relative)
    resolved_root = root.resolve()
    try:
        candidate.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise ExtensionValidationError([f"{label} path escapes the extension root: {value!r}."]) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ExtensionValidationError([f"{label} path crosses a symbolic link: {value!r}."])
    return candidate


def _validate_command_script_path(
    root: Path,
    command: list[str],
    *,
    label: str,
    errors: list[str],
) -> None:
    """Validate package script operands used with common interpreters."""

    executable = Path(command[0]).name.casefold()
    for suffix in (".exe", ".cmd", ".bat"):
        if executable.endswith(suffix):
            executable = executable[: -len(suffix)]
            break
    raw_script = ""
    expected_suffixes: tuple[str, ...] = ()
    if executable in {"python", "python3", "py"} and len(command) > 1:
        raw_script = command[1]
        expected_suffixes = (".py",)
    elif executable in {"node", "deno", "bun"} and len(command) > 1:
        raw_script = command[1]
        expected_suffixes = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
    elif executable in {"bash", "sh", "zsh"} and len(command) > 1:
        raw_script = command[1]
        expected_suffixes = (".sh",)
    elif executable in {"powershell", "pwsh"}:
        lowered = [part.casefold() for part in command]
        if "-file" in lowered and lowered.index("-file") + 1 < len(command):
            raw_script = command[lowered.index("-file") + 1]
            expected_suffixes = (".ps1",)
    elif executable == "cmd" and len(command) > 2 and command[1].casefold() in {"/c", "/k"}:
        raw_script = command[2]
        expected_suffixes = (".cmd", ".bat")

    if not raw_script or raw_script.startswith("-"):
        return
    if Path(raw_script).suffix.casefold() not in expected_suffixes:
        return
    try:
        script = _safe_bundle_path(root, raw_script, label=f"{label} script")
        if not script.is_file():
            errors.append(f"{label} script does not exist: {raw_script!r}.")
    except ExtensionValidationError as exc:
        errors.extend(exc.errors)


def _validate_contribution_paths(root: Path, contributes: Mapping[str, Any], errors: list[str]) -> None:
    for index, item in enumerate(contributes.get("skills", [])):
        if isinstance(item, str):
            raw_path = item
        elif isinstance(item, dict):
            raw_path = str(item.get("path", ""))
        else:
            errors.append(f"contributes.skills[{index}] must be a path or object.")
            continue
        try:
            skill_root = _safe_bundle_path(root, raw_path, label=f"skills[{index}]")
        except ExtensionValidationError as exc:
            errors.extend(exc.errors)
            continue
        if not (skill_root / "SKILL.md").is_file():
            errors.append(f"Skill contribution {raw_path!r} does not contain SKILL.md.")

    path_fields = {
        "tools": "command",
        "hooks": "command",
    }
    for kind, command_field in path_fields.items():
        for index, item in enumerate(contributes.get(kind, [])):
            if not isinstance(item, dict):
                errors.append(f"contributes.{kind}[{index}] must be an object.")
                continue
            command = item.get(command_field)
            if command is None:
                errors.append(f"contributes.{kind}[{index}] must declare '{command_field}'.")
                continue
            if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
                errors.append(f"contributes.{kind}[{index}].{command_field} must be a non-empty string array.")
                continue
            _validate_command_script_path(
                root,
                command,
                label=f"contributes.{kind}[{index}]",
                errors=errors,
            )
            raw_env = item.get("env", {})
            if raw_env not in ({}, None) and not isinstance(raw_env, dict):
                errors.append(f"contributes.{kind}[{index}].env must be an object.")
            elif isinstance(raw_env, dict):
                for name, value in raw_env.items():
                    if _sensitive_field_name(name) and not _is_secret_reference(value):
                        errors.append(
                            f"contributes.{kind}[{index}].env.{name} must use a secretRef."
                        )

    hook_events = {
        "pre_tool_use", "post_tool_use", "agent_start", "agent_stop",
        "subagent_start", "subagent_stop", "session_save", "session_resume",
        "user_input", "assistant_output", "startup", "shutdown",
    }
    for index, item in enumerate(contributes.get("hooks", [])):
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "").strip().lower()
        if event not in hook_events:
            errors.append(
                f"contributes.hooks[{index}].event must be a supported HookEvent value."
            )
        if item.get("match") not in (None, {}) and not isinstance(item.get("match"), dict):
            errors.append(f"contributes.hooks[{index}].match must be an object.")

    for kind, text_field, file_field in (
        ("commands", "prompt", "promptFile"),
        ("agents", "instructions", "instructionsFile"),
    ):
        for index, item in enumerate(contributes.get(kind, [])):
            if not isinstance(item, dict):
                errors.append(f"contributes.{kind}[{index}] must be an object.")
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                errors.append(f"contributes.{kind}[{index}].name cannot be empty.")
            inline_value = str(item.get(text_field) or "").strip()
            file_value = str(item.get(file_field) or "").strip()
            if not inline_value and not file_value:
                errors.append(
                    f"contributes.{kind}[{index}] must declare '{text_field}' or '{file_field}'."
                )
            if file_value:
                try:
                    referenced = _safe_bundle_path(
                        root,
                        file_value,
                        label=f"{kind}[{index}].{file_field}",
                    )
                    if not referenced.is_file():
                        errors.append(
                            f"contributes.{kind}[{index}].{file_field} does not exist: {file_value!r}."
                        )
                except ExtensionValidationError as exc:
                    errors.extend(exc.errors)

    for index, item in enumerate(contributes.get("tools", [])):
        if not isinstance(item, dict):
            continue
        if not str(item.get("name") or "").strip():
            errors.append(f"contributes.tools[{index}].name cannot be empty.")
        effect = str(item.get("effect") or "privileged").strip().lower()
        if effect not in {
            "read_only", "workspace_write", "external_side_effect", "privileged"
        }:
            errors.append(f"contributes.tools[{index}].effect is unsupported: {effect!r}.")
        if item.get("inputSchema") not in (None, {}) and not isinstance(item.get("inputSchema"), dict):
            errors.append(f"contributes.tools[{index}].inputSchema must be an object.")
        context_kind = item.get("contextResultKind", "opaque")
        if context_kind not in ("opaque", "read_data") or (context_kind == "read_data" and effect != "read_only"):
            errors.append(f"contributes.tools[{index}].contextResultKind must be opaque, or read_data on a read_only tool.")

    for server_name, item in contributes.get("mcpServers", {}).items():
        if not isinstance(item, dict):
            errors.append(f"contributes.mcpServers.{server_name} must be an object.")
            continue
        has_command = bool(item.get("command"))
        has_url = bool(item.get("url"))
        transport = str(item.get("transport") or "").strip().lower()
        if has_command == has_url:
            errors.append(
                f"contributes.mcpServers.{server_name} must declare exactly one of command or url."
            )
        if has_command and not isinstance(item.get("command"), str):
            errors.append(f"contributes.mcpServers.{server_name}.command must be a string.")
        if has_command and transport not in {"", "stdio"}:
            errors.append(
                f"contributes.mcpServers.{server_name}.transport must be 'stdio' for a command server."
            )
        if has_url:
            parsed = urlparse(str(item.get("url") or ""))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"contributes.mcpServers.{server_name}.url must be an http(s) URL.")
            if transport not in {"", "http", "streamable-http"}:
                errors.append(
                    f"contributes.mcpServers.{server_name}.transport must be 'streamable-http'."
                )
        if item.get("env") not in (None, {}) and not isinstance(item.get("env"), dict):
            errors.append(f"contributes.mcpServers.{server_name}.env must be an object.")
        for field_name in ("env", "headers"):
            values = item.get(field_name, {})
            if values not in ({}, None) and not isinstance(values, dict):
                errors.append(
                    f"contributes.mcpServers.{server_name}.{field_name} must be an object."
                )
                continue
            if isinstance(values, dict):
                for name, value in values.items():
                    if _sensitive_field_name(name) and not _is_secret_reference(value):
                        errors.append(
                            f"contributes.mcpServers.{server_name}.{field_name}.{name} must use a secretRef."
                        )

    for provider_name, item in contributes.get("providers", {}).items():
        if not _PROVIDER_ID_RE.fullmatch(str(provider_name)):
            errors.append(
                f"contributes.providers key must be a lowercase provider ID: {provider_name!r}."
            )
        if str(provider_name) in _RESERVED_PROVIDER_IDS:
            errors.append(
                f"contributes.providers cannot replace built-in provider {provider_name!r}."
            )
        if not isinstance(item, dict):
            errors.append(f"contributes.providers.{provider_name} must be an object.")
            continue
        transport = str(item.get("transport") or "openai-compatible").strip().lower()
        if transport not in {"openai", "openai-compatible"}:
            errors.append(
                f"contributes.providers.{provider_name}.transport is unsupported: {transport!r}."
            )
        base_url = str(item.get("baseUrl") or "").strip()
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append(f"contributes.providers.{provider_name}.baseUrl must be an http(s) URL.")
        if not _is_secret_reference(item.get("apiKey")):
            errors.append(
                f"contributes.providers.{provider_name}.apiKey must use a secretRef."
            )

    for index, item in enumerate(contributes.get("ui", [])):
        if not isinstance(item, dict):
            errors.append(f"contributes.ui[{index}] must be an object.")
            continue
        raw_path = str(item.get("path") or "").strip()
        surface = str(item.get("surface") or "task").strip().lower()
        if surface not in {"task", "capabilities", "runtime", "context", "settings"}:
            errors.append(
                f"contributes.ui[{index}].surface must target a supported MiniCode surface."
            )
        if raw_path:
            try:
                ui_path = _safe_bundle_path(root, raw_path, label=f"ui[{index}]")
                if not ui_path.exists():
                    errors.append(f"UI contribution path does not exist: {raw_path!r}.")
            except ExtensionValidationError as exc:
                errors.extend(exc.errors)


def _contains_secret_reference(value: Any) -> bool:
    if isinstance(value, dict):
        if set(value) == {"secretRef"}:
            return True
        return any(_contains_secret_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_secret_reference(item) for item in value)
    return isinstance(value, str) and value.startswith("secret://env/")


def _required_contribution_permissions(contributes: Mapping[str, Any]) -> set[str]:
    required: set[str] = set()
    if contributes.get("tools") or contributes.get("hooks"):
        required.add("process:spawn")
    for item in contributes.get("tools", []):
        if not isinstance(item, dict):
            continue
        effect = str(item.get("effect") or "privileged").strip().lower()
        if effect == "workspace_write":
            required.add("workspace:write")
        elif effect == "external_side_effect":
            required.add("network:access")
    for value in contributes.get("mcpServers", {}).values():
        if not isinstance(value, dict):
            continue
        if value.get("command"):
            required.add("process:spawn")
        if value.get("url"):
            required.add("network:access")
    if contributes.get("providers"):
        required.add("network:access")
    if _contains_secret_reference(contributes):
        required.add("secrets:read")
    return required


def _tree_integrity(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ExtensionValidationError([f"Symbolic links are not allowed in extension packages: {path}."])
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if relative == "extension.json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                payload = dict(payload)
                payload.pop("integrity", None)
                payload.pop("signature", None)
                payload.pop("enabled", None)
                content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            else:
                content = path.read_bytes()
        else:
            content = path.read_bytes()
        digest.update(content)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def read_extension_manifest(
    manifest_path: str | Path,
    *,
    scope: str,
    cwd: str | Path,
) -> ExtensionManifest:
    """Read and normalize one manifest without importing executable code."""

    path = Path(manifest_path)
    root = path.parent
    errors: list[str] = []
    try:
        payload = _read_json_object(path)
    except ExtensionValidationError as exc:
        payload = {}
        errors.extend(exc.errors)

    raw_id = str(payload.get("id") or payload.get("name") or root.name).strip()
    display_name = str(payload.get("name") or raw_id).strip()
    if not _EXTENSION_ID_RE.fullmatch(raw_id):
        errors.append(
            "Extension 'id' must start with an alphanumeric character and contain only letters, digits, '.', '_' or '-'."
        )
    if not display_name:
        errors.append("Extension 'name' cannot be empty.")

    raw_api_version = str(payload.get("apiVersion") or "").strip()
    legacy = not raw_api_version
    api_version = raw_api_version or EXTENSION_API_VERSION
    if api_version != EXTENSION_API_VERSION:
        errors.append(
            f"Unsupported extension apiVersion {api_version!r}; expected {EXTENSION_API_VERSION!r}."
        )

    version = str(payload.get("version") or "").strip()
    if version and not _VERSION_RE.fullmatch(version):
        errors.append(f"Invalid extension version: {version!r}.")
    if not version:
        version = "0.0.0"

    contributes = _normalize_contributes(payload.get("contributes", {}), errors)
    _validate_contribution_paths(root, contributes, errors)
    _validate_secret_references(contributes, errors)
    permissions = _coerce_string_list(payload.get("permissions", []), field_name="permissions", errors=errors)
    if not legacy:
        missing_permissions = sorted(
            _required_contribution_permissions(contributes) - set(permissions)
        )
        if missing_permissions:
            errors.append(
                "Extension contributions require declared permissions: "
                + ", ".join(missing_permissions)
            )
    activation_events = _coerce_string_list(
        payload.get("activationEvents", []), field_name="activationEvents", errors=errors
    )
    dependencies = _normalize_dependencies(payload.get("dependencies", {}), errors)

    entrypoint = str(payload.get("entrypoint") or "").strip()
    if entrypoint:
        try:
            entrypoint_path = _safe_bundle_path(root, entrypoint, label="entrypoint")
            if not entrypoint_path.is_file():
                errors.append(f"Extension entrypoint does not exist: {entrypoint!r}.")
        except ExtensionValidationError as exc:
            errors.extend(exc.errors)

    try:
        integrity = _tree_integrity(root) if root.exists() else ""
    except (OSError, ExtensionValidationError) as exc:
        integrity = ""
        if isinstance(exc, ExtensionValidationError):
            errors.extend(exc.errors)
        else:
            errors.append(f"Cannot compute extension integrity: {exc}")
    declared_integrity = str(payload.get("integrity") or "").strip()
    if declared_integrity and declared_integrity != integrity:
        errors.append(
            f"Declared integrity {declared_integrity!r} does not match package integrity {integrity!r}."
        )

    normalized_scope = _normalize_scope(scope)
    state_payload = _read_state(normalized_scope, cwd)
    raw_state = state_payload.get("extensions", {}).get(raw_id, {})
    if not isinstance(raw_state, dict):
        raw_state = {}
    enabled = bool(raw_state.get("enabled", payload.get("enabled", True)))
    trusted = bool(raw_state.get("trusted", False))
    if normalized_scope == "project" and os.environ.get(
        "MINI_CODE_TRUST_PROJECT_EXTENSIONS", ""
    ).strip().lower() in {"1", "true", "yes", "on"}:
        trusted = True
    grants = _coerce_string_list(
        raw_state.get("grantedPermissions", []),
        field_name="grantedPermissions",
        errors=errors,
    )
    source = str(raw_state.get("source") or payload.get("source") or "").strip()

    health = "ready"
    detail = "Extension is ready for activation."
    if errors:
        health = "invalid"
        detail = errors[0]
    elif not enabled:
        health = "disabled"
        detail = "Extension is disabled."
    elif (entrypoint or contributes) and not trusted:
        health = "untrusted"
        detail = "Extension is installed but its contributions have not been trusted."
    elif permissions and not set(permissions).issubset(set(grants)):
        health = "permission_required"
        detail = "Extension has permissions that have not been granted."

    return ExtensionManifest(
        id=raw_id,
        name=display_name,
        scope=normalized_scope,
        path=str(path),
        root=str(root),
        version=version,
        api_version=api_version,
        description=str(payload.get("description") or "").strip(),
        publisher=str(payload.get("publisher") or "").strip(),
        enabled=enabled,
        trusted=trusted,
        granted_permissions=grants,
        entrypoint=entrypoint,
        permissions=permissions,
        activation_events=activation_events,
        dependencies=dependencies,
        contributes=contributes,
        source=source,
        integrity=integrity,
        declared_integrity=declared_integrity,
        legacy=legacy,
        validation_errors=tuple(errors),
        health=health,
        health_detail=detail,
    )


def discover_extensions(
    cwd: str | Path,
    *,
    search_roots: Iterable[tuple[str, Path]] | None = None,
) -> list[ExtensionManifest]:
    """Discover global and project packages, including invalid manifests."""

    manifests: list[ExtensionManifest] = []
    for scope, root in list(search_roots or extension_search_roots(cwd)):
        if not root.exists() or not root.is_dir():
            continue
        try:
            candidates = sorted(
                (entry / "extension.json" for entry in root.iterdir() if entry.is_dir()),
                key=lambda item: item.parent.name.casefold(),
            )
        except OSError:
            continue
        for manifest_path in candidates:
            if not manifest_path.exists():
                continue
            manifests.append(read_extension_manifest(manifest_path, scope=scope, cwd=cwd))
    return manifests


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(str(version or ""))
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups()[:3])  # type: ignore[return-value]


def version_satisfies(version: str, constraint: str) -> bool:
    """Evaluate the small semver subset used by local extension dependencies."""

    requested = str(constraint or "*").strip()
    if requested in {"", "*", "latest"}:
        return True
    actual = _version_tuple(version)
    if requested.startswith("^"):
        base = _version_tuple(requested[1:])
        upper = (base[0] + 1, 0, 0) if base[0] else (0, base[1] + 1, 0)
        return actual >= base and actual < upper
    if requested.startswith("~"):
        base = _version_tuple(requested[1:])
        return actual >= base and actual < (base[0], base[1] + 1, 0)
    for operator in (">=", "<=", ">", "<", "="):
        if requested.startswith(operator):
            base = _version_tuple(requested[len(operator):].strip())
            return {
                ">=": actual >= base,
                "<=": actual <= base,
                ">": actual > base,
                "<": actual < base,
                "=": actual == base,
            }[operator]
    return actual == _version_tuple(requested)


def resolve_extension_set(
    cwd: str | Path,
    *,
    search_roots: Iterable[tuple[str, Path]] | None = None,
) -> list[ExtensionManifest]:
    """Apply project precedence, trust, permissions, and dependency health."""

    discovered = discover_extensions(cwd, search_roots=search_roots)
    selected: dict[str, ExtensionManifest] = {}
    for manifest in discovered:
        current = selected.get(manifest.id)
        if current is None or manifest.scope == "project":
            selected[manifest.id] = manifest

    resolved: list[ExtensionManifest] = []
    for manifest in discovered:
        winner = selected.get(manifest.id)
        if winner is not manifest:
            resolved.append(
                replace(
                    manifest,
                    health="shadowed",
                    health_detail=f"Shadowed by {winner.key if winner else 'another extension'}.",
                    shadowed_by=winner.key if winner else "",
                )
            )
            continue
        resolved.append(manifest)

    by_id = {item.id: item for item in resolved if not item.shadowed_by}

    # Installed packages are pinned by content hash.  Repo-shared bundles that
    # were only copied into place remain usable without a lock entry, while a
    # managed package cannot be silently modified after installation.
    for extension_id, manifest in list(by_id.items()):
        lock = _read_lock(manifest.scope, cwd)
        lock_entry = lock.get("extensions", {}).get(extension_id, {})
        if isinstance(lock_entry, dict):
            expected = str(lock_entry.get("integrity") or "")
            if expected and expected != manifest.integrity:
                by_id[extension_id] = replace(
                    manifest,
                    health="integrity_mismatch",
                    health_detail=(
                        f"Installed package integrity changed: expected {expected}, "
                        f"observed {manifest.integrity}."
                    ),
                )

    statuses: dict[str, tuple[str, str]] = {}
    visiting: list[str] = []

    def resolve_dependencies(extension_id: str) -> tuple[str, str]:
        if extension_id in statuses:
            return statuses[extension_id]
        manifest = by_id[extension_id]
        if manifest.health != "ready":
            statuses[extension_id] = (manifest.health, manifest.health_detail)
            return statuses[extension_id]
        if extension_id in visiting:
            cycle = visiting[visiting.index(extension_id) :] + [extension_id]
            detail = "Extension dependency cycle: " + " -> ".join(cycle)
            for cycle_id in cycle:
                statuses[cycle_id] = ("dependency_cycle", detail)
            return statuses[extension_id]

        visiting.append(extension_id)
        try:
            for dependency_id, constraint in manifest.dependencies.items():
                dependency = by_id.get(dependency_id)
                if dependency is None:
                    statuses[extension_id] = (
                        "dependency_blocked",
                        f"Missing extension dependency {dependency_id!r}.",
                    )
                    return statuses[extension_id]
                if not version_satisfies(dependency.version, constraint):
                    statuses[extension_id] = (
                        "dependency_blocked",
                        (
                            f"Extension dependency {dependency_id!r} requires {constraint!r}, "
                            f"but {dependency.version!r} is installed."
                        ),
                    )
                    return statuses[extension_id]
                dependency_status, _ = resolve_dependencies(dependency_id)
                if dependency_status != "ready":
                    if extension_id not in statuses:
                        statuses[extension_id] = (
                            "dependency_blocked",
                            f"Extension dependency {dependency_id!r} is not active ({dependency_status}).",
                        )
                    return statuses[extension_id]
            statuses.setdefault(extension_id, ("ready", "Extension is ready for activation."))
            return statuses[extension_id]
        finally:
            if visiting and visiting[-1] == extension_id:
                visiting.pop()

    for extension_id in by_id:
        resolve_dependencies(extension_id)

    next_resolved: list[ExtensionManifest] = []
    for manifest in resolved:
        if manifest.shadowed_by:
            next_resolved.append(manifest)
            continue
        updated = by_id[manifest.id]
        health, detail = statuses[manifest.id]
        next_resolved.append(replace(updated, health=health, health_detail=detail))
    return sorted(next_resolved, key=lambda item: (item.id.casefold(), item.scope))


class ExtensionRegistry:
    """Typed view over all installed packages and their contributions."""

    def __init__(self, cwd: str | Path) -> None:
        self.cwd = Path(cwd)
        self._manifests: list[ExtensionManifest] = []
        self.refresh()

    def refresh(self) -> list[ExtensionManifest]:
        self._manifests = resolve_extension_set(self.cwd)
        return self.list()

    def list(self) -> list[ExtensionManifest]:
        return list(self._manifests)

    def active(self) -> list[ExtensionManifest]:
        return [manifest for manifest in self._manifests if manifest.is_active]

    def contributions(self, kind: str) -> list[ExtensionContribution]:
        if kind not in CONTRIBUTION_KINDS:
            raise ValueError(f"Unknown extension contribution kind: {kind}")
        result: list[ExtensionContribution] = []
        for manifest in self.active():
            raw = manifest.contributes.get(kind, {} if kind in _MAPPING_CONTRIBUTIONS else [])
            if isinstance(raw, dict):
                for name, value in raw.items():
                    result.append(ExtensionContribution(manifest, kind, str(name), value))
            elif isinstance(raw, list):
                for index, value in enumerate(raw):
                    if isinstance(value, dict):
                        name = str(value.get("name") or value.get("id") or index)
                    elif kind == "skills":
                        name = Path(str(value)).name
                    else:
                        name = str(index)
                    result.append(ExtensionContribution(manifest, kind, name, value))
        return result


def _parse_identifier(identifier: str) -> tuple[str, str]:
    requested = str(identifier or "").strip()
    if not requested:
        raise ValueError("Extension name is required.")
    if ":" in requested:
        maybe_scope, remainder = requested.split(":", 1)
        if maybe_scope.strip().lower() in {"global", "project", "user"}:
            return _normalize_scope(maybe_scope), remainder.strip()
    return "", requested


def resolve_extension(cwd: str | Path, identifier: str) -> ExtensionManifest:
    scope, requested = _parse_identifier(identifier)
    matches = [
        manifest for manifest in resolve_extension_set(cwd)
        if (not scope or manifest.scope == scope)
        and requested in {manifest.id, manifest.name}
    ]
    if not matches:
        raise ValueError(f"No extension named '{identifier}' was found.")
    if len(matches) > 1:
        options = ", ".join(sorted(manifest.key for manifest in matches))
        raise ValueError(
            f"Multiple extensions matched '{identifier}'. Use one of: {options}"
        )
    return matches[0]


def extension_manifest_payload(manifest: ExtensionManifest) -> dict[str, Any]:
    return _read_json_object(Path(manifest.path))


def _update_extension_state(
    cwd: str | Path,
    manifest: ExtensionManifest,
    **updates: Any,
) -> ExtensionManifest:
    state = _read_state(manifest.scope, cwd)
    extensions = dict(state.get("extensions", {}))
    current = dict(extensions.get(manifest.id, {}))
    current.update(updates)
    extensions[manifest.id] = current
    _write_state(manifest.scope, cwd, {"extensions": extensions})
    return resolve_extension(cwd, manifest.key)


def set_extension_enabled(cwd: str | Path, identifier: str, enabled: bool) -> ExtensionManifest:
    manifest = resolve_extension(cwd, identifier)
    updated = _update_extension_state(cwd, manifest, enabled=bool(enabled))

    # Preserve the v0 manifest contract for existing local bundles while the
    # separate state file is authoritative for the v1 runtime.
    if manifest.legacy:
        payload = extension_manifest_payload(manifest)
        payload["enabled"] = bool(enabled)
        _atomic_write_json(Path(manifest.path), payload)
        updated = resolve_extension(cwd, manifest.key)
    return updated


def _validated_permission_grants(
    declared_permissions: Iterable[str],
    *,
    trusted: bool,
    granted_permissions: Iterable[str] | None,
) -> tuple[str, ...]:
    declared = tuple(dict.fromkeys(str(item).strip() for item in declared_permissions if str(item).strip()))
    requested = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in (granted_permissions if granted_permissions is not None else declared)
            if str(item).strip()
        )
    )
    if not trusted:
        if requested and granted_permissions is not None:
            raise ExtensionTrustError("Permissions cannot be granted to an untrusted extension.")
        return ()
    undeclared = sorted(set(requested) - set(declared))
    if undeclared:
        raise ExtensionTrustError(
            "Cannot grant undeclared permissions: " + ", ".join(undeclared)
        )
    missing = sorted(set(declared) - set(requested))
    if missing:
        raise ExtensionTrustError("Missing permission grants: " + ", ".join(missing))
    return requested


def set_extension_trusted(
    cwd: str | Path,
    identifier: str,
    trusted: bool,
    *,
    granted_permissions: Iterable[str] | None = None,
) -> ExtensionManifest:
    manifest = resolve_extension(cwd, identifier)
    grants = _validated_permission_grants(
        manifest.permissions,
        trusted=trusted,
        granted_permissions=granted_permissions,
    )
    return _update_extension_state(
        cwd,
        manifest,
        trusted=bool(trusted),
        grantedPermissions=list(grants) if trusted else [],
    )


def resolve_secret_value(value: Any, *, environ: Mapping[str, str] | None = None) -> Any:
    """Resolve explicit environment secret references without persisting values."""

    env = environ if environ is not None else os.environ
    reference = ""
    if isinstance(value, dict) and set(value) == {"secretRef"}:
        reference = str(value.get("secretRef") or "").strip()
    elif isinstance(value, str) and value.startswith("secret://env/"):
        reference = value[len("secret://env/") :].strip()
    if reference:
        if not _SECRET_NAME_RE.fullmatch(reference):
            raise ExtensionValidationError([f"Invalid environment secret reference: {reference!r}."])
        if reference not in env or not str(env.get(reference) or ""):
            raise ExtensionError(f"Required secret environment variable {reference!r} is not configured.")
        return str(env[reference])
    if isinstance(value, dict):
        return {str(key): resolve_secret_value(item, environ=env) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_secret_value(item, environ=env) for item in value]
    return value


def extension_secret_environment(cwd: str | Path) -> dict[str, str]:
    """Return local settings secrets overlaid by the current process environment.

    Only values named by an explicit ``secretRef`` are ever projected into a
    contribution.  Keeping this merge here makes CLI and Web-saved credentials
    behave the same without exposing the full settings environment to child
    processes.
    """

    result: dict[str, str] = {}
    try:
        effective = config_module.load_effective_settings(cwd)
        settings_env = effective.get("env", {}) if isinstance(effective, dict) else {}
        if isinstance(settings_env, dict):
            result.update(
                {
                    str(name): str(value)
                    for name, value in settings_env.items()
                    if str(name).strip() and value not in (None, "")
                }
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        pass
    result.update({str(name): str(value) for name, value in os.environ.items()})
    return result


def _sanitize_segment(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_").lower()
    return normalized or "extension"


def collect_extension_mcp_servers(
    cwd: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[ExtensionRuntimeDiagnostic]]:
    resolved_environ = environ if environ is not None else extension_secret_environment(cwd)
    result: dict[str, dict[str, Any]] = {}
    diagnostics: list[ExtensionRuntimeDiagnostic] = []
    for contribution in ExtensionRegistry(cwd).contributions("mcpServers"):
        try:
            if not isinstance(contribution.value, dict):
                raise ExtensionValidationError(["MCP contribution must be an object."])
            config = resolve_secret_value(contribution.value, environ=resolved_environ)
            if not isinstance(config, dict):
                raise ExtensionValidationError(["Resolved MCP contribution must be an object."])
            config = dict(config)
            config.setdefault("cwd", contribution.extension.root)
            config["_extensionId"] = contribution.extension.id
            config["_extensionScope"] = contribution.extension.scope
            server_name = (
                f"ext__{_sanitize_segment(contribution.extension.id)}__"
                f"{_sanitize_segment(contribution.name)}"
            )
            result[server_name] = config
            diagnostics.append(
                ExtensionRuntimeDiagnostic(
                    contribution.extension.id,
                    "mcpServers",
                    contribution.name,
                    "ready",
                    "MCP server configuration and secret references resolved.",
                )
            )
        except Exception as exc:  # noqa: BLE001 - isolate one extension server
            diagnostics.append(
                ExtensionRuntimeDiagnostic(
                    contribution.extension.id,
                    "mcpServers",
                    contribution.name,
                    "blocked",
                    str(exc)[:1_000],
                )
            )
    return result, diagnostics


def extension_mcp_servers(cwd: str | Path) -> dict[str, dict[str, Any]]:
    servers, _ = collect_extension_mcp_servers(cwd)
    return servers


def extension_provider_presets(cwd: str | Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for contribution in ExtensionRegistry(cwd).contributions("providers"):
        if not isinstance(contribution.value, dict):
            continue
        descriptor = dict(contribution.value)
        descriptor["extensionId"] = contribution.extension.id
        descriptor["extensionScope"] = contribution.extension.scope
        result.setdefault(contribution.name, descriptor)
    return result


def apply_extension_provider(
    cwd: str | Path,
    runtime: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve a declarative provider preset onto the existing custom adapter."""

    result = dict(runtime)
    requested = str(result.get("provider") or "").strip().lower()
    descriptor = extension_provider_presets(cwd).get(requested)
    if descriptor is None:
        return result
    transport = str(descriptor.get("transport") or "openai-compatible").strip().lower()
    if transport not in {"openai-compatible", "openai"}:
        raise ExtensionValidationError(
            [f"Extension provider {requested!r} uses unsupported transport {transport!r}."]
        )
    resolved = resolve_secret_value(
        descriptor,
        environ=environ if environ is not None else extension_secret_environment(cwd),
    )
    base_url = str(resolved.get("baseUrl") or "").strip().rstrip("/")
    if not base_url:
        raise ExtensionValidationError([f"Extension provider {requested!r} has no baseUrl."])
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ExtensionValidationError([f"Extension provider {requested!r} baseUrl must be http(s)."])
    api_key = str(resolved.get("apiKey") or "").strip()
    result.update(
        {
            "provider": "custom",
            "customBaseUrl": base_url,
            "customApiKey": api_key,
            "customExtraHeaders": dict(resolved.get("headers", {})) if isinstance(resolved.get("headers"), dict) else {},
            "_extensionProvider": requested,
            "_extensionProviderSource": f"{descriptor.get('extensionScope')}:{descriptor.get('extensionId')}",
        }
    )
    models = resolved.get("models")
    if isinstance(models, list):
        result["extensionProviderModels"] = [str(item) for item in models if str(item).strip()]
    return result


def inspect_extension_health(
    cwd: str | Path,
    identifier: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> list[ExtensionHealthCheck]:
    manifest = resolve_extension(cwd, identifier)
    resolved_environ = environ if environ is not None else extension_secret_environment(cwd)
    checks = [
        ExtensionHealthCheck("manifest", "pass" if not manifest.validation_errors else "fail", manifest.health_detail),
        ExtensionHealthCheck(
            "activation",
            "pass" if manifest.health == "ready" else "blocked",
            manifest.health_detail,
        ),
        ExtensionHealthCheck("enabled", "pass" if manifest.enabled else "blocked", "Enabled." if manifest.enabled else "Disabled."),
        ExtensionHealthCheck(
            "trust",
            "pass" if manifest.trusted or not manifest.requires_trust else "blocked",
            "Trusted." if manifest.trusted else "Trust is required before contributions can activate.",
        ),
        ExtensionHealthCheck(
            "permissions",
            "pass" if set(manifest.permissions).issubset(set(manifest.granted_permissions)) else "blocked",
            (
                "All declared permissions are granted."
                if set(manifest.permissions).issubset(set(manifest.granted_permissions))
                else "Missing grants: " + ", ".join(
                    sorted(set(manifest.permissions) - set(manifest.granted_permissions))
                )
            ),
        ),
    ]
    secret_names: set[str] = set()

    def collect_secret_names(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) == {"secretRef"}:
                secret_names.add(str(value.get("secretRef") or "").strip())
                return
            for item in value.values():
                collect_secret_names(item)
        elif isinstance(value, list):
            for item in value:
                collect_secret_names(item)
        elif isinstance(value, str) and value.startswith("secret://env/"):
            secret_names.add(value[len("secret://env/") :].strip())

    collect_secret_names(manifest.contributes)
    if secret_names:
        missing_secrets = sorted(
            name for name in secret_names if not name or not str(resolved_environ.get(name) or "")
        )
        checks.append(
            ExtensionHealthCheck(
                "secrets",
                "blocked" if missing_secrets else "pass",
                (
                    "Missing environment secret reference(s): " + ", ".join(missing_secrets)
                    if missing_secrets
                    else f"{len(secret_names)} environment secret reference(s) are configured."
                ),
            )
        )
    for kind in manifest.contribution_kinds:
        raw = manifest.contributes.get(kind)
        count = len(raw) if isinstance(raw, (list, dict)) else 0
        checks.append(ExtensionHealthCheck(kind, "pass" if count else "fail", f"{count} contribution(s) declared."))
    return checks


def _reject_symlinks(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise ExtensionValidationError([f"Symbolic links are not allowed in extension packages: {path}."])


def _copy_source_tree(source: Path, destination: Path) -> None:
    _reject_symlinks(source)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )


def _safe_extract_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            raw_name = info.filename.replace("\\", "/")
            member = Path(raw_name)
            if member.is_absolute() or any(part in {"", ".", ".."} for part in member.parts):
                raise ExtensionValidationError([f"Unsafe ZIP member path: {info.filename!r}."])
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise ExtensionValidationError([f"ZIP symbolic links are not allowed: {info.filename!r}."])
            target = destination.joinpath(*member.parts)
            try:
                target.resolve(strict=False).relative_to(destination.resolve())
            except ValueError as exc:
                raise ExtensionValidationError([f"ZIP member escapes destination: {info.filename!r}."]) from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source_handle, target.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)


def _prepare_bundle_source(
    source: str | Path,
    destination: Path,
    *,
    base_dir: str | Path | None = None,
    revision: str | None = None,
) -> tuple[Path, str]:
    raw_source = str(source)
    local = Path(raw_source).expanduser()
    if not local.is_absolute():
        local = Path(base_dir or Path.cwd()) / local
    source_label = raw_source
    if local.exists():
        local = local.resolve()
        if local.is_dir():
            _copy_source_tree(local, destination)
        elif local.is_file() and local.suffix.lower() == ".zip":
            destination.mkdir(parents=True, exist_ok=True)
            _safe_extract_zip(local, destination)
        else:
            raise ExtensionValidationError(["Extension source must be a directory, ZIP file, or Git URL."])
    else:
        git_url = raw_source.removeprefix("git+")
        parsed = urlparse(git_url)
        is_git = parsed.scheme in {"https", "ssh", "file"} or git_url.startswith("git@")
        if not is_git:
            raise ExtensionValidationError([f"Extension source does not exist: {raw_source}"])
        if parsed.scheme == "https" and (parsed.username or parsed.password or parsed.query):
            raise ExtensionValidationError(
                [
                    (
                        "Git extension URLs cannot contain credentials or query parameters; "
                        "use the system Git credential manager and the revision field."
                    )
                ]
            )
        command = ["git", "-c", "core.hooksPath=", "clone", "--depth", "1"]
        if revision:
            command.extend(["--branch", revision])
        command.extend([git_url, str(destination)])
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "git clone failed").strip()
            raise ExtensionError(detail[:1_000])
        git_dir = destination / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)
        _reject_symlinks(destination)

    if (destination / "extension.json").is_file():
        return destination, source_label
    child_candidates = [
        child for child in destination.iterdir()
        if child.is_dir() and (child / "extension.json").is_file()
    ]
    if len(child_candidates) == 1:
        return child_candidates[0], source_label
    if (destination / "SKILL.md").is_file():
        inferred_id = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(raw_source).stem).strip("-_") or "skill"
        _atomic_write_json(
            destination / "extension.json",
            {
                "apiVersion": EXTENSION_API_VERSION,
                "id": inferred_id,
                "name": inferred_id,
                "version": "0.0.0",
                "description": "Imported MiniCode skill package",
                "contributes": {"skills": [{"name": inferred_id, "path": "."}]},
            },
        )
        return destination, source_label
    raise ExtensionValidationError(["Extension bundle must contain extension.json at its root."])


def _record_lock(cwd: str | Path, manifest: ExtensionManifest, source: str) -> None:
    payload = _read_lock(manifest.scope, cwd)
    extensions = dict(payload.get("extensions", {}))
    extensions[manifest.id] = {
        "version": manifest.version,
        "integrity": manifest.integrity,
        "source": source,
        "installedAt": int(time.time()),
    }
    _write_lock(manifest.scope, cwd, {"extensions": extensions})


def install_extension(
    cwd: str | Path,
    source: str | Path,
    *,
    scope: str = "global",
    trusted: bool = False,
    granted_permissions: Iterable[str] | None = None,
    revision: str | None = None,
    source_label_override: str | None = None,
) -> ExtensionManifest:
    """Install or update a local/ZIP/Git bundle with atomic replacement."""

    normalized_scope = _normalize_scope(scope)
    root = extension_root(normalized_scope, cwd)
    root.mkdir(parents=True, exist_ok=True)
    staging_root = root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="install-", dir=str(staging_root)))
    prepared = staging / "source"
    staged_target: Path | None = None
    backup_target: Path | None = None
    try:
        bundle_root, prepared_source_label = _prepare_bundle_source(
            source,
            prepared,
            base_dir=cwd,
            revision=revision,
        )
        source_label = str(source_label_override or prepared_source_label).strip()
        provisional = read_extension_manifest(
            bundle_root / "extension.json", scope=normalized_scope, cwd=cwd
        )
        if provisional.validation_errors:
            raise ExtensionValidationError(provisional.validation_errors)
        permissions = _validated_permission_grants(
            provisional.permissions,
            trusted=trusted,
            granted_permissions=granted_permissions,
        )
        target = root / provisional.id
        staged_target = staging / "ready"
        if bundle_root == staging or staged_target == bundle_root:
            raise ExtensionError("Invalid staging layout.")
        os.replace(bundle_root, staged_target)

        if target.exists():
            backup_root = root / ".backups" / provisional.id
            backup_root.mkdir(parents=True, exist_ok=True)
            backup_target = backup_root / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
            os.replace(target, backup_target)
        try:
            os.replace(staged_target, target)
        except Exception:
            if backup_target is not None and backup_target.exists() and not target.exists():
                os.replace(backup_target, target)
            raise

        installed = read_extension_manifest(target / "extension.json", scope=normalized_scope, cwd=cwd)
        state = _read_state(normalized_scope, cwd)
        extensions = dict(state.get("extensions", {}))
        extensions[installed.id] = {
            "enabled": True,
            "trusted": bool(trusted),
            "grantedPermissions": list(dict.fromkeys(permissions)),
            "source": source_label,
        }
        _write_state(normalized_scope, cwd, {"extensions": extensions})
        _record_lock(cwd, installed, source_label)
        return resolve_extension(cwd, f"{normalized_scope}:{installed.id}")
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def install_mcp_extension(
    cwd: str | Path,
    *,
    name: str,
    transport: str,
    scope: str = "project",
    command: str = "",
    args: Iterable[str] = (),
    url: str = "",
) -> ExtensionManifest:
    """Create and install a declarative MCP-only extension from form values."""

    display_name = str(name or "").strip()
    if not display_name:
        raise ExtensionValidationError(["MCP connection name cannot be empty."])
    if len(display_name) > 128:
        raise ExtensionValidationError(["MCP connection name cannot exceed 128 characters."])

    normalized_transport = str(transport or "").strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", display_name.casefold()).strip("-._")
    if not slug:
        digest = hashlib.sha256(display_name.encode("utf-8")).hexdigest()[:10]
        slug = f"server-{digest}"
    extension_id = f"mcp.{slug[:120]}"
    server_name = slug[:128]

    descriptor: dict[str, Any]
    permissions: list[str]
    source_label: str
    if normalized_transport == "streamable-http":
        endpoint = str(url or "").strip()
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ExtensionValidationError(["Remote MCP URL must be a valid http(s) address."])
        if parsed.username or parsed.password:
            raise ExtensionValidationError(
                ["Remote MCP URL cannot contain credentials; use a secret reference instead."]
            )
        descriptor = {"url": endpoint, "transport": "streamable-http"}
        if parsed.scheme == "http":
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ExtensionValidationError(
                    ["Remote MCP uses HTTPS; plain HTTP is allowed only for a loopback address."]
                )
            descriptor["allowInsecureHttp"] = True
        permissions = ["network:access"]
        source_label = "MiniCode MCP form · remote"
    elif normalized_transport == "stdio":
        executable = str(command or "").strip()
        if not executable:
            raise ExtensionValidationError(["Local MCP command cannot be empty."])
        if "\x00" in executable or any(ord(character) < 32 for character in executable):
            raise ExtensionValidationError(["Local MCP command contains unsupported characters."])
        clean_args: list[str] = []
        for raw_arg in args:
            argument = str(raw_arg)
            if "\x00" in argument or "\r" in argument or "\n" in argument:
                raise ExtensionValidationError(
                    ["Each local MCP argument must be supplied as one plain value."]
                )
            if len(argument) > 4_096:
                raise ExtensionValidationError(
                    ["A local MCP argument cannot exceed 4096 characters."]
                )
            clean_args.append(argument)
            if len(clean_args) > 128:
                raise ExtensionValidationError(["Local MCP accepts at most 128 arguments."])
        descriptor = {"command": executable, "args": clean_args, "transport": "stdio"}
        permissions = ["process:spawn"]
        source_label = "MiniCode MCP form · local"
    else:
        raise ExtensionValidationError(
            ["MCP transport must be 'streamable-http' or 'stdio'."]
        )

    with tempfile.TemporaryDirectory(prefix="minicode-mcp-") as temporary_directory:
        bundle = Path(temporary_directory) / extension_id
        bundle.mkdir()
        _atomic_write_json(
            bundle / "extension.json",
            {
                "apiVersion": EXTENSION_API_VERSION,
                "id": extension_id,
                "name": display_name,
                "version": "0.0.0",
                "description": "MCP connection created in MiniCode.",
                "permissions": permissions,
                "contributes": {"mcpServers": {server_name: descriptor}},
            },
        )
        return install_extension(
            cwd,
            bundle,
            scope=scope,
            trusted=False,
            source_label_override=source_label,
        )


def rollback_extension(cwd: str | Path, identifier: str) -> ExtensionManifest:
    manifest = resolve_extension(cwd, identifier)
    root = extension_root(manifest.scope, cwd)
    backup_root = root / ".backups" / manifest.id
    candidates = sorted(
        [item for item in backup_root.iterdir() if item.is_dir()] if backup_root.exists() else [],
        key=lambda item: item.name,
        reverse=True,
    )
    if not candidates:
        raise ExtensionError(f"No rollback version is available for {manifest.key}.")
    target = Path(manifest.root)
    selected = candidates[0]
    rollback_slot = backup_root / f"rollback-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    os.replace(target, rollback_slot)
    try:
        os.replace(selected, target)
    except Exception:
        os.replace(rollback_slot, target)
        raise
    rolled_back = read_extension_manifest(
        target / "extension.json", scope=manifest.scope, cwd=cwd
    )
    _record_lock(cwd, rolled_back, rolled_back.source or "rollback")
    return resolve_extension(cwd, manifest.key)


def uninstall_extension(cwd: str | Path, identifier: str) -> dict[str, Any]:
    """Move an installed package to a recoverable trash area."""

    manifest = resolve_extension(cwd, identifier)
    root = extension_root(manifest.scope, cwd)
    target = Path(manifest.root)
    trash_root = root / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    trash_target = trash_root / f"{manifest.id}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    os.replace(target, trash_target)

    state = _read_state(manifest.scope, cwd)
    state_extensions = dict(state.get("extensions", {}))
    state_extensions.pop(manifest.id, None)
    _write_state(manifest.scope, cwd, {"extensions": state_extensions})

    lock = _read_lock(manifest.scope, cwd)
    lock_extensions = dict(lock.get("extensions", {}))
    lock_extensions.pop(manifest.id, None)
    _write_lock(manifest.scope, cwd, {"extensions": lock_extensions})
    return {
        "removed": True,
        "extension": manifest.key,
        "trashPath": str(trash_target),
        "recoverable": True,
    }
