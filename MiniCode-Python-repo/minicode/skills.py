from __future__ import annotations

import os
import hashlib
import json
import re
import shutil
import tempfile
import time
import uuid
import stat
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SkillSummary:
    name: str
    description: str
    path: str
    source: str
    package_root: str = ""
    extension_id: str = ""
    version: str = ""
    disable_model_invocation: bool = False
    user_invocable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    error: str = ""
    branches: tuple[str, ...] = ()


@dataclass(slots=True)
class LoadedSkill(SkillSummary):
    content: str = ""
    body: str = ""
    source_hash: str = ""
    source_text: str = ""


def extract_description(markdown: str) -> str:
    return parse_skill_document(markdown)[0].description


def build_skill_catalog(skills, runtime=None):
    from Package.AgentRuntime.Src.Boot.Skills import createApp
    from minicode.context_manager import estimate_tokens, resolve_context_limit
    settings = runtime or {}
    limit = resolve_context_limit(str(settings.get("model") or ""), settings)
    maximum = int(settings.get("skillCatalogMaxTokens", 2000))
    ratio = float(settings.get("skillCatalogWindowRatio", 0.01))
    if maximum < 0 or not 0 <= ratio <= 1:
        raise ValueError("Invalid Skill catalog budget")
    budget = min(maximum, int(limit * ratio))
    catalog = createApp().catalog(skills, token_budget=budget, measure=estimate_tokens)
    if isinstance(runtime, dict):
        runtime["_skillCatalogDiagnostics"] = {"total": catalog.total, "shown": len(catalog.shown),
            "omitted": len(catalog.omitted), "budget": budget, "estimated_tokens": catalog.estimated_tokens,
            "characters": len(catalog.text), "utf8_bytes": len(catalog.text.encode("utf-8")),
            "route": "local_discovery" if catalog.omitted else "direct_catalog"}
    return catalog


def _legacy_description(markdown: str) -> str:
    normalized = markdown.replace("\r\n", "\n")
    paragraphs = [block.strip() for block in normalized.split("\n\n") if block.strip()]
    for block in paragraphs:
        if block.startswith("#"):
            continue
        for line in [part.strip() for part in block.split("\n")]:
            if line and not line.startswith("#"):
                return line.replace("`", "")
    return "No description provided."


def parse_skill_document(markdown: str):
    """Safe YAML front matter; execution extensions are validated separately."""
    from Package.AgentRuntime.Src.Boot.Skills import createApp
    import yaml

    normalized = markdown.lstrip("\ufeff").replace("\r\n", "\n")
    body, fields = normalized, {}
    if normalized.startswith("---\n"):
        end = re.search(r"^---\s*$", normalized[4:], re.MULTILINE)
        if end is None or end.start() > 32768:
            raise ValueError("Skill front matter must close within 32768 characters")
        header = normalized[4:4 + end.start()]
        if len(header.encode("utf-8")) > 65536:
            raise ValueError("Skill front matter exceeds 65536 bytes")
        class UniqueLoader(yaml.SafeLoader):
            def compose_node(self, parent, index):
                if self.check_event(yaml.AliasEvent):
                    raise ValueError("Skill YAML aliases are unsupported")
                return super().compose_node(parent, index)

            def construct_mapping(self, node, deep=False):
                value = {}
                for key_node, value_node in node.value:
                    key = self.construct_object(key_node, deep=deep)
                    if not isinstance(key, str) or key in value:
                        raise ValueError("Skill YAML keys must be unique strings")
                    value[key] = self.construct_object(value_node, deep=deep)
                return value
        try:
            fields = yaml.load(header, Loader=UniqueLoader)
            json.dumps(fields, allow_nan=False)
        except (yaml.YAMLError, TypeError, RecursionError) as error:
            raise ValueError("Invalid Skill YAML: " + str(error)) from error
        body = normalized[4 + end.end():].lstrip("\n")
        if not isinstance(fields, dict) or not {"name", "description"} <= fields.keys():
            raise ValueError("Skill YAML requires name and description; plain Markdown remains supported")
    return createApp().metadata(fields, fallback_description=_legacy_description(body)), body


@lru_cache(maxsize=4096)
def _summary_content(path: str, signature: tuple):
    # Discovery needs metadata, not the entire body. Loading always rereads the
    # actual bytes; stat caching is never used as invocation version evidence.
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return stream.read(65536)


@lru_cache(maxsize=4096)
def _summary_fields(path: str, signature: tuple):
    described = _describe(LoadedSkill(name="", description="", path=path, source="",
                                    content=_summary_content(path, signature)))
    return {key: getattr(described, key) for key in
            ("description", "disable_model_invocation", "user_invocable", "metadata", "diagnostics", "error", "branches")}


def _describe(skill: LoadedSkill) -> LoadedSkill:
    try:
        metadata, body = parse_skill_document(skill.content)
        skill.description = metadata.description
        skill.disable_model_invocation = metadata.disable_model_invocation
        skill.user_invocable = metadata.user_invocable
        skill.metadata = metadata.fields
        skill.branches = tuple(sorted(metadata.branches))
        skill.diagnostics = metadata.diagnostics
        skill.body = body
        skill.error = ""
    except ValueError as error:
        skill.error = str(error)
        skill.description = "Unavailable: " + str(error)
        skill.disable_model_invocation = True
        skill.user_invocable = False
    return skill


def _read_summary(path: Path) -> str:
    stat = path.stat()
    return _summary_content(str(path), (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))


def _discovered_fields(path: Path, metadata):
    # Advisory metadata only. Invocation always reopens and hashes the real
    # source. Copy cached fields so one caller cannot mutate another's catalog.
    signature = (metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_size, metadata.st_ino)
    return deepcopy(_summary_fields(str(path), signature))


def _home_dir() -> Path:
    return Path.home()


def _skill_roots(cwd: str | Path) -> list[tuple[Path, str]]:
    from minicode.config import ISOLATED_STATE_DIRECTORY, MINI_CODE_DIR

    base = Path(cwd)
    home = _home_dir()
    if ISOLATED_STATE_DIRECTORY:
        return [
            (base / ".mini-code" / "skills", "project"),
            (MINI_CODE_DIR / "skills", "user"),
            (base / ".claude" / "skills", "compat_project"),
        ]
    return [
        (base / ".mini-code" / "skills", "project"),
        (home / ".mini-code" / "skills", "user"),
        (base / ".claude" / "skills", "compat_project"),
        (home / ".claude" / "skills", "compat_user"),
    ]


def _list_skill_dirs(root: Path, source: str, *, name: str | None = None) -> list[LoadedSkill]:
    if not root.exists():
        return []
    if name is None:
        return _scan_skill_summaries(root, source)
    results: list[LoadedSkill] = []
    for entry in (root / name,) if name is not None else root.iterdir():
        try:
            from minicode.path_safety import is_link_or_reparse, is_unsafe_stored_path
            if entry.name.startswith(".") or not entry.is_dir() or is_link_or_reparse(entry):
                continue
        except OSError:
            # Windows: untrusted mount points, broken symlinks, etc.
            continue
        skill_path = entry / "SKILL.md"
        if not skill_path.exists() or is_unsafe_stored_path(skill_path, boundary=root, reject_hardlinks=True):
            continue
        try:
            content = _read_summary(skill_path)
        except (OSError, UnicodeDecodeError):
            continue
        results.append(
            _describe(LoadedSkill(
                name=entry.name,
                description="",
                path=str(skill_path),
                source=source,
                package_root=str(entry),
                content=content,
            ))
        )
    return results


def _scan_skill_summaries(root: Path, source: str) -> list[LoadedSkill]:
    """One fresh scan, one root resolution, one parse per file version.

    This is directory metadata discovery, not invocation authorization. Exact
    loading retains the stricter fresh path walk and full-content validation.
    """
    from minicode.path_safety import _resolved_comparison_path
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    results = []
    try:
        resolved_root = root.resolve(strict=True)
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    # DirEntry reuses Windows enumeration metadata. Reject
                    # aliases before inspecting any Skill text below it.
                    parent_stat = entry.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(parent_stat.st_mode) or getattr(parent_stat, "st_file_attributes", 0) & reparse:
                        continue
                    skill_path = Path(entry.path) / "SKILL.md"
                    leaf = skill_path.lstat()
                    if (not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink > 1
                            or getattr(leaf, "st_file_attributes", 0) & reparse):
                        continue
                    _resolved_comparison_path(skill_path.resolve(strict=True)).relative_to(
                        _resolved_comparison_path(resolved_root))
                    fields = _discovered_fields(skill_path, leaf)
                    results.append(LoadedSkill(name=entry.name, path=str(skill_path), source=source,
                                               package_root=entry.path, **fields))
                except (OSError, ValueError, RuntimeError, UnicodeDecodeError):
                    continue
        if root.resolve(strict=True) != resolved_root:
            return []  # A redirected configured root changed during discovery.
    except (OSError, RuntimeError):
        return []
    return results


def _extension_skills(cwd: str | Path) -> list[LoadedSkill]:
    """Load declarative skill contributions from trusted extension packages."""

    from minicode.extensions import (
        ExtensionRegistry,
        ExtensionValidationError,
        _safe_bundle_path,
    )

    results: list[LoadedSkill] = []
    for contribution in ExtensionRegistry(cwd).contributions("skills"):
        item = contribution.value
        if isinstance(item, str):
            raw_path = item
            name = contribution.name
        elif isinstance(item, dict):
            raw_path = str(item.get("path") or "")
            name = str(item.get("name") or contribution.name).strip()
        else:
            continue
        try:
            package_root = _safe_bundle_path(
                Path(contribution.extension.root), raw_path, label="skill"
            )
        except ExtensionValidationError:
            continue
        skill_path = package_root / "SKILL.md"
        try:
            from minicode.path_safety import is_unsafe_stored_path
            if is_unsafe_stored_path(skill_path, boundary=Path(contribution.extension.root), reject_hardlinks=True):
                continue
            content = _read_summary(skill_path)
        except (OSError, UnicodeDecodeError):
            continue
        results.append(
            _describe(LoadedSkill(
                name=name or package_root.name,
                description="",
                path=str(skill_path),
                source=f"extension:{contribution.extension.scope}:{contribution.extension.id}",
                package_root=str(package_root),
                extension_id=contribution.extension.id,
                version=contribution.extension.version,
                content=content,
            ))
        )
    return results


def discover_skills(cwd: str | Path) -> list[SkillSummary]:
    by_name: dict[str, LoadedSkill] = {}
    roots = _skill_roots(cwd)
    extension_skills = _extension_skills(cwd)
    ordered_groups: list[list[LoadedSkill]] = [
        _list_skill_dirs(*roots[0]),
        [skill for skill in extension_skills if skill.source.startswith("extension:project:")],
        _list_skill_dirs(*roots[1]),
        [skill for skill in extension_skills if skill.source.startswith("extension:global:")],
        *[_list_skill_dirs(*root) for root in roots[2:]],
    ]
    for skills in ordered_groups:
        for skill in skills:
            by_name.setdefault(skill.name, skill)
    return [
        SkillSummary(
            name=skill.name,
            description=skill.description,
            path=skill.path,
            source=skill.source,
            package_root=skill.package_root,
            extension_id=skill.extension_id,
            version=skill.version,
            disable_model_invocation=skill.disable_model_invocation,
            user_invocable=skill.user_invocable,
            metadata=skill.metadata,
            diagnostics=skill.diagnostics,
            error=skill.error,
            branches=skill.branches,
        )
        for skill in sorted(by_name.values(), key=lambda item: item.name)
    ]


def find_skill(cwd: str | Path, name: str) -> SkillSummary | None:
    """Resolve one identity without scanning every project/user Skill file."""
    name = name.strip()
    if not name or name.startswith(".") or any(c in name for c in "/\\:\0"):
        return None
    roots = _skill_roots(cwd)
    project = _list_skill_dirs(*roots[0], name=name)
    if project:
        return SkillSummary(**{key: getattr(project[0], key) for key in SkillSummary.__dataclass_fields__})
    extensions = _extension_skills(cwd)
    groups = ([s for s in extensions if s.name == name and s.source.startswith("extension:project:")],
              _list_skill_dirs(*roots[1], name=name),
              [s for s in extensions if s.name == name and s.source.startswith("extension:global:")],
              *[_list_skill_dirs(*root, name=name) for root in roots[2:]])
    for group in groups:
        if group:
            return SkillSummary(**{key: getattr(group[0], key) for key in SkillSummary.__dataclass_fields__})
    return None


def load_skill(cwd: str | Path, name: str, *, authorize=None) -> LoadedSkill | None:
    normalized_name = name.strip()
    if not normalized_name:
        return None
    summary = find_skill(cwd, normalized_name)
    if summary is not None:
        if authorize is not None:
            authorize(Path(summary.path).resolve())
        try:
            with Path(summary.path).open("rb") as stream:
                raw = stream.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("SKILL.md exceeds the 1 MiB material limit; split the workflow")
            content = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, UnicodeDecodeError):
            return None
        loaded = _describe(LoadedSkill(**asdict(summary), content=content, source_hash=hashlib.sha256(raw).hexdigest(), source_text=raw.decode("utf-8")))
        if loaded.error:
            raise ValueError(loaded.error)
        return loaded
    return None


def _managed_skill_root(scope: str, cwd: str | Path) -> Path:
    from minicode.config import ISOLATED_STATE_DIRECTORY, MINI_CODE_DIR

    if scope == "project":
        return Path(cwd) / ".mini-code" / "skills"
    return (MINI_CODE_DIR if ISOLATED_STATE_DIRECTORY else _home_dir() / ".mini-code") / "skills"


def _validate_skill_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized):
        raise RuntimeError(
            "Skill name must start with an alphanumeric character and contain only letters, digits, '.', '_' or '-'."
        )
    return normalized


def _reject_skill_symlinks(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise RuntimeError(f"Skill packages cannot contain symbolic links: {path}")


def install_skill(cwd: str | Path, source_path: str, name: str | None = None, scope: str = "user") -> dict[str, Any]:
    source = Path(source_path)
    if not source.is_absolute():
        source = Path(cwd) / source
    if source.is_dir():
        skill_file = source / "SKILL.md"
        inferred_name = source.name
        package_root = source
    else:
        skill_file = source
        inferred_name = skill_file.parent.name
        package_root = skill_file.parent
    if not skill_file.exists():
        raise RuntimeError(f"No SKILL.md found in {source}")

    skill_name = _validate_skill_name(name or inferred_name)
    _reject_skill_symlinks(package_root)

    managed_root = _managed_skill_root(scope, cwd)
    managed_root.mkdir(parents=True, exist_ok=True)
    staging_root = managed_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f"{skill_name}-", dir=str(staging_root)))
    staged_package = staging_dir / skill_name
    target_dir = managed_root / skill_name
    backup_path: Path | None = None
    try:
        shutil.copytree(package_root, staged_package)
        if target_dir.exists():
            backup_root = managed_root / ".backups" / skill_name
            backup_root.mkdir(parents=True, exist_ok=True)
            backup_path = backup_root / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
            os.replace(target_dir, backup_path)
        try:
            os.replace(staged_package, target_dir)
        except Exception:
            if backup_path is not None and backup_path.exists() and not target_dir.exists():
                os.replace(backup_path, target_dir)
            raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    file_count = sum(1 for item in target_dir.rglob("*") if item.is_file())
    return {
        "name": skill_name,
        "targetPath": str(target_dir / "SKILL.md"),
        "packagePath": str(target_dir),
        "fileCount": file_count,
    }


def remove_managed_skill(cwd: str | Path, name: str, scope: str = "user") -> dict[str, object]:
    skill_name = _validate_skill_name(name)
    managed_root = _managed_skill_root(scope, cwd)
    target_path = managed_root / skill_name
    if not target_path.exists():
        return {"removed": False, "targetPath": str(target_path)}
    trash_root = managed_root / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    trash_path = trash_root / f"{skill_name}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    os.replace(target_path, trash_path)
    return {
        "removed": True,
        "targetPath": str(target_path),
        "trashPath": str(trash_path),
        "recoverable": True,
    }


def rollback_managed_skill(cwd: str | Path, name: str, scope: str = "user") -> dict[str, Any]:
    skill_name = _validate_skill_name(name)
    managed_root = _managed_skill_root(scope, cwd)
    target = managed_root / skill_name
    backup_root = managed_root / ".backups" / skill_name
    backups = sorted(
        [item for item in backup_root.iterdir() if item.is_dir()] if backup_root.exists() else [],
        key=lambda item: item.name,
        reverse=True,
    )
    if not backups:
        raise RuntimeError(f"No rollback version is available for skill {skill_name!r}.")
    rollback_slot = backup_root / f"rollback-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    if target.exists():
        os.replace(target, rollback_slot)
    try:
        os.replace(backups[0], target)
    except Exception:
        if rollback_slot.exists() and not target.exists():
            os.replace(rollback_slot, target)
        raise
    return {
        "name": skill_name,
        "targetPath": str(target / "SKILL.md"),
        "packagePath": str(target),
    }


def lint_skill(cwd: str | Path, name: str, *, permissions=None) -> dict:
    from Package.AgentRuntime.Src.Boot.Skills import createApp
    from Package.AgentRuntime.Src.Application.Dto.Skill import SkillDocument
    from minicode.tooling import ToolContext
    from minicode.workspace import resolve_tool_path

    context = ToolContext(cwd=str(cwd), permissions=permissions)
    def authorize(path):
        return resolve_tool_path(context, str(path), "read")
    def lookup(selected):
        loaded = load_skill(cwd, selected, authorize=authorize)
        if loaded is None:
            raise ValueError("Unknown or untrusted Skill: " + selected)
        metadata, body = parse_skill_document(loaded.content)
        return SkillDocument(loaded.name, loaded.description, loaded.source, loaded.path, loaded.package_root,
                             loaded.source_hash, body, metadata, loaded.version, loaded.source_text)
    return createApp().lint(name, lookup=lookup, authorize=authorize)


def _main():
    import argparse
    from Package.AgentRuntime.Src.Boot.Skills import createApp
    from minicode.config import load_runtime_config
    from minicode.paged_memory.config import PagedMemoryConfig
    parser = argparse.ArgumentParser(description="Read-only Skill package lint and invocation audit")
    parser.add_argument("action", choices=("lint", "audit"))
    parser.add_argument("identity", help="Skill name for lint; invocation id for audit")
    parser.add_argument("--workspace", default=".")
    options = parser.parse_args()
    cwd = str(Path(options.workspace).resolve())
    try:
        if options.action == "lint":
            from minicode.permissions import PermissionManager
            result = lint_skill(cwd, options.identity, permissions=PermissionManager(cwd))
        else:
            database = PagedMemoryConfig.from_sources(load_runtime_config(cwd)).sqlite_path
            result = createApp().audit(database, workspace=cwd, invocation_id=options.identity)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
