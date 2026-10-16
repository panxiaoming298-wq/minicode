"""Declarative Agent Profile contributions from trusted extensions."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode.extensions import (
    ExtensionRegistry,
    ExtensionValidationError,
    _safe_bundle_path,
)
from minicode.tooling import ToolDefinition


@dataclass(frozen=True, slots=True)
class ExtensionAgentProfile:
    name: str
    description: str
    instructions: str
    extension_id: str
    extension_scope: str
    model: str = ""
    provider: str = ""
    execution_profile: str = ""
    tool_allow: tuple[str, ...] = ()
    tool_deny: tuple[str, ...] = ()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def list_extension_agent_profiles(cwd: str | Path) -> list[ExtensionAgentProfile]:
    profiles: list[ExtensionAgentProfile] = []
    claimed: set[str] = set()
    for contribution in ExtensionRegistry(cwd).contributions("agents"):
        descriptor = contribution.value
        if not isinstance(descriptor, dict):
            continue
        try:
            name = str(descriptor.get("name") or contribution.name).strip().lower()
            if not name or name in claimed:
                continue
            instructions = str(descriptor.get("instructions") or "").strip()
            instructions_file = str(descriptor.get("instructionsFile") or "").strip()
            if instructions_file:
                path = _safe_bundle_path(
                    Path(contribution.extension.root),
                    instructions_file,
                    label=f"agent {name!r} instructionsFile",
                )
                if not path.is_file():
                    raise ExtensionValidationError(
                        [f"Agent Profile instructions file does not exist: {instructions_file!r}."]
                    )
                instructions = path.read_text(encoding="utf-8").strip()
            if not instructions:
                raise ExtensionValidationError(
                    [f"Agent Profile {name!r} has no instructions."]
                )
            execution_profile = str(descriptor.get("executionProfile") or "").strip().lower()
            if execution_profile and execution_profile not in {"standard", "deep", "ultra"}:
                raise ExtensionValidationError(
                    ["Agent Profile executionProfile must be 'standard', 'deep', or 'ultra'."]
                )
            profiles.append(
                ExtensionAgentProfile(
                    name=name,
                    description=str(descriptor.get("description") or "Agent Profile.").strip(),
                    instructions=instructions,
                    extension_id=contribution.extension.id,
                    extension_scope=contribution.extension.scope,
                    model=str(descriptor.get("model") or "").strip(),
                    provider=str(descriptor.get("provider") or "").strip().lower(),
                    execution_profile=execution_profile,
                    tool_allow=_string_tuple(descriptor.get("toolAllow")),
                    tool_deny=_string_tuple(descriptor.get("toolDeny")),
                )
            )
            claimed.add(name)
        except (OSError, UnicodeDecodeError, ExtensionValidationError):
            continue
    return sorted(profiles, key=lambda item: item.name)


def apply_extension_agent_profile(
    cwd: str | Path,
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(runtime)
    selected = str(
        os.environ.get("MINI_CODE_AGENT_PROFILE")
        or result.get("agentProfile")
        or ""
    ).strip().lower()
    if not selected:
        return result
    profile = next(
        (item for item in list_extension_agent_profiles(cwd) if item.name == selected),
        None,
    )
    if profile is None:
        available = ", ".join(item.name for item in list_extension_agent_profiles(cwd)) or "none"
        raise ExtensionValidationError(
            [f"Agent Profile {selected!r} is not available. Available profiles: {available}."]
        )
    result.update(
        {
            "agentProfile": profile.name,
            "agentInstructions": profile.instructions,
            "agentToolAllow": list(profile.tool_allow),
            "agentToolDeny": list(profile.tool_deny),
            "_extensionAgentProfile": profile.name,
            "_extensionAgentProfileSource": f"{profile.extension_scope}:{profile.extension_id}",
        }
    )
    if profile.model:
        result["model"] = profile.model
        result["configuredModel"] = profile.model
    if profile.provider:
        result["provider"] = profile.provider
    if profile.execution_profile:
        result["executionProfile"] = profile.execution_profile
    return result


def filter_tools_for_agent_profile(
    tools: Iterable[ToolDefinition],
    runtime: Mapping[str, Any] | None,
) -> list[ToolDefinition]:
    runtime = runtime or {}
    allow = _string_tuple(runtime.get("agentToolAllow"))
    deny = _string_tuple(runtime.get("agentToolDeny"))

    def matches(name: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)

    return [
        tool
        for tool in tools
        if (not allow or matches(tool.name, allow)) and not matches(tool.name, deny)
    ]
