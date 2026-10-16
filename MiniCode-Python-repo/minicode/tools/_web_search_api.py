"""Official search API adapters; credentials never come from model arguments."""
from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable

from minicode.search_configuration import SEARCH_PROVIDERS, resolve_search_configuration
from minicode.search_usage import mark_usage_stale, request_brave_with_usage
from minicode.web_transport import WebRequestError, request_text


def settings(runtime: dict | None = None) -> dict:
    config = resolve_search_configuration(runtime)
    if config["errors"]:
        raise WebRequestError("configuration", " ".join(config["errors"]))
    provider = config["provider"]
    if provider not in SEARCH_PROVIDERS:
        raise WebRequestError("configuration", "Search provider must be auto, public, brave or tavily.")
    if provider in {"brave", "tavily"} and not config[provider]:
        raise WebRequestError("configuration", f"{provider} search requires its own API key; model API keys are not search credentials.")
    return config


def _with_credentials(keys: tuple[str, ...], request: Callable[[str], dict]) -> dict:
    """Try each enabled key at most once; transport failures do not consume other keys.

    Each request inherits the search's shared deadline/cancellation scope. Switching
    keys never adds another overall timeout or exposes credential values in errors.
    """
    for index, key in enumerate(keys):
        try:
            return request(key)
        except WebRequestError as error:
            if error.code not in {"authentication", "rate_limited"}:
                raise
            if index == len(keys) - 1:
                raise WebRequestError(error.code, f"All {len(keys)} enabled API keys were rejected or rate limited.",
                                      retry_after=error.retry_after) from None
    raise WebRequestError("configuration", "No enabled search API key is available.")


def _json_response(url: str, *, headers: dict, body: dict | None = None) -> dict:
    text = request_text(url, headers={"Accept": "application/json", **headers}, body=body, timeout=12)
    return _parse_json(text)


def _parse_json(text: str) -> dict:
    try:
        result = json.loads(text)
    except ValueError as error:
        raise WebRequestError("invalid_response", "Search API returned non-JSON content.") from error
    if not isinstance(result, dict):
        raise WebRequestError("invalid_response", "Search API returned an invalid result object.")
    return result


def parse_brave(data: dict, limit: int) -> list[dict[str, str]]:
    web = data.get("web") or {}
    items = web.get("results", []) if isinstance(web, dict) else []
    results = []
    for item in items:
        if not isinstance(item, dict) or not item.get("url") or not item.get("title"):
            continue
        result = {"title": str(item["title"]), "url": str(item["url"]),
                  "snippet": str(item.get("description") or "")}
        if item.get("page_age"):
            result["published"] = str(item["page_age"])
        results.append(result)
    return results[:limit]


def parse_tavily(data: dict, limit: int) -> list[dict[str, str]]:
    results = []
    for item in data.get("results") or []:
        if not isinstance(item, dict) or not item.get("url") or not item.get("title"):
            continue
        result = {"title": str(item["title"]), "url": str(item["url"]),
                  "snippet": str(item.get("content") or "")}
        if item.get("published_date"):
            result["published"] = str(item["published_date"])
        results.append(result)
    # A generated "answer" is deliberately ignored: it is not source evidence.
    return results[:limit]


def providers(config: dict, *, domains: list[str], freshness: str, news: bool) -> list[tuple[str, object]]:
    selected = config["provider"]
    if selected == "public":
        return []
    names = [selected] if selected != "auto" else [name for name in ("brave", "tavily") if config[name]]
    result = []
    for name in names:
        keys = tuple(row["apiKey"] for row in config["credentials"][name] if row["enabled"])
        if name == "brave":
            def brave(query: str, limit: int, keys=keys):
                scoped = query + (" (" + " OR ".join("site:" + host for host in domains) + ")" if domains else "")
                parameters = {"q": scoped, "count": min(limit, 20), "text_decorations": "false"}
                if freshness != "any":
                    parameters["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[freshness]
                if any("\u3400" <= char <= "\u9fff" for char in query):
                    parameters["search_lang"] = "zh-hans"
                url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(parameters)
                data = _with_credentials(keys, lambda key: _parse_json(request_brave_with_usage(url, key).text))
                return parse_brave(data, limit)
            result.append(("Brave Search API", brave))
        elif name == "tavily":
            def tavily(query: str, limit: int, keys=keys):
                body = {"query": query, "max_results": min(limit, 20), "search_depth": "basic",
                        "topic": "news" if news else "general", "include_answer": False,
                        "include_raw_content": False, "include_published_date": True,
                        "include_domains": domains}
                if freshness != "any":
                    body["time_range"] = freshness
                    body["filter_by_published_date"] = True
                data = _with_credentials(keys, lambda key: _json_response(
                    "https://api.tavily.com/search", headers={"Authorization": "Bearer " + key}, body=body))
                mark_usage_stale("tavily")
                return parse_tavily(data, limit)
            result.append(("Tavily Search API", tavily))
    return result
