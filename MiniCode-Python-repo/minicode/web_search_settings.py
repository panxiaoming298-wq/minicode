"""Local search-settings API; secret values only leave through provider requests."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, StrictBool

from minicode import config
from minicode.search_configuration import (
    MAX_SEARCH_KEYS,
    SEARCH_FIELDS,
    SEARCH_KEY_LISTS,
    SEARCH_PROVIDERS,
    resolve_search_configuration,
)
from minicode.search_usage import cached_usage, refresh_usage


class SearchUsageRefresh(BaseModel):
    model_config = {"extra": "forbid"}
    revision: str = Field(max_length=80)
    allow_search: StrictBool = Field(default=False, alias="allowSearch")


class SearchKeyUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(default="", max_length=64)
    enabled: StrictBool = True
    api_key: SecretStr | None = Field(default=None, alias="apiKey", max_length=4096)


class SearchSettingsUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    provider: Literal["auto", "public", "brave", "tavily"] | None = None
    brave_api_key: SecretStr | None = Field(default=None, alias="braveApiKey", max_length=4096)
    tavily_api_key: SecretStr | None = Field(default=None, alias="tavilyApiKey", max_length=4096)
    clear_brave_api_key: StrictBool = Field(default=False, alias="clearBraveApiKey")
    clear_tavily_api_key: StrictBool = Field(default=False, alias="clearTavilyApiKey")
    revision: str | None = Field(default=None, max_length=80)
    brave_keys: list[SearchKeyUpdate] | None = Field(default=None, alias="braveKeys", max_length=MAX_SEARCH_KEYS)
    tavily_keys: list[SearchKeyUpdate] | None = Field(default=None, alias="tavilyKeys", max_length=MAX_SEARCH_KEYS)


def _mask(key: str) -> str:
    return ("••••••••" + key[-4:] if len(key) > 4 else "••••••••") if key else ""


def search_settings_view(cwd: str | Path | None = None) -> dict:
    effective = config.load_effective_settings(cwd)
    resolved = resolve_search_configuration(effective)
    valid = resolved["provider"] in SEARCH_PROVIDERS
    services = {}
    for name in ("brave", "tavily"):
        key = resolved[name]
        services[name] = {
            "apiKeyConfigured": bool(key),
            "apiKeyMask": _mask(key),
            "apiKeySource": resolved["sources"][name] if resolved["credentials"][name] else "none",
            "keys": [{"id": row["id"], "name": row["name"], "enabled": row["enabled"],
                      "apiKeyMask": _mask(row["apiKey"]), "usage": cached_usage(name, row["apiKey"])}
                     for row in resolved["credentials"][name]],
        }
    return {
        "provider": resolved["provider"] if valid else "auto",
        "providerSource": resolved["sources"]["provider"],
        "configurationError": " ".join(resolved["errors"] + ([] if valid else ["搜索源配置无效，请选择自动、公开搜索、Brave 或 Tavily。"])),
        "revision": effective.get("webSearchSettingsRevision", ""),
        "maxKeysPerService": MAX_SEARCH_KEYS,
        **services,
    }


def _key_list(rows: list[SearchKeyUpdate], existing: list[dict], label: str) -> list[dict]:
    known = {row["id"]: row for row in existing}
    result, ids, secrets = [], set(), set()
    for index, row in enumerate(rows):
        if row.id is not None and row.id not in known:
            raise HTTPException(409, "密钥列表已改变，请重新读取设置后再保存。")
        identifier = row.id or uuid4().hex
        value = row.api_key.get_secret_value().strip() if row.api_key is not None else ""
        value = value or (known[identifier]["apiKey"] if identifier in known else "")
        if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise HTTPException(400, f"{label} 第 {index + 1} 项需要有效密钥，不能包含空白或非 ASCII 字符。")
        if identifier in ids or value in secrets:
            raise HTTPException(400, f"{label} 列表中有重复密钥，请移除重复项后保存。")
        ids.add(identifier)
        secrets.add(value)
        result.append({"id": identifier, "name": row.name.strip() or f"密钥 {index + 1}",
                       "apiKey": value, "enabled": row.enabled})
    return result


def save_search_settings(update: SearchSettingsUpdate, cwd: str | Path | None = None) -> dict:
    # Compare the revision and apply the edit under the same lock as other settings writes.
    with config._SETTINGS_WRITE_LOCK:
        return _save_search_settings(update, cwd)


def _save_search_settings(update: SearchSettingsUpdate, cwd: str | Path | None) -> dict:
    effective = config.load_effective_settings(cwd)
    current = resolve_search_configuration(effective)
    list_update = update.brave_keys is not None or update.tavily_keys is not None
    if list_update and update.revision is None:
        raise HTTPException(409, "请重新读取搜索设置后再保存密钥列表。")
    if update.revision is not None and update.revision != effective.get("webSearchSettingsRevision", ""):
        raise HTTPException(409, "搜索设置已在其他页面修改，请重新读取后再保存。当前编辑内容尚未保存。")
    env_updates, root_updates = {}, {}
    if update.provider is not None:
        if current["sources"]["provider"] == "environment" and update.provider != current["provider"]:
            raise HTTPException(409, "搜索源由启动环境变量指定，请先在启动环境中修改或移除该变量。")
        env_updates["MINI_CODE_SEARCH_PROVIDER"] = update.provider
        root_updates["webSearchProvider"] = update.provider
    for name, label in (("brave", "Brave"), ("tavily", "Tavily")):
        rows = getattr(update, f"{name}_keys")
        secret = getattr(update, f"{name}_api_key")
        value = secret.get_secret_value().strip() if secret is not None else ""
        clear = getattr(update, f"clear_{name}_api_key")
        if (clear and value) or (rows is not None and (clear or value)):
            raise HTTPException(400, f"不能同时填写和清除 {label} 密钥，请选择其中一项。")
        if rows is None and not clear and not value:
            continue  # Empty or omitted inputs keep the saved key.
        if current["sources"][name] == "environment":
            raise HTTPException(409, f"{label} 密钥由启动环境变量提供，请在启动环境中修改或移除。")
        env_names, root_key, _ = SEARCH_FIELDS[name]
        list_key = SEARCH_KEY_LISTS[name]
        if rows is not None:
            root_updates[list_key] = _key_list(rows, current["credentials"][name], label)
        elif list_key in effective:
            raise HTTPException(409, "密钥已升级为列表，请刷新页面后使用新版联网搜索设置。")
        env_updates.update({key: "" for key in env_names})
        root_updates[root_key] = ""  # Clear legacy aliases too; a removed key must not reappear.
        if rows is None and not clear:
            env_updates[env_names[0]] = value
    if env_updates or root_updates:
        patch = {"env": env_updates, **root_updates, "webSearchSettingsRevision": uuid4().hex}
        proposed = resolve_search_configuration(config.merge_settings(effective, patch))
        selected = proposed["provider"]
        if selected in {"brave", "tavily"} and not proposed[selected]:
            raise HTTPException(400, "所选搜索服务需要至少一把启用的密钥；请添加或启用密钥，或选择自动、公开搜索后保存。")
        config.save_mini_code_settings(patch)
        if os.name != "nt":
            config.MINI_CODE_SETTINGS_PATH.chmod(0o600)
    return search_settings_view(cwd)


def install_search_settings_routes(app) -> None:
    @app.get("/api/settings/search")
    def search_get():
        return JSONResponse(search_settings_view(app.state.current_workspace), headers={"Cache-Control": "no-store"})

    @app.put("/api/settings/search")
    def search_put(update: SearchSettingsUpdate):
        return JSONResponse(save_search_settings(update, app.state.current_workspace), headers={"Cache-Control": "no-store"})

    @app.post("/api/settings/search/{service}/keys/{key_id}/usage")
    def search_usage(service: Literal["brave", "tavily"], key_id: str, update: SearchUsageRefresh):
        with config._SETTINGS_WRITE_LOCK:
            effective = config.load_effective_settings(app.state.current_workspace)
            if update.revision != effective.get("webSearchSettingsRevision", ""):
                raise HTTPException(409, "搜索设置已改变，请重新读取设置后查询额度。")
            rows = resolve_search_configuration(effective)["credentials"][service]
            row = next((item for item in rows if item["id"] == key_id), None)
            if row is None:
                raise HTTPException(404, "密钥不存在，请先保存或重新读取设置。")
            if service == "brave" and not update.allow_search:
                raise HTTPException(400, "Brave 查询会发起一次搜索，请使用页面上的明确查询按钮。")
            key = row["apiKey"]
        try:
            usage = refresh_usage(service, key)
        except (OSError, sqlite3.Error) as error:
            raise HTTPException(503, "暂时无法保存额度记录，请稍后重试。") from error
        # An in-flight query must not display the old key's balance after replacement.
        latest = resolve_search_configuration(config.load_effective_settings(app.state.current_workspace))
        if not any(item["id"] == key_id and item["apiKey"] == key for item in latest["credentials"][service]):
            raise HTTPException(409, "查询期间密钥已改变，请重新读取设置后查询。")
        return JSONResponse(usage, headers={"Cache-Control": "no-store"})
