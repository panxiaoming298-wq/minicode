from __future__ import annotations

import ssl
import socket
import urllib.error

import pytest

import minicode.tools.web_fetch as web_fetch_module
import minicode.tools.web_search as web_search_module
from minicode.tools.current_time import current_time_tool


def test_current_time_supports_iana_timezone_without_network_or_shell() -> None:
    input_data = current_time_tool.validator({"timezone": "Asia/Shanghai", "format": "full"})

    result = current_time_tool.run(input_data, None)

    assert result.ok is True
    assert "TIMEZONE: Asia/Shanghai" in result.output
    assert "UTC_OFFSET: +08:00" in result.output
    assert "DATE:" in result.output
    assert "WEEKDAY:" in result.output


def test_bing_rss_search_parser_returns_structured_results(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = """<?xml version="1.0" encoding="utf-8"?>
    <rss><channel><item>
      <title>信阳天气预报</title>
      <link>https://www.weather.com.cn/weather/101180601.shtml</link>
      <description>信阳今日天气与未来预报</description>
    </item></channel></rss>"""
    monkeypatch.setattr(web_search_module, "_request_text", lambda *args, **kwargs: payload)

    results = web_search_module._search_bing_rss("信阳天气", 5)

    assert results == [
        {
            "title": "信阳天气预报",
            "url": "https://www.weather.com.cn/weather/101180601.shtml",
            "snippet": "信阳今日天气与未来预报",
        }
    ]


def test_google_news_rss_parser_returns_source_and_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = """<?xml version="1.0" encoding="utf-8"?>
    <rss><channel><item>
      <title>C罗在利雅得胜利的最新表现 - 路透社</title>
      <link>https://news.google.com/rss/articles/example</link>
      <description>报道了 Cristiano Ronaldo 最近一场比赛的表现。</description>
      <pubDate>Tue, 01 Sep 2026 08:00:00 GMT</pubDate>
      <source url="https://reuters.com">路透社</source>
    </item></channel></rss>"""
    monkeypatch.setattr(web_search_module, "_request_text", lambda *args, **kwargs: payload)

    results = web_search_module._search_google_news_rss("C罗 近况", 5)

    assert results == [
        {
            "title": "C罗在利雅得胜利的最新表现 - 路透社",
            "url": "https://news.google.com/rss/articles/example",
            "snippet": "报道了 Cristiano Ronaldo 最近一场比赛的表现。",
            "source": "路透社",
            "published": "Tue, 01 Sep 2026 08:00:00 GMT",
        }
    ]


def test_current_news_search_prefers_google_news(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_search_google_news_rss",
        lambda *_args, **_kwargs: [
            {
                "title": "Cristiano Ronaldo latest update",
                "url": "https://example.com/ronaldo",
                "snippet": "Current club and match update",
                "source": "Example Sports",
                "published": "Tue, 01 Sep 2026 08:00:00 GMT",
            }
        ],
    )
    monkeypatch.setattr(
        web_search_module,
        "_search_bing_rss",
        lambda *_args, **_kwargs: pytest.fail("Bing should not run after relevant Google News results"),
    )

    result = web_search_module._run({"query": "C罗 近况", "num_results": 5}, None)

    assert result.ok is True
    assert "PROVIDER: Google News RSS" in result.output
    assert "Published: Tue, 01 Sep 2026" in result.output
    assert "PROVIDER_QUERY: Cristiano Ronaldo Al Nassr Portugal football" in result.output
    assert "RELEVANCE_FILTER: strong" in result.output
    assert "Do not repeat equivalent searches" in result.output


def test_subject_terms_keep_short_entity_and_drop_dates_and_intent_words() -> None:
    assert web_search_module._subject_terms("F1 2026 race calendar next race date") == ["f1"]
    assert web_search_module._subject_terms(
        "2026 F1 Italian Grand Prix Monza September 6 race"
    ) == ["f1", "italian", "grand", "prix", "monza"]


def test_relevance_filter_rejects_year_only_and_country_language_drift() -> None:
    year_drift = [
        {
            "title": "2026年日历全年完整图",
            "url": "https://example.com/calendar/2026",
            "snippet": "2026年全年日历与节假日安排",
            "published": "Tue, 01 Sep 2026 08:00:00 GMT",
        },
        {
            "title": "2026年政府工作报告",
            "url": "https://example.com/report",
            "snippet": "2026年工作任务",
            "published": "Tue, 01 Sep 2026 08:00:00 GMT",
        },
    ]
    italian_drift = [
        {
            "title": "Italian language dictionary",
            "url": "https://example.com/italian",
            "snippet": "Learn the Italian language online",
        },
        {
            "title": "Discover Italy",
            "url": "https://example.com/italy",
            "snippet": "Official tourism information for Italy",
        },
    ]

    assert web_search_module._filter_relevant_results(
        "2026 F1 Italian Grand Prix Monza September 6 race",
        year_drift,
    ) == []
    assert web_search_module._filter_relevant_results(
        '"Italian Grand Prix" 2026 Monza race September 6 schedule',
        italian_drift,
    ) == []


def test_schedule_results_rank_fact_bearing_page_above_generic_entity_homepage() -> None:
    results = [
        {
            "title": "F1 - The Official Home of Formula 1 Racing",
            "url": "https://www.formula1.com/",
            "snippet": "Latest F1 news, videos and results",
        },
        {
            "title": "2026赛季F1大奖赛",
            "url": "https://example.com/f1-2026",
            "snippet": "赛程：F1意大利大奖赛正赛 09-06 21:00",
        },
    ]

    filtered = web_search_module._filter_relevant_results(
        "F1 2026 race calendar next race date",
        results,
    )

    assert [item["url"] for item in filtered] == [
        "https://example.com/f1-2026",
        "https://www.formula1.com/",
    ]
    assert filtered[0]["_matched_context_terms"] == "2026, race, calendar"


def test_schedule_search_falls_back_from_drift_and_prefers_official_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_queries: list[str] = []

    def bing(query: str, _limit: int) -> list[dict[str, str]]:
        provider_queries.append(query)
        return [
            {
                "title": "2026 calendar",
                "url": "https://example.com/2026-calendar",
                "snippet": "Printable calendar for 2026",
            }
        ]

    def duckduckgo(query: str, _limit: int) -> list[dict[str, str]]:
        provider_queries.append(query)
        return [
            {
                "title": "F1 Schedule 2026 - Official Grand Prix calendar",
                "url": "https://www.formula1.com/en/racing/2026",
                "snippet": "Formula 1 next race: Italian Grand Prix at Monza, 04-06 September",
            }
        ]

    monkeypatch.setattr(web_search_module, "_search_bing_rss", bing)
    monkeypatch.setattr(web_search_module, "_search_duckduckgo", duckduckgo)

    result = web_search_module._run(
        {"query": "F1 2026 race calendar next race date", "num_results": 6},
        None,
    )

    assert result.ok is True
    assert "PROVIDER: DuckDuckGo HTML" in result.output
    assert "RELEVANCE_FILTER: strong" in result.output
    assert "RELEVANCE_MATCHED_TERMS: f1" in result.output
    assert "2026 calendar" not in result.output
    assert provider_queries == [
        "F1 2026 race calendar next race date official",
        "F1 2026 race calendar next race date official",
    ]


def test_cross_source_dated_news_is_marked_ready_for_summary() -> None:
    results = [
        {
            "title": "Cristiano Ronaldo update",
            "url": "https://example.com/1",
            "snippet": "",
            "source": "Reuters",
            "published": "Tue, 01 Sep 2026 08:00:00 GMT",
        },
        {
            "title": "Ronaldo returns for Al-Nassr",
            "url": "https://example.com/2",
            "snippet": "",
            "source": "ESPN",
            "published": "Mon, 31 Aug 2026 08:00:00 GMT",
        },
        {
            "title": "Portugal captain update",
            "url": "https://example.com/3",
            "snippet": "",
            "source": "USA Today",
            "published": "Sun, 30 Aug 2026 08:00:00 GMT",
        },
    ]

    output = web_search_module._format_results("C罗 最新消息", "Google News RSS", results)

    assert "EVIDENCE_READY: cross-source dated news results" in output


def test_current_news_search_discards_query_drift_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_search_module,
        "_search_google_news_rss",
        lambda *_args, **_kwargs: [
            {"title": "高德地图", "url": "https://amap.com", "snippet": "地图导航"},
        ],
    )
    monkeypatch.setattr(
        web_search_module,
        "_search_bing_rss",
        lambda *_args, **_kwargs: [
            {"title": "C语言入门教程", "url": "https://example.com/c", "snippet": "编程基础"},
        ],
    )
    monkeypatch.setattr(
        web_search_module,
        "_search_duckduckgo",
        lambda *_args, **_kwargs: [
            {
                "title": "Cristiano Ronaldo trains with Al-Nassr",
                "url": "https://example.com/sport",
                "snippet": "Ronaldo returned to team training this week.",
            },
        ],
    )

    result = web_search_module._run({"query": "C罗 最新消息", "num_results": 5}, None)

    assert result.ok is True
    assert "PROVIDER: DuckDuckGo HTML" in result.output
    assert "Cristiano Ronaldo" in result.output
    assert "高德地图" not in result.output
    assert "C语言" not in result.output


def test_web_search_falls_back_to_second_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise web_search_module._SearchProviderError("需要人机验证")

    monkeypatch.setattr(web_search_module, "_search_bing_rss", unavailable)
    monkeypatch.setattr(
        web_search_module,
        "_search_duckduckgo",
        lambda *_args, **_kwargs: [
            {"title": "信阳天气结果", "url": "https://example.com", "snippet": "信阳天气摘要"},
        ],
    )

    result = web_search_module._run({"query": "信阳天气", "num_results": 5}, None)

    assert result.ok is True
    assert "PROVIDER: DuckDuckGo HTML" in result.output
    assert "https://example.com" in result.output


def test_web_search_reports_human_verification_as_provider_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise web_search_module._SearchProviderError("搜索站点要求人机验证")

    monkeypatch.setattr(web_search_module, "_search_bing_rss", unavailable)
    monkeypatch.setattr(web_search_module, "_search_duckduckgo", unavailable)

    result = web_search_module._run({"query": "信阳天气", "num_results": 5}, None)

    assert result.ok is False
    assert result.output.startswith("WEB_SEARCH_UNAVAILABLE")
    assert "人机验证" in result.output
    assert "web_fetch" in result.output


class _FakeResponse:
    status = 200
    headers = {"Content-Type": "text/plain; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        return "响应成功".encode()


class _GoogleNewsShellResponse(_FakeResponse):
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def read(self, _size: int = -1) -> bytes:
        return b"<html><body>Google News</body></html>"


class _StaticOpener:
    def __init__(self, response) -> None:
        self.response = response

    def open(self, *_args, **_kwargs):
        return self.response


def test_web_fetch_percent_encodes_unicode_query_before_request() -> None:
    request = web_fetch_module._request(
        "https://news.google.com/rss/search?q=C罗 近况&hl=zh-CN"
    )

    assert request.full_url.isascii()
    assert "C%E7%BD%97%20%E8%BF%91%E5%86%B5" in request.full_url


def test_web_fetch_rejects_empty_google_news_article_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_fetch_module.urllib.request,
        "build_opener",
        lambda *_args: _StaticOpener(_GoogleNewsShellResponse()),
    )

    result = web_fetch_module._run(
        {"url": "https://news.google.com/rss/articles/example", "max_chars": 1000},
        None,
    )

    assert result.ok is False
    assert result.output.startswith("WEB_FETCH_EMPTY_CONTENT")
    assert "不要重复读取" in result.output


class _RetryingOpener:
    def __init__(self, *, always_fail: bool = False) -> None:
        self.calls = 0
        self.always_fail = always_fail

    def open(self, *_args, **_kwargs):
        self.calls += 1
        if self.always_fail or self.calls == 1:
            raise urllib.error.URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"))
        return _FakeResponse()


def test_web_fetch_retries_one_transient_tls_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _RetryingOpener()
    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_args: opener)

    result = web_fetch_module._run({"url": "https://example.com/data", "max_chars": 1000}, None)

    assert result.ok is True
    assert opener.calls == 2
    assert "ATTEMPTS: 2" in result.output
    assert "响应成功" in result.output


def test_web_fetch_tls_failure_explains_target_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _RetryingOpener(always_fail=True)
    monkeypatch.setattr(web_fetch_module.urllib.request, "build_opener", lambda *_args: opener)

    result = web_fetch_module._run({"url": "https://example.com/data", "max_chars": 1000}, None)

    assert result.ok is False
    assert opener.calls == 2
    assert result.output.startswith("WEB_FETCH_TLS_ERROR")
    assert "不代表 web_fetch 工具不存在" in result.output


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://172.31.1.2/private",
        "http://[fc00::1]/private",
        "http://2130706433/private",
        "http://localhost./private",
    ],
)
def test_web_fetch_rejects_non_public_numeric_and_local_targets(url: str) -> None:
    safe, reason = web_fetch_module._is_safe_url(url)

    assert safe is False
    assert "internal" in reason


def test_web_fetch_revalidates_dns_result_before_socket_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_fetch_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80))
        ],
    )

    with pytest.raises(OSError, match="internal addresses blocked"):
        web_fetch_module._validated_address_infos("rebind.example", 80)


def test_web_fetch_bounds_response_read_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    class OversizedResponse(_FakeResponse):
        headers = {"Content-Type": "text/plain; charset=utf-8", "Content-Length": "999999"}

        def __init__(self) -> None:
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return b"x" * size

    response = OversizedResponse()
    monkeypatch.setattr(
        web_fetch_module.urllib.request,
        "build_opener",
        lambda *_args: _StaticOpener(response),
    )

    result = web_fetch_module._run({"url": "https://example.com/data", "max_chars": 1000}, None)

    assert result.ok is True
    assert response.read_sizes == [4001]
    assert "TRUNCATED: yes" in result.output
