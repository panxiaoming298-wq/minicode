from __future__ import annotations

import html as html_module
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from html.parser import HTMLParser
from xml.etree import ElementTree

from minicode.tooling import ToolDefinition, ToolResult
from minicode.tools._validation import validate_contract
from minicode.web_transport import WebRequestError, request_scope, request_text

MAX_RESULTS = 10
WEB_SEARCH_INPUT = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "minLength": 1, "description": "The nonblank search query"},
        "num_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": 5,
                        "description": "Number of results to return (integer 1-10, default: 5)"},
        "domains": {"type": "array", "maxItems": 5, "items": {"type": "string", "minLength": 1},
                    "description": "Optional source domains, e.g. docs.python.org; includes their subdomains"},
        "freshness": {"type": "string", "enum": ["any", "day", "week", "month", "year"], "default": "any",
                      "description": "Optional publication-age filter; use any for historical questions"},
    },
    "required": ["query"],
}
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
    "recent",
    "news",
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
    "fixture",
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
    "c罗": ("c罗", "cristiano", "克里斯蒂亚诺"),
    "cristiano": ("cristiano", "c罗", "克里斯蒂亚诺"),
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
_REFERENCE_DOMAINS = ("apnews.com", "reuters.com", "bbc.com", "bbc.co.uk", "espn.com",
    "theguardian.com", "fifa.com", "uefa.com", "formula1.com", "fia.com", "python.org")


class _SearchProviderError(RuntimeError):
    """A provider answered, but did not return usable search results."""


def _validate(input_data: dict) -> dict:
    validate_contract(input_data, WEB_SEARCH_INPUT)
    query = input_data["query"]
    if not query.strip():
        raise ValueError("$.query: must be nonblank")
    num_results = input_data.get("num_results", 5)
    domains = []
    for domain in input_data.get("domains", []):
        host = domain.strip().lower().rstrip(".")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise ValueError("$.domains: expected a domain name") from error
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", host):
            raise ValueError("$.domains: use bare domain names without paths, ports or credentials")
        if host not in domains:
            domains.append(host)
    return {"query": query.strip(), "num_results": num_results,
            "domains": domains, "freshness": input_data.get("freshness", "any")}


MAX_SEARCH_RESPONSE_BYTES = 2_000_000


def _request_text(url: str, *, accept: str, timeout: int = 15) -> str:
    return request_text(url, headers={"Accept": accept}, timeout=min(timeout, 10),
                        max_bytes=MAX_SEARCH_RESPONSE_BYTES)


def _clean_markup(value: str, limit: int = 240) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value or "")
    cleaned = " ".join(html_module.unescape(without_tags).split())
    return cleaned[:limit]


def _is_news_query(query: str) -> bool:
    return _has_markers(query, _NEWS_QUERY_MARKERS)


def _is_schedule_query(query: str) -> bool:
    return _has_markers(query, _SCHEDULE_QUERY_MARKERS)


def _has_markers(query: str, markers: tuple[str, ...]) -> bool:
    lowered = query.casefold()
    return any(re.search(r"\b" + re.escape(marker) + r"\b", lowered) if marker.isascii()
               else marker in lowered for marker in markers)


def _provider_query(query: str) -> str:
    # Preserve the question. Adding a club, country or "official" silently
    # changes historical and general queries; intent controls guidance only.
    return query


def _subject_terms(query: str) -> list[str]:
    """Extract distinct entity/topic terms, retaining short names such as F1."""

    lowered = query.casefold()
    lowered = re.sub(r"\bsite:\S+|\bwhen:\S+|(?<!\S)-\S+", " ", lowered)
    for marker in sorted((*_NEWS_QUERY_MARKERS, *_QUERY_WEAK_PHRASES), key=len, reverse=True):
        lowered = re.sub(r"\b" + re.escape(marker) + r"\b", " ", lowered) if marker.isascii() else lowered.replace(marker, " ")
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


def _source_authority_score(url: str) -> int:
    hostname = (urllib.parse.urlsplit(url).hostname or "").casefold()
    return 2 if any(hostname == host or hostname.endswith("." + host) for host in _REFERENCE_DOMAINS) else 0


def _is_entity_anchor(term: str) -> bool:
    return (
        term in _SHORT_ENTITY_TERMS
        or bool(re.search(r"[a-z]", term) and re.search(r"\d", term))
        or any("\u3400" <= char <= "\u9fff" for char in term)
    )


def _overall_relevance(results: list[dict[str, str]]) -> str:
    return "strong" if results and all(result.get("_relevance") == "strong" for result in results) else "moderate"


def _filter_relevant_results(query: str, results: list[dict[str, str]]) -> list[dict[str, str]]:
    """Require entity/topic overlap instead of accepting a shared year or intent word."""

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
        if quoted_phrases and not all(phrase in normalized_searchable for phrase in quoted_phrases):
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
        if entity_anchors:
            if not matched_anchors:
                continue
            # F1 or a person's name alone is a lead, not evidence for all the
            # other entities in a multi-part question.
            if len(subject_terms) > 1 and len(matched_terms) < min(2, len(subject_terms)):
                relevance = "moderate"
            else:
                relevance = "strong" if len(matched_terms) / len(subject_terms) >= 0.6 else "moderate"
        elif len(subject_terms) == 1:
            if not matched_terms:
                continue
            relevance = "strong"
        elif subject_terms:
            required_matches = 2
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

        if _is_schedule_query(query) and not matched_context_terms:
            relevance = "moderate"
        from minicode.web_http import _is_safe_url
        if not _is_safe_url(str(result.get("url", "")))[0]:
            continue

        # Keep source text inside its field: embedded newlines must not become
        # tool metadata, and verbose API snippets must not consume the context.
        annotated = dict(result)
        for field, limit in (("title", 250), ("snippet", 1500), ("source", 100), ("published", 100)):
            if field in annotated:
                annotated[field] = _clean_markup(str(annotated[field]), limit)
        annotated["_relevance"] = relevance
        annotated["_matched_terms"] = ", ".join(matched_terms)
        annotated["_matched_context_terms"] = ", ".join(matched_context_terms)
        annotated["_ranking_score"] = str(
            len(matched_context_terms) * 2 + len(matched_terms) * 3
            + _source_authority_score(result.get("source_url") or result.get("url", ""))
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


def _search_google_news_rss(query: str, max_results: int, *, freshness: str = "any") -> list[dict[str, str]]:
    is_chinese = bool(re.search(r"[\u3400-\u9fff]", query))
    locale = "hl=zh-CN&gl=CN&ceid=CN:zh-Hans" if is_chinese else "hl=en-US&gl=US&ceid=US:en"
    if freshness != "any":
        query += " when:" + {"day": "1d", "week": "7d", "month": "30d", "year": "1y"}[freshness]
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&" + locale
    payload = _request_text(url, accept="application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8")
    return _parse_google_news_rss(payload, max_results)


def _parse_google_news_rss(payload: str, max_results: int) -> list[dict[str, str]]:
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
            if source_node is not None and source_node.get("url"):
                result["source_url"] = source_node.get("url", "")
            results.append(result)
        if len(results) >= max_results:
            break
    if not results:
        raise _SearchProviderError("Google News RSS 没有返回可用结果")
    return results


def _search_bing_rss(query: str, max_results: int) -> list[dict[str, str]]:
    url = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(query)
    payload = _request_text(url, accept="application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8")
    return _parse_bing_rss(payload, max_results)


def _parse_bing_rss(payload: str, max_results: int) -> list[dict[str, str]]:
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

    class Results(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.results = []
            self.current = None
            self.field = ""
            self.depth = 0

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            classes = (attributes.get("class") or "").split()
            if tag == "a" and "result__a" in classes:
                self.current = {"url": _unwrap_duckduckgo_url(attributes.get("href") or ""), "title": "", "snippet": ""}
                self.results.append(self.current)
                self.field, self.depth = "title", 1
            elif "result__snippet" in classes and self.current is not None:
                self.field, self.depth = "snippet", 1
            elif self.field and tag not in {"br", "img", "input", "meta", "link", "hr", "wbr"}:
                self.depth += 1

        def handle_endtag(self, tag):
            if self.field:
                self.depth -= 1
                if self.depth <= 0:
                    self.field = ""

        def handle_data(self, text):
            if self.field and self.current is not None:
                self.current[self.field] += text + " "

    parser = Results()
    parser.feed(payload)
    parser.close()
    return [{"url": item["url"], "title": _clean_markup(item["title"], 250),
             "snippet": _clean_markup(item["snippet"], 1000)}
            for item in parser.results if item["title"].strip() and item["url"].startswith(("http://", "https://"))][:max_results]


def _search_duckduckgo(query: str, max_results: int, *, freshness: str = "any") -> list[dict[str, str]]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    if freshness != "any":
        url += "&df=" + {"day": "d", "week": "w", "month": "m", "year": "y"}[freshness]
    payload = _request_text(url, accept="text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8")
    lowered = payload.lower()
    if "complete the following challenge" in lowered or "select all squares containing a duck" in lowered:
        raise WebRequestError("human_verification", "DuckDuckGo 要求人机验证")
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
        if result.get("source_url"):
            lines.append(f"   Source homepage: {result['source_url']}")
        if result.get("_provider"):
            lines.append(f"   Search provider: {result['_provider']}")
        if result.get("_relevance"):
            lines.append(f"   Relevance: {result['_relevance']}")
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
    lines.append("EVIDENCE_LEVEL: search leads; relevance is not factual verification or proof of freshness.")
    if "Google News RSS" in provider:
        lines.append("SOURCE_NOTE: Google News links are aggregator redirects, not original article bodies. "
                     "Use the named publisher/title to locate original text if a redirect is unreadable.")
    if _is_news_query(query):
        lines.extend(
            [
                "SEARCH_GUIDANCE: This is a current/news query. Treat snippets as leads, not final proof.",
                "Fetch the strongest source first; expand to 2-3 sources for missing or conflicting facts.",
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
    else:
        lines.append("SEARCH_GUIDANCE: Read the strongest original source with web_fetch before asserting details. "
                     "For historical queries distinguish article publication dates from event dates.")
    return "\n".join(lines)


def _run(input_data: dict, context) -> ToolResult:
    from minicode.tools._web_search_api import providers as api_providers
    from minicode.tools._web_search_api import settings

    query = input_data["query"]
    num_results = input_data["num_results"]
    domains = input_data.get("domains", [])
    freshness = input_data.get("freshness", "any")
    try:
        config = settings(getattr(context, "_runtime", None))
    except WebRequestError as error:
        return ToolResult(ok=False, failureKind="invalid_input", output=f"WEB_SEARCH_CONFIG_ERROR\n{error}")

    deadline = time.monotonic() + 25
    stop = threading.Event()
    parent_cancel = getattr(context, "cancellation_event", None)

    class Cancellation:
        def is_set(self):
            return stop.is_set() or parent_cancel is not None and parent_cancel.is_set()

    cancellation = Cancellation()
    diagnostics: list[str] = []
    candidates: list[dict[str, str]] = []
    provider_query = _provider_query(query)
    # Public sources receive explicit source restrictions, but topic terms and
    # years remain intact. API adapters have native domain/freshness controls.
    public_query = provider_query + (" (" + " OR ".join("site:" + domain for domain in domains) + ")" if domains else "")
    site_domains, excluded_domains = _query_domain_filters(query)
    required_domains = domains or site_domains

    def search(provider_name, provider, sent_query, group_cancel):
        started = time.monotonic()
        try:
            with request_scope(deadline, group_cancel):
                results = provider(sent_query, min(20, max(10, num_results * 2)))
            relevant_results = _filter_relevant_results(query, results)
            relevant_results = [item for item in relevant_results if _matches_domains(item, required_domains)
                                and not (excluded_domains and _matches_domains(item, excluded_domains))]
            for item in relevant_results:
                item["_provider"] = provider_name
            status = "ok" if relevant_results else "irrelevant_results" if results else "no_results"
            return relevant_results, f"- {provider_name}: {status}; received={len(results)}, accepted={len(relevant_results)}; {time.monotonic()-started:.2f}s"
        except Exception as error:  # noqa: BLE001 - isolate provider failures so other sources remain available
            code = getattr(error, "code", "provider_error")
            return [], f"- {provider_name}: {code}; {_short_error(error)}; {time.monotonic()-started:.2f}s"

    def collect(group, sent_query):
        # Each live request owns a killable process. Stop signals are observed
        # before shutdown(wait=True), so no HTTP work survives this tool.
        group_stop = threading.Event()
        class GroupCancellation:
            def is_set(self):
                return cancellation.is_set() or group_stop.is_set()
        with ThreadPoolExecutor(max_workers=min(3, len(group)), thread_name_prefix="minicode-search") as pool:
            futures = [pool.submit(search, name, provider, sent_query, GroupCancellation()) for name, provider in group]
            for future in as_completed(futures):
                found, diagnostic = future.result()
                candidates.extend(found)
                if not (group_stop.is_set() and ": canceled;" in diagnostic):
                    diagnostics.append(diagnostic)
                merged_so_far = _merge_results(candidates, num_results)
                if len(merged_so_far) >= num_results:
                    group_stop.set()

    try:
        for provider in api_providers(config, domains=domains, freshness=freshness, news=_is_news_query(query)):
            collect([provider], provider_query)
            if len(_merge_results(candidates, num_results)) >= min(3, num_results):
                break
            if cancellation.is_set() or time.monotonic() >= deadline:
                break
        if len(_merge_results(candidates, num_results)) < min(3, num_results) and not cancellation.is_set():
            news_provider = ("Google News RSS", _search_google_news_rss if freshness == "any" else
                lambda q, n: _search_google_news_rss(q, n, freshness=freshness))
            duck_provider = ("DuckDuckGo HTML", _search_duckduckgo if freshness == "any" else
                lambda q, n: _search_duckduckgo(q, n, freshness=freshness))
            historical = any(int(year) < datetime.now(UTC).year for year in re.findall(r"\b(?:19|20)\d{2}\b", query))
            prefer_news = _is_news_query(query) or historical or freshness != "any"
            if prefer_news:
                collect([news_provider], public_query)
            if not candidates or (_overall_relevance(candidates) != "strong"
                                  and len(_merge_results(candidates, num_results)) < min(3, num_results)):
                public = [duck_provider] if freshness != "any" else [("Bing RSS", _search_bing_rss), duck_provider]
                if time.monotonic() < deadline and not cancellation.is_set():
                    collect(public, public_query)
            # A news index can also supply schedule announcements when general
            # indexes are unavailable; keep the query and label these as leads.
            if (not candidates and _is_schedule_query(query) and not prefer_news
                    and time.monotonic() < deadline and not cancellation.is_set()):
                collect([news_provider], public_query)
    finally:
        stop.set()

    if parent_cancel is not None and parent_cancel.is_set():
        return ToolResult(ok=False, failureKind="canceled", output="Canceled: parent Runtime canceled this tool call")
    merged = _merge_results(candidates, num_results)
    if merged:
        names = list(dict.fromkeys(item["_provider"] for item in merged))
        output = _format_results(query, ", ".join(names), merged, provider_query=provider_query)
        if "Bing RSS" in names:
            output += "\nDATE_NOTE: Bing RSS dates may be indexing dates, not publication or event dates."
        output += "\nFRESHNESS_REQUESTED: " + freshness
        output += "\nPROVIDER_DIAGNOSTICS:\n" + "\n".join(diagnostics)
        return ToolResult(ok=True, output=output)
    return ToolResult(ok=False, failureKind="remote_rejected", output="\n".join([
        "WEB_SEARCH_UNAVAILABLE", "联网搜索已结束，但没有取得符合查询条件的可用结果。",
        *diagnostics,
        ("NEXT_ACTION: Read a known original URL with web_fetch, or configure a search API. "
        "Do not repeat the identical search or treat model-generated links as retrieved sources."),
        "CONFIGURATION: MINI_CODE_SEARCH_PROVIDER=auto|public|brave|tavily; BRAVE_SEARCH_API_KEY or TAVILY_API_KEY.",
        "NETWORK: MINI_CODE_WEB_PROXY=auto (system proxy), direct, or an HTTP proxy URL.",
        f"QUERY: {query}",
    ]))


def _matches_domains(result: dict[str, str], domains: list[str]) -> bool:
    host = (urllib.parse.urlsplit(result.get("source_url") or result["url"]).hostname or "").lower().rstrip(".")
    return not domains or any(host == domain.lower() or host.endswith("." + domain.lower()) for domain in domains)


def _query_domain_filters(query: str) -> tuple[list[str], list[str]]:
    included, excluded = [], []
    for match in re.finditer(r"(?<![\w-])(-?)site:([a-zA-Z0-9.-]+)", query, re.IGNORECASE):
        (excluded if match[1] else included).append(match[2].lower().rstrip("."))
    return included, excluded


def _canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    parameters = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                  if not key.casefold().startswith("utm_") and key.casefold() not in {"gclid", "fbclid"}]
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/",
                                   urllib.parse.urlencode(sorted(parameters)), ""))


def _merge_results(results: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    ordered = sorted(results, key=lambda item: (item.get("_relevance") == "strong",
        int(item.get("_ranking_score", "0"))), reverse=True)
    unique = {}
    for item in ordered:
        unique.setdefault(_canonical_url(item["url"]), item)
    selected, deferred, counts = [], [], {}
    for item in unique.values():
        host = urllib.parse.urlsplit(item.get("source_url") or item["url"]).hostname or ""
        count = counts.get(host, 0)
        (selected if count < 2 else deferred).append(item)
        counts[host] = count + 1
    return (selected + deferred)[:limit]


web_search_tool = ToolDefinition(
    name="web_search",
    description=(
        "Search the web using configured official search APIs with public-source fallback; return source titles, URLs, snippets, "
        "sources, publication dates, and an explicit relevance strength. Short entities such as F1 are preserved, "
        "while years and generic date words cannot make an unrelated result pass. Current/news queries prefer "
        "Google News RSS. Optional domains and freshness restrict sources. A topic match is only a lead, not verified evidence. "
        "The query is preserved, including historical dates. For a simple lookup, search once, fetch the "
        "strongest source, and answer when the requested fact is present instead of repeating equivalent searches. "
        "This is a network-backed capability: a registered tool can still be unavailable because of "
        "connectivity, provider rate limits, or human-verification pages."
    ),
    input_schema=WEB_SEARCH_INPUT,
    validator=_validate,
    run=_run,
)
