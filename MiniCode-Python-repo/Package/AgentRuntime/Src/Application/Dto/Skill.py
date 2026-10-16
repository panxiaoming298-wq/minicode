"""Portable Skill metadata and bounded invocation contracts."""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    description: str
    name: str = ""
    disable_model_invocation: bool = False
    user_invocable: bool = True
    fields: dict[str, Any] = field(default_factory=dict)
    required_resources: tuple[str, ...] = ()
    branches: dict[str, tuple[str, ...]] = field(default_factory=dict)
    required_tools: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillDocument:
    name: str
    description: str
    source: str
    path: str
    package_root: str
    source_hash: str
    body: str
    metadata: SkillMetadata
    version: str = ""
    source_text: str = ""


@dataclass(frozen=True, slots=True)
class SkillResource:
    path: str
    content: str
    source_hash: str


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    text: str
    total: int
    shown: tuple[str, ...]
    omitted: tuple[str, ...]
    estimated_tokens: int
