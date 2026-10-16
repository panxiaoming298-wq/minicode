"""Bounded, credential-free history of model connections saved in the UI."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


MAX_CONNECTION_HISTORY = 20


def connection_history(*sources: Any) -> list[dict[str, str]]:
    """Keep the most recent unique routes; never copy secret or unknown fields."""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in sources:
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            if any(not isinstance(item.get(key), str) for key in ("provider", "model", "baseUrl")):
                continue
            provider = item["provider"].strip().lower()
            model = item["model"].strip()
            base_url = item["baseUrl"].strip().rstrip("/")
            if not provider or not model or not base_url:
                continue
            try:
                parsed = urlparse(base_url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                    continue
            except ValueError:
                continue
            identity = (provider, model, base_url)
            if identity in seen:
                continue
            seen.add(identity)
            result.append({"provider": provider, "model": model, "baseUrl": base_url})
            if len(result) == MAX_CONNECTION_HISTORY:
                return result
    return result
