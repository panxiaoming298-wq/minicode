"""Read-only Skill management inventory; discovery remains invocation authority."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode.extensions import ExtensionManifest, _safe_bundle_path, extension_search_roots, resolve_extension_set
from minicode.path_safety import is_unsafe_stored_path
from minicode.skills import (
    LoadedSkill, SkillSummary, _describe, _list_skill_dirs, _read_summary,
    _skill_roots, discover_skills, find_skill,
)


@dataclass
class _Entry:
    skill: SkillSummary
    boundary: Path
    extension: ExtensionManifest | None = None
    readable: bool = True


def _identity(skill: SkillSummary) -> tuple[str, str, str]:
    return skill.name, skill.source, str(Path(skill.path).absolute())


def _key(cwd: Path, skill: SkillSummary) -> str:
    value = "\0".join((str(cwd.absolute()), *_identity(skill)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extension_entries(cwd: Path) -> list[_Entry]:
    entries = []
    for manifest in resolve_extension_set(cwd):
        declared = manifest.contributes.get("skills", [])
        if not isinstance(declared, list):
            continue
        for index, item in enumerate(declared):
            if isinstance(item, str):
                raw_path, name = item, Path(item).name
            elif isinstance(item, dict):
                raw_path = item.get("path", "")
                name = str(item.get("name") or item.get("id") or index).strip()
            else:
                continue
            source = f"extension:{manifest.scope}:{manifest.id}"
            boundary = Path(manifest.root)
            skill = LoadedSkill(name=name, description="", path="", source=source,
                                extension_id=manifest.id, version=manifest.version)
            readable = False
            try:
                package = _safe_bundle_path(boundary, raw_path, label="skill")
                skill.name = name or package.name
                skill.path = str(package / "SKILL.md")
                skill.package_root = str(package)
                if is_unsafe_stored_path(Path(skill.path), boundary=boundary, reject_hardlinks=True):
                    raise ValueError("Skill 文件路径不安全，无法读取。")
                skill.content = _read_summary(Path(skill.path))
                readable = True
                skill = _describe(skill)
                skill.content = ""
                skill.body = ""
            except (OSError, RuntimeError, ValueError, UnicodeDecodeError) as error:
                skill.error = str(error)[:1000]
            entries.append(_Entry(skill, boundary, manifest, readable))
    return entries


def _entries(cwd: Path) -> list[_Entry]:
    result = [_Entry(skill, root) for root, source in _skill_roots(cwd)
              for skill in _list_skill_dirs(root, source)]
    result.extend(_extension_entries(cwd))
    return list({_key(cwd, entry.skill): entry for entry in result}.values())


def _command_name(name: str) -> bool:
    # Match the names the existing public capability snapshot can safely expose.
    return bool(0 < len(name) <= 100 and not name.startswith(".")
                and re.fullmatch(r"[^\s/\\:\x00]+", name))


def _public(cwd: Path, entry: _Entry, winners: dict[str, SkillSummary]) -> dict[str, Any]:
    skill, extension = entry.skill, entry.extension
    winner = winners.get(skill.name)
    effective = winner is not None and _identity(winner) == _identity(skill)
    status, reason = "ready", "可由当前工作区解析并使用。"
    if extension and not extension.is_active:
        status, reason = extension.health, extension.health_detail
    elif skill.error:
        status, reason = "invalid", skill.error
    elif not effective:
        status, reason = ("shadowed", "同名 Skill 已由其他来源提供。") if winner else ("unavailable", "当前发现结果未包含此 Skill，请刷新目录。")
    elif not _command_name(skill.name):
        status, reason = "invalid_name", "名称无法用于当前 Skill 命令，请检查名称长度与字符。"
    return {
        "key": _key(cwd, skill), "name": skill.name,
        "description": skill.description[:1000], "path": skill.path,
        "source": skill.source,
        "scope": extension.scope if extension else ("project" if skill.source in {"project", "compat_project"} else "global"),
        "extensionId": skill.extension_id, "extensionKey": extension.key if extension else None,
        "version": skill.version, "status": status, "reason": reason[:1400],
        "error": skill.error[:1000], "effective": effective,
        "shadowedBy": _key(cwd, winner) if winner and not effective else None,
        "userInvocable": skill.user_invocable,
        "modelInvocable": not skill.disable_model_invocation,
        "canInvoke": status == "ready" and effective and skill.user_invocable,
        "canPreview": entry.readable,
    }


def skill_inventory(cwd: str | Path, *, invocation_cwd: str | Path | None = None,
                    include_project_roots: bool = True) -> dict[str, Any]:
    root = Path(cwd)
    winners = {skill.name: skill for skill in discover_skills(root)}
    invocation_winners = winners if invocation_cwd is None or Path(invocation_cwd) == root else {
        skill.name: skill for skill in discover_skills(invocation_cwd)}
    rows = [_public(root, entry, winners) for entry in _entries(root)]
    for row in rows:
        current = invocation_winners.get(row["name"])
        matches = current is not None and _identity(current) == (row["name"], row["source"], str(Path(row["path"]).absolute()))
        row["canInvokeInConversation"] = bool(matches and not current.error and current.user_invocable and _command_name(current.name))
        row["invocationReason"] = ("填入后可继续编辑，发送任务时才会调用。" if row["canInvokeInConversation"] else
            "当前对话中的同名 Skill 来自其他位置。" if current and not matches else
            "此 Skill 当前不允许手动调用，请检查可用状态与调用设置。" if matches else
            "此 Skill 不在当前对话的可用范围内，请进入对应项目的对话后使用。")
    rows.sort(key=lambda row: (row["name"].casefold(), not row["effective"], row["source"], row["path"]))
    directories = [(path, source, "project" if source in {"project", "compat_project"} else "global")
                   for path, source in _skill_roots(root)]
    directories.extend((path, "extensions", scope) for scope, path in extension_search_roots(root))
    return {"skills": rows, "discoveryRoots": [
        {"path": str(path), "source": source, "scope": scope, "exists": path.is_dir()}
        for path, source, scope in directories if include_project_roots or scope == "global"]}


def _selected(cwd: Path, key: str) -> _Entry:
    for entry in _entries(cwd):
        if _key(cwd, entry.skill) == key:
            return entry
    raise LookupError("Skill 已移除或来源已改变，请刷新列表。")


def preview_skill(cwd: str | Path, key: str) -> dict[str, Any]:
    root = Path(cwd)
    entry = _selected(root, key)
    path = Path(entry.skill.path)
    if not entry.readable or is_unsafe_stored_path(path, boundary=entry.boundary, reject_hardlinks=True):
        raise ValueError("Skill 文件当前不可安全预览，请刷新列表。")
    limit = 262144
    before = path.stat()
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    after = path.stat()
    if ((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
            or is_unsafe_stored_path(path, boundary=entry.boundary, reject_hardlinks=True)):
        raise ValueError("Skill 在读取期间发生变化，请重新预览。")
    return {"key": key, "path": str(path), "content": raw[:limit].decode("utf-8-sig", errors="replace"),
            "truncated": len(raw) > limit, "bytes": after.st_size}


def skill_invocation_command(cwd: str | Path, key: str, *, invocation_cwd: str | Path | None = None) -> dict[str, str]:
    root = Path(cwd)
    selected = _selected(root, key).skill
    current = find_skill(invocation_cwd if invocation_cwd is not None else root, selected.name)
    if current is None or _identity(current) != _identity(selected):
        raise ValueError("此 Skill 在当前对话中未生效或已被同名版本覆盖，请检查对话范围并刷新列表。")
    if current.error or not current.user_invocable or not _command_name(current.name):
        raise ValueError("此 Skill 当前不允许手动调用，请检查状态和调用方式。")
    return {"key": key, "command": f"/skill {current.name} "}
