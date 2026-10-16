"""Search configuration shared by runtime loading, tools and the settings API."""
from __future__ import annotations

import os
from collections.abc import Mapping

SEARCH_PROVIDERS = ("auto", "public", "brave", "tavily")
SEARCH_FIELDS = {
    "provider": (("MINI_CODE_SEARCH_PROVIDER",), "webSearchProvider", "auto"),
    "brave": (("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"), "braveSearchApiKey", ""),
    "tavily": (("TAVILY_API_KEY",), "tavilyApiKey", ""),
}
SEARCH_KEY_LISTS = {"brave": "braveSearchApiKeys", "tavily": "tavilyApiKeys"}
MAX_SEARCH_KEYS = 20


def _saved_keys(values: dict, name: str, legacy: str) -> list[dict]:
    field = SEARCH_KEY_LISTS[name]
    if field not in values:
        return [{"id": f"legacy-{name}", "name": "原有密钥", "apiKey": legacy, "enabled": True}] if legacy else []
    rows = values[field]
    if not isinstance(rows, list) or len(rows) > MAX_SEARCH_KEYS:
        raise ValueError("Invalid search credential list.")
    result, ids, secrets = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("Invalid search credential entry.")
        key, identifier, label = row.get("apiKey"), row.get("id"), row.get("name", "")
        enabled = row.get("enabled", True)
        if (not isinstance(key, str) or not key.strip() or not isinstance(identifier, str) or not identifier
                or not isinstance(label, str) or not isinstance(enabled, bool)
                or identifier in ids or key.strip() in secrets):
            raise ValueError("Invalid or duplicate search credential entry.")
        ids.add(identifier)
        secrets.add(key.strip())
        result.append({"id": identifier, "name": label.strip() or "密钥", "apiKey": key.strip(), "enabled": enabled})
    return result


def resolve_search_configuration(values: dict | None = None, *, environment: Mapping | None = None) -> dict:
    values = values or {}
    environment = os.environ if environment is None else environment
    saved_env = values.get("env", {})
    saved_env = saved_env if isinstance(saved_env, dict) else {}
    resolved, sources = {}, {}
    for name, (env_names, root_key, default) in SEARCH_FIELDS.items():
        candidates = [(environment.get(key), "environment") for key in env_names]
        candidates += [(saved_env.get(key), "settings") for key in env_names]
        candidates += [(values.get(root_key), "settings")]
        value, source = next(((value.strip(), source) for value, source in candidates
                              if isinstance(value, str) and value.strip()), (default, "default"))
        resolved[name] = value.lower() if name == "provider" else value
        sources[name] = source
    credentials, errors = {}, []
    for name, field in SEARCH_KEY_LISTS.items():
        if sources[name] == "environment":
            credentials[name] = [{"id": f"environment-{name}", "name": "启动环境密钥",
                                  "apiKey": resolved[name], "enabled": True}]
        else:
            try:
                credentials[name] = _saved_keys(values, name, resolved[name])
            except (TypeError, ValueError):
                credentials[name] = []
                errors.append(f"{name} 密钥列表格式无效，请在联网搜索设置中重新保存。")
            resolved[name] = next((row["apiKey"] for row in credentials[name] if row["enabled"]), "")
            sources[name] = "settings" if credentials[name] or field in values else sources[name]
    return {**resolved, "sources": sources, "credentials": credentials, "errors": errors}


def runtime_search_configuration(values: dict) -> dict:
    resolved = resolve_search_configuration(values)
    runtime = {root_key: resolved[name] for name, (_env_names, root_key, _default) in SEARCH_FIELDS.items()}
    for name, field in SEARCH_KEY_LISTS.items():
        # Preserve invalid lists for the tool's configuration check, rather than hiding errors.
        runtime[field] = values[field] if resolved["errors"] and field in values else resolved["credentials"][name]
    return runtime
