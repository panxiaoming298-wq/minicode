"""Declarative slash-command contributions from trusted extensions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode.extensions import (
    ExtensionRegistry,
    ExtensionValidationError,
    _safe_bundle_path,
)


@dataclass(frozen=True, slots=True)
class ExtensionCommand:
    name: str
    description: str
    prompt: str
    extension_id: str
    extension_scope: str
    argument_hint: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def usage(self) -> str:
        suffix = f" {self.argument_hint}" if self.argument_hint else ""
        return f"/{self.name}{suffix}"


@dataclass(frozen=True, slots=True)
class ExtensionCommandInvocation:
    command: ExtensionCommand
    arguments: str
    prompt: str


def _normalize_command_name(value: Any) -> str:
    name = str(value or "").strip().lstrip("/").lower()
    if not name or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in name):
        raise ExtensionValidationError(
            ["Extension command names may contain only lowercase letters, digits, '-' and '_'."]
        )
    return name


def list_extension_commands(cwd: str | Path) -> list[ExtensionCommand]:
    commands: list[ExtensionCommand] = []
    claimed: set[str] = set()
    for contribution in ExtensionRegistry(cwd).contributions("commands"):
        descriptor = contribution.value
        if not isinstance(descriptor, dict):
            continue
        try:
            name = _normalize_command_name(descriptor.get("name") or contribution.name)
            prompt = str(descriptor.get("prompt") or "").strip()
            prompt_file = str(descriptor.get("promptFile") or "").strip()
            if prompt_file:
                path = _safe_bundle_path(
                    Path(contribution.extension.root),
                    prompt_file,
                    label=f"command {name!r} promptFile",
                )
                if not path.is_file():
                    raise ExtensionValidationError(
                        [f"Extension command prompt file does not exist: {prompt_file!r}."]
                    )
                prompt = path.read_text(encoding="utf-8").strip()
            if not prompt:
                raise ExtensionValidationError(
                    [f"Extension command {name!r} must declare prompt or promptFile."]
                )
            aliases = tuple(
                _normalize_command_name(item)
                for item in descriptor.get("aliases", [])
                if str(item).strip()
            )
            all_names = (name, *aliases)
            if any(item in claimed for item in all_names):
                continue
            claimed.update(all_names)
            commands.append(
                ExtensionCommand(
                    name=name,
                    description=str(descriptor.get("description") or "Extension command.").strip(),
                    prompt=prompt,
                    extension_id=contribution.extension.id,
                    extension_scope=contribution.extension.scope,
                    argument_hint=str(descriptor.get("argumentHint") or "").strip(),
                    aliases=aliases,
                )
            )
        except (OSError, UnicodeDecodeError, ExtensionValidationError):
            continue
    return sorted(commands, key=lambda item: item.name)


def expand_extension_command(
    cwd: str | Path,
    user_input: str,
) -> ExtensionCommandInvocation | None:
    raw = str(user_input or "").strip()
    if not raw.startswith("/"):
        return None
    head, _, arguments = raw[1:].partition(" ")
    requested = head.strip().lower()
    for command in list_extension_commands(cwd):
        if requested not in {command.name, *command.aliases}:
            continue
        expanded = command.prompt.replace("{args}", arguments.strip())
        return ExtensionCommandInvocation(command, arguments.strip(), expanded)
    return None
