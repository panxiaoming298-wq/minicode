"""Prepare provider cache controls; only provider usage can prove a cache hit."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit


class PreparePromptCache:
    def execute(self, body, *, mode, protocol, provider, endpoint, namespace, permissions):
        if mode not in {"off", "auto", "stable-prefix"}:
            raise ValueError("runtimePromptCache must be off, auto or stable-prefix")
        if mode == "off":
            return dict(body), None
        prepared = deepcopy(dict(body))
        parsed = urlsplit(endpoint)
        origin = f"{parsed.scheme}://{parsed.hostname or ''}:{parsed.port or ''}{parsed.path}"
        tools = prepared.get("tools", [])
        if isinstance(tools, list):
            tools = sorted(tools, key=lambda t: str(t.get("name") or t.get("function", {}).get("name") or ""))
            prepared["tools"] = json.loads(json.dumps(tools, ensure_ascii=False, sort_keys=True))
        prefix = prepared.get("system") or [m for m in prepared.get("messages", []) if m.get("role") in {"system", "developer"}]
        if protocol == "anthropic" and isinstance(prefix, list):
            prefix = [{k: v for k, v in block.items() if k != "cache_control"} for block in prefix]
        if protocol == "anthropic" and isinstance(prefix, str):
            prefix = [{"type": "text", "text": prefix}]
        scope = {"provider": provider, "protocol": protocol, "endpoint": origin,
            "namespace": namespace, "model": prepared.get("model"), "tools": prepared.get("tools", []),
            "permissions": list(permissions), "prefix": prefix,
            "generation": {k: prepared[k] for k in ("temperature", "top_p", "reasoning_effort", "thinking", "output_config", "tool_choice", "response_format") if k in prepared}}
        identity = hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        method = "provider_automatic_or_unsupported"
        if parsed.scheme == "https" and parsed.hostname == "api.openai.com" and protocol == "openai" and namespace:
            prepared["prompt_cache_key"] = identity
            method = "openai_cache_key"
        elif parsed.scheme == "https" and parsed.hostname == "api.anthropic.com" and protocol == "anthropic":
            if mode == "auto":
                prepared["cache_control"] = {"type": "ephemeral"}
                method = "anthropic_automatic"
            elif isinstance(prepared.get("system"), str) and prepared["system"]:
                prepared["system"] = [{"type": "text", "text": prepared["system"], "cache_control": {"type": "ephemeral"}}]
                method = "anthropic_stable_system"
            elif isinstance(prepared.get("system"), list) and prepared["system"]:
                prepared["system"][-1]["cache_control"] = {"type": "ephemeral"}
                method = "anthropic_stable_system"
        return prepared, {"policy": "provider-prompt-cache-v1", "mode": mode, "method": method,
            "scope_sha256": identity, "hit": "unknown_until_provider_usage", "prewarm_requests": 0}
