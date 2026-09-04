from __future__ import annotations

import html as html_module
import re
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

from minicode.tooling import ToolDefinition, ToolResult

MAX_RESULTS = 10
_USER_AGENT = "MiniCode-Python/0.5.0 (local coding assistant)"
_NEWS_QUERY_MARKERS = (
    "近况",
    "最新",
    "新闻",
    "消息",
    "近期",
    "最近",
    "实时",
    "today",
    "latest",
    "current",
    "recent",
    "news",
    "update",
)
_SCHEDULE_QUERY_MARKERS = (
    "下一场",
    "下一次",
    "什么时候",
    "赛程",
    "日程",
    "schedule",
    "calendar",
    "next race",
    "race date",
    "grand prix",
    "fixture",
)
_CRISTIANO_QUERY_MARKERS = ("c罗", "cristiano", "克里斯蒂亚诺")
_CRISTIANO_RESULT_ALIASES = (
    "c罗",
    "cristiano",
    "ronaldo",
    "克里斯蒂亚诺",
    "罗纳尔多",
)
_LOW_VALUE_CURRENT_RESULT_MARKERS = (
    "高德地图",
    "百度地图",
    "map.baidu.",
    "百度百科",
    "baike.baidu.",
    "维基百科",
    "wikipedia.org",
    "教程",
    "tutorial",
    "入门指南",
)
_QUERY_STOP_WORDS = {
    "the",
    "and",
    "for",
    "today",
    "latest",
    "current",
    "recent",
    "news",
    "update",
    "updates",
    "消息",
    "新闻",
    "近况",
    "最新",
    "近期",
    "最近",
    "实时",
}
_QUERY_WEAK_TERMS = {
    "a",
    "about",
    "at",
    "calendar",
    "date",
    "dates",
    "event",
    "events",
    "find",
    "for",
    "game",
    "games",
    "in",
    "is",
    "match",
    "matches",
    "next",
    "of",
    "official",
    "on",
    "race",
    "races",
    "result",
    "results",
    "schedule",
    "time",
    "times",
    "upcoming",
    "what",
    "when",
    "where",
    "who",
    "with",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}
_QUERY_WEAK_PHRASES = (
    "什么时候",
    "下一次",
    "下一场",
    "最近",
    "赛程",
    "日程",
    "比赛",
    "赛事",
    "日期",
    "时间",
    "结果",
    "直播",
    "是什么",
    "哪一场",
    "查询",
)
_SHORT_ENTITY_TERMS = {
    "ai",
    "atp",
    "cpu",
    "f1",
    "gpu",
    "ios",
    "mlb",
    "nba",
    "nfl",
    "nhl",
    "npm",
    "php",
    "ufc",
    "vue",
    "wta",
}
_TERM_ALIASES = {
    "f1": ("f1", "formula 1", "formula one"),
}
_CONTEXT_TERM_ALIASES = {
    "calendar": ("calendar", "schedule", "赛程", "日程"),
    "date": ("date", "日期", "时间"),
    "next": ("next", "upcoming", "下一", "预约"),
    "race": ("race", "grand prix", "大奖赛", "比赛", "赛事"),
    "races": ("race", "races", "grand prix", "大奖赛", "比赛", "赛事"),
    "schedule": ("schedule", "calendar", "赛程", "日程"),
    "upcoming": ("upcoming", "next", "下一", "预约"),
}
_HIGH_CONFIDENCE_SOURCE_MARKERS = (
    "associated press",
    "apnews.com",
    "reuters",
    "bbc",
    "cnn",
    "espn",
    "fox sports",
    "sports illustrated",
    "usa today",
    "the guardian",
    "yahoo sports",
    "sky sports",
    "the athletic",
    "fifa",
    "uefa",
    "formula1.com",
    "fia.com",
    "新华社",
    "央视",
    "新浪体育",
    "懂球帝",
)


class _SearchProviderError(RuntimeError):
    """A provider answered, but did not return usable search results."""


def _validate(input_data: dict) -> dict:
    query = input_data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be non-empty")
    num_results = int(input_data.get("num_results", 5))
    if num_results < 1 or num_results > MAX_RESULTS:
        raise ValueError(f"num_results must be between 1 and {MAX_RESULTS}")
    return {"query": query.strip(), "num_results": num_results}


MAX_SEARCH_RESPONSE_BYTES = 2_000_000


def _request_text(url: str, *, accept: str, timeout: int = 15) -> str:
    from minicode.tools.web_fetch import _LimitedRedirectHandler, _build_safe_opener, _is_safe_url

    safe, reason = _is_safe_url(url)
    if not safe:
        raise _SearchProviderError(reason)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": accept},
    )
    with _build_safe_opener(_LimitedRedirectHandler()).open(request, timeout=timeout) as response:
        payload = response.read(MAX_SEARCH_RESPONSE_BYTES + 1)
        if len(payload) > MAX_SEARCH_RESPONSE_BYTES:
            raise _SearchProviderError(
                f"Search provider response exceeded {MAX_SEARCH_RESPONSE_BYTES} bytes"
            )
        return payload.decode("utf-8", errors="replace")


def _clean_markup(value: str, limit: int = 240) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value or "")
    cleaned = " ".join(html_module.unescape(without_tags).split())
    return cleaned[:limit]


def _is_news_query(query: str) -> bool:
    lowered = query.casefold()
    return any(marker in lowered for marker in _NEWS_QUERY_MARKERS)


def _is_schedule_query(query: str) -> bool:
    lowered = query.casefold()
    return any(marker in lowered for marker in _SCHEDULE_QUERY_MARKERS)


def _canonical_news_query(query: str) -> str:
    """Add an unambiguous football context for Cristiano Ronaldo news lookups."""

    lowered = query.casefold()
    is_cristiano_query = any(marker in lowered for marker in _CRISTIANO_QUERY_MARKERS)
    if not is_cristiano_query:
        return query

    normalized = re.sub(r"c罗", "Cristiano Ronaldo", query, flags=re.IGNORECASE)
    normalized = re.sub(
        r"克里斯蒂亚诺(?:[·\s]*罗纳尔多)?",
        "Cristiano Ronaldo",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bcristiano(?:\s+ronaldo)?\b",
        "Cristiano Ronaldo",
        normalized,
        flags=re.IGNORECASE,
    )
    for marker in _NEWS_QUERY_MARKERS:
        normalized = re.sub(re.escape(marker), " ", normalized, flags=re.IGNORECASE)
    normalized = " ".join(normalized.split())
    context = normalized.casefold()
    additions: list[str] = []
    if not any(marker in context for marker in ("al nassr", "al-nassr", "利雅得胜利")):
        additions.append("Al Nassr")
    if not any(marker in context for marker in ("portugal", "葡萄牙")):
        additions.append("Portugal")
    if not any(marker in context for marker in ("football", "soccer", "足球")):
        additions.append("football")
    return " ".join([normalized, *additions]).strip()


def _provider_query(query: str) -> str:
    if _is_news_query(query):
        return _canonical_news_query(query)
    if _is_schedule_query(query) and "official" not in query.casefold():
        return f"{query} official"
    return query


def _subject_terms(query: str) -> list[str]:
    """Extract distinct entity/topic terms, retaining short names such as F1."""

    lowered = query.casefold()
    for marker in sorted((*_NEWS_QUERY_MARKERS, *_QUERY_WEAK_PHRASES), key=len, reverse=True):
        lowered = lowered.replace(marker, " ")
    candidates = re.findall(r"[a-z0-9][a-z0-9+._-]+|[\u3400-\u9fff]{2,}", lowered)
    terms: list[str] = []
    if "c罗" in query.casefold():
        terms.append("c罗")
    for term in candidates:
        clean = term.strip("+._-")
        if (
            not clean
            or clean.isdigit()
            or clean in _QUERY_STOP_WORDS
            or clean in _QUERY_WEAK_TERMS
            or clean in terms
        ):
            continue
        terms.append(clean)
    return terms


def _context_terms(query: str) -> list[str]:
    """Return dates and intent terms used for ranking, never for pass/fail."""

    candidates = re.findall(
        r"[a-z0-9][a-z0-9+._-]+|[\u3400-\u9fff]{2,}",
        query.casefold(),
    )
    terms: list[str] = []
    for term in candidates:
        clean = term.strip("+._-")
        if (
            clean
            and clean not in terms
            and (
                clean.isdigit()
                or clean in _QUERY_WEAK_TERMS
                or clean in _SCHEDULE_QUERY_MARKERS
            )
        ):
            terms.append(clean)
    return terms


def _normalize_search_text(value: str) -> str:
    lowered = html_module.unescape(value or "").casefold()
    return " ".join(re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", lowered).split())


def _quoted_phrases(query: str) -> list[str]:
    phrases: list[str] = []
    for match in re.finditer(r'["“]([^"”]{3,})["”]', query):
        normalized = _normalize_search_text(match.group(1))
        if normalized and normalized not in phrases:
            phrases.append(normalized)
    return phrases


def _term_matches(term: str, normalized_text: str) -> bool:
    aliases = _TERM_ALIASES.get(term, (term,))
    for alias in aliases:
        normalized_alias = _normalize_search_text(alias)
        if not normalized_alias:
            continue
        if re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])",
            normalized_text,
        ):
            return True
    return False


def _context_term_matches(term: str, normalized_text: str) -> bool:
    for alias in _CONTEXT_TERM_ALIASES.get(term, (term,)):
        normalized_alias = _normalize_search_text(alias)
        if normalized_alias and re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])",
            normalized_text,
        ):
            return True
    return False


def _source_authority_score(raw_searchable: str) -> int:
    lowered = raw_searchable.casefold()
    return 2 if any(marker in lowered for marker in _HIGH_CONFIDENCE_SOURCE_MARKERS) else 0


def _is_entity_anchor(term: str) -> bool:
    return (
        term in _SHORT_ENTITY_TERMS
        or bool(re.search(r"[a-z]", term) and re.search(r"\d", term))
        or any("\u3400" <= char <= "\u9fff" for char in term)
    )


def _overall_relevance(results: list[dict[str, str]]) -> str:
    return "strong" if any(result.get("_relevance") == "strong" for result in results) else "moderate"


def _filter_relevant_results(query: str, results: list[dict[str, str]]) -> list[dict[str, str]]:
    """Require entity/topic overlap instead of accepting a shared year or intent word."""

    lowered_query = query.casefold()
    require_cristiano = any(marker in lowered_query for marker in _CRISTIANO_QUERY_MARKERS)
    subject_terms = _subject_terms(query)
    context_terms = _context_terms(query)
    quoted_phrases = _quoted_phrases(query)
    entity_anchors = [term for term in subject_terms if _is_entity_anchor(term)]
    time_sensitive_intent = _is_news_query(query) or _is_schedule_query(query)
    filtered: list[dict[str, str]] = []

    for result in results:
        raw_searchable = " ".join(
            str(result.get(key, ""))
            for key in ("title", "snippet", "source", "url")
        )
        searchable = raw_searchable.casefold()
        normalized_searchable = _normalize_search_text(raw_searchable)
        if require_cristiano and not any(alias in searchable for alias in _CRISTIANO_RESULT_ALIASES):
            continue
        if quoted_phrases and not any(phrase in normalized_searchable for phrase in quoted_phrases):
            continue
        if (
            time_sensitive_intent
            and any(marker in searchable for marker in _LOW_VALUE_CURRENT_RESULT_MARKERS)
        ):
            continue

        matched_terms = [term for term in subject_terms if _term_matches(term, normalized_searchable)]
        matched_context_terms = [
            term for term in context_terms if _context_term_matches(term, normalized_searchable)
        ]
        matched_anchors = [term for term in entity_anchors if term in matched_terms]
        if require_cristiano:
            relevance = "strong"
            if not matched_terms:
                matched_terms = ["Cristiano Ronaldo"]
        elif entity_anchors:
            if not matched_anchors:
                continue
            relevance = "strong"
        elif len(subject_terms) == 1:
            if not matched_terms:
                continue
            relevance = "strong"
        elif subject_terms:
            required_matches = 2 if len(subject_terms) <= 5 else 3
            if len(matched_terms) < required_matches:
                continue
            relevance = (
                "strong"
                if quoted_phrases or len(matched_terms) / len(subject_terms) >= 0.6
                else "moderate"
            )
        else:
            # A query made only of dates/intent words is too ambiguous to certify.
            continue

        annotated = dict(result)
        annotated["_relevance"] = relevance
        annotated["_matched_terms"] = ", ".join(matched_terms)
        annotated["_matched_context_terms"] = ", ".join(matched_context_terms)
        annotated["_ranking_score"] = str(
            len(matched_context_terms) * 2 + _source_authority_score(raw_searchable)
        )
        filtered.append(annotated)
    filtered.sort(
        key=lambda item: (
            int(item.get("_ranking_score", "0")),
            item.get("_relevance") == "strong",
        ),
        reverse=True,
    )
    return filtered


def _has_cross_source_dated_evidence(results: list[dict[str, str]]) -> bool:
    dated_results = [result for result in results if result.get("published") and result.get("source")]
    sources = {result["source"].casefold().strip() for result in dated_results}
    trusted = any(
        marker in f"{result.get('source', '')} {result.get('title', '')}".casefold()
        for result in dated_results
        for marker in _HIGH_CONFIDENCE_SOURCE_MARKERS
    )
    return len(dated_results) >= 3 and len(sources) >= 2 and trusted


def _search_google_news_rss(query: str, max_results: int) -> list[dict[str, str]]:
    is_chinese = bool(re.search(r"[\u3400-\u9fff]", query))
    locale = "hl=zh-CN&gl=CN&ceid=CN:zh-Hans" if is_chinese else "hl=en-US&gl=US&ceid=US:en"
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&" + locale
    payload = _request_text(url, accept="application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise _SearchProviderError("Google News RSS 返回了无法解析的内容") from error

    results: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = _clean_markup(item.findtext("title") or "", 200)
        link = (item.findtext("link") or "").strip()
        snippet = _clean_markup(item.findtext("description") or "")
        published = _clean_markup(item.findtext("pubDate") or "", 100)
        source_node = item.find("source")
        source = _clean_markup(source_node.text if source_node is not None and source_node.text else "", 100)
        if title and link.startswith(("http://", "https://")):
            result = {"title": title, "url": link, "snippet": snippet}
            if source:
                result["source"] = source
            if published:
                result["published"] = published
            results.append(result)
        if len(results) >= max_results:
            break
    if not results:
        raise _SearchProviderError("Google News RSS 没有返回可用结果")
    return results


def _search_bing_rss(query: str, max_results: int) -> list[dict[str, str]]:
    url = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(query)
    payload = _request_text(url, accept="application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise _SearchProviderError("Bing RSS 返回了无法解析的内容") from error

    results: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = _clean_markup(item.findtext("title") or "", 200)
        link = (item.findtext("link") or "").strip()
        snippet = _clean_markup(item.findtext("description") or "")
        published = _clean_markup(item.findtext("pubDate") or "", 100)
        if title and link.startswith(("http://", "https://")):
            result = {"title": title, "url": link, "snippet": snippet}
            if published:
                result["published"] = published
            results.append(result)
        if len(results) >= max_results:
            break
    if not results:
        raise _SearchProviderError("Bing RSS 没有返回可用结果")
    return results


def _unwrap_duckduckgo_url(value: str) -> str:
    decoded = html_module.unescape(value)
    parsed = urllib.parse.urlparse(decoded)
    if parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [])
        if target:
            return target[0]
    return decoded


def _parse_duckduckgo_results(payload: str, max_results: int) -> list[dict[str, str]]:
    """Parse DuckDuckGo's HTML result page when it is actually available."""

    results: list[dict[str, str]] = []
    result_pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    for match in result_pattern.finditer(payload):
        url = _unwrap_duckduckgo_url(match.group(1))
        title = _clean_markup(match.group(2), 200)
        snippet = _clean_markup(match.group(3))
        if url.startswith(("http://", "https://")) and title:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def _search_duckduckgo(query: str, max_results: int) -> list[dict[str, str]]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    payload = _request_text(url, accept="text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8")
    lowered = payload.lower()
    if "complete the following challenge" in lowered or "select all squares containing a duck" in lowered:
        raise _SearchProviderError("DuckDuckGo 要求人机验证")
    results = _parse_duckduckgo_results(payload, max_results)
    if not results:
        raise _SearchProviderError("DuckDuckGo 没有返回可用结果")
    return results


def _short_error(error: Exception) -> str:
    reason = getattr(error, "reason", error)
    return " ".join(str(reason).split())[:180] or error.__class__.__name__


def _format_results(
    query: str,
    provider: str,
    results: list[dict[str, str]],
    *,
    filtered_out: int = 0,
    provider_query: str = "",
) -> str:
    relevance = _overall_relevance(results)
    matched_terms = sorted(
        {
            term.strip()
            for result in results
            for term in result.get("_matched_terms", "").split(",")
            if term.strip()
        }
    )
    lines = [
        f"Search results for: {query}",
        f"PROVIDER: {provider}",
        f"PROVIDER_QUERY: {provider_query or query}",
        f"RELEVANCE_FILTER: {relevance}",
        f"RELEVANCE_MATCHED_TERMS: {', '.join(matched_terms) or 'not available'}",
        "=" * 60,
        "",
    ]
    for index, result in enumerate(results, 1):
        lines.extend([f"{index}. {result['title']}", f"   URL: {result['url']}"])
        if result.get("source"):
            lines.append(f"   Source: {result['source']}")
        if result.get("published"):
            lines.append(f"   Published: {result['published']}")
        if result.get("_matched_terms"):
            lines.append(f"   Matched query terms: {result['_matched_terms']}")
        if result.get("_matched_context_terms"):
            lines.append(f"   Matched query context: {result['_matched_context_terms']}")
        lines.extend([f"   {result['snippet']}", ""])
    lines.append(f"Total results: {len(results)}")
    if filtered_out:
        lines.append(f"Irrelevant results discarded: {filtered_out}")
    if provider == "Bing RSS" and any(result.get("published") for result in results):
        lines.append("DATE_NOTE: Bing RSS dates can be indexing dates; verify event dates in the page body.")
    if _is_news_query(query):
        if _has_cross_source_dated_evidence(results):
            lines.append("EVIDENCE_READY: cross-source dated news results")
        lines.extend(
            [
                "SEARCH_GUIDANCE: This is a current/news query. Treat snippets as leads, not final proof.",
                "If EVIDENCE_READY is present, answer from the named, dated headlines and label headline-level details.",
                "Otherwise fetch the strongest source first; expand to 2-3 sources only for missing or conflicting facts.",
                "Do not repeat equivalent searches.",
            ]
        )
    elif _is_schedule_query(query):
        lines.extend(
            [
                "SEARCH_GUIDANCE: This is a schedule lookup. Prefer an official organizer or governing-body result.",
                "Fetch the strongest result once and answer if its page contains the event date/time; search again only on conflict.",
            ]
        )
    return "\n".join(lines)


def _run(input_data: dict, context) -> ToolResult:
    query = input_data["query"]
    num_results = input_data["num_results"]
    failures: list[str] = []
    provider_query = _provider_query(query)

    providers = (
        (
            ("Google News RSS", _search_google_news_rss),
            ("Bing RSS", _search_bing_rss),
            ("DuckDuckGo HTML", _search_duckduckgo),
        )
        if _is_news_query(query)
        else (
            ("Bing RSS", _search_bing_rss),
            ("DuckDuckGo HTML", _search_duckduckgo),
        )
    )
    best_moderate_result: ToolResult | None = None
    for provider_name, provider in providers:
        try:
            results = provider(provider_query, num_results)
            relevant_results = _filter_relevant_results(query, results)
            if not relevant_results:
                raise _SearchProviderError(f"{provider_name} 返回的结果与查询主题无关")
            candidate = ToolResult(
                ok=True,
                output=_format_results(
                    query,
                    provider_name,
                    relevant_results,
                    filtered_out=len(results) - len(relevant_results),
                    provider_query=provider_query,
                ),
            )
            if _overall_relevance(relevant_results) == "strong":
                return candidate
            if best_moderate_result is None:
                best_moderate_result = candidate
        except (urllib.error.URLError, TimeoutError, _SearchProviderError) as error:
            failures.append(f"- {provider_name}: {_short_error(error)}")
        except Exception as error:  # noqa: BLE001 - isolate one public provider from the next
            failures.append(f"- {provider_name}: {_short_error(error)}")

    if best_moderate_result is not None:
        return best_moderate_result

    return ToolResult(
        ok=False,
        output="\n".join(
            [
                "WEB_SEARCH_UNAVAILABLE",
                "联网搜索暂时不可用：公共搜索入口没有返回可用结果。",
                *failures,
                "建议：稍后重试，换一个更具体的关键词，或用 web_fetch 读取已知网址。",
                f"QUERY: {query}",
            ]
        ),
    )


web_search_tool = ToolDefinition(
    name="web_search",
    description=(
        "Search the public web through multiple no-key providers and return topic-matched titles, URLs, snippets, "
        "sources, publication dates, and an explicit relevance strength. Short entities such as F1 are preserved, "
        "while years and generic date words cannot make an unrelated result pass. Current/news queries prefer "
        "Google News RSS; schedule queries prefer official results. For a simple lookup, search once, fetch the "
        "strongest source, and answer when the requested fact is present instead of repeating equivalent searches. "
        "This is a network-backed capability: a registered tool can still be unavailable because of "
        "connectivity, provider rate limits, or human-verification pages."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
            "num_results": {"type": "number", "description": "Number of results to return (1-10, default: 5)"},
        },
        "required": ["query"],
    },
    validator=_validate,
    run=_run,
)
