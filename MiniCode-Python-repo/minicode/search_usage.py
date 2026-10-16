"""Provider quota observations, stored without credentials and shared by workers."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager

from minicode import config
from minicode.web_transport import WebRequestError, WebResponse, request_response

REFRESH_INTERVAL_MS = 60_000
STALE_AFTER_MS = 300_000
_MESSAGES = {
    "authentication": "密钥认证失败，请检查或更换密钥。",
    "rate_limited": "服务暂时限流，请稍后查询；限流不代表额度已用完。",
    "timeout": "查询超时，请稍后重试。",
    "network": "无法连接搜索服务，请检查网络后重试。",
    "invalid_response": "服务返回了无法识别的用量数据。",
    "redirect": "服务重定向了认证请求，已停止查询。",
}


def _now() -> int:
    return int(time.time() * 1000)


def _identity(service: str, key: str) -> str:
    return hashlib.sha256((service + "\0" + key).encode()).hexdigest()


def _path():
    return config.MINI_CODE_DIR / "search-usage.sqlite3"


@contextmanager
def _database():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=0.2)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS usage_cache (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS usage_refresh (id TEXT PRIMARY KEY, next_at INTEGER NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS usage_activity (service TEXT PRIMARY KEY, at INTEGER NOT NULL)")
        with connection:
            yield connection
    finally:
        connection.close()


def _empty(service: str) -> dict:
    return {"status": "unknown", "source": "search_headers" if service == "brave" else "usage_api",
            "checkedAt": None, "attemptedAt": None, "stale": False, "errorCode": "", "metrics": [],
            "nextRefreshAt": None, "message": "尚未获取额度。"}


def cached_usage(service: str, key: str) -> dict:
    view = _empty(service)
    if not _path().exists():
        return view
    try:
        with _database() as database:
            identity = _identity(service, key)
            row = database.execute("SELECT payload FROM usage_cache WHERE id = ?", (identity,)).fetchone()
            if row:
                view.update(json.loads(row[0]))
            refresh = database.execute("SELECT next_at FROM usage_refresh WHERE id = ?", (identity,)).fetchone()
            view["nextRefreshAt"] = refresh[0] if refresh else None
            activity = database.execute("SELECT at FROM usage_activity WHERE service = ?", (service,)).fetchone()
        checked, now = view["checkedAt"], _now()
        view["stale"] = bool(view["status"] == "error" or (checked and (
            now - checked > STALE_AFTER_MS or (activity and activity[0] > checked)
            or any(item.get("resetAt") is not None and item["resetAt"] <= now for item in view["metrics"]))))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        view = {**_empty(service), "status": "error", "errorCode": "cache",
                "message": "暂时无法读取本机额度记录，请稍后重试。"}
    return view


def _store(service: str, key: str, observation: dict, *, preserve: bool = False) -> None:
    try:
        with _database() as database:
            database.execute("BEGIN IMMEDIATE")
            identity = _identity(service, key)
            previous = database.execute("SELECT payload FROM usage_cache WHERE id = ?", (identity,)).fetchone()
            snapshot = _empty(service)
            if preserve and previous:
                snapshot.update(json.loads(previous[0]))
            snapshot.update(observation)
            database.execute("INSERT OR REPLACE INTO usage_cache VALUES (?, ?)",
                             (identity, json.dumps(snapshot, ensure_ascii=False)))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # Quota bookkeeping must never turn a successful search into a failure.
        return


def mark_usage_stale(service: str) -> None:
    # Account quotas may be shared by several keys. Do not invent a local balance
    # by subtracting one from each key; mark previous observations as historical.
    try:
        with _database() as database:
            database.execute("INSERT OR REPLACE INTO usage_activity VALUES (?, ?)", (service, _now()))
    except (OSError, sqlite3.Error):
        return


def _number(value):
    return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value) and 0 <= value <= 10**15) else None


def parse_tavily_usage(data: dict, *, checked_at: int) -> dict:
    metrics = []
    for identifier, label, group, used_key, limit_key in (
        ("plan", "账户套餐", "account", "plan_usage", "plan_limit"),
        ("key", "当前密钥", "key", "usage", "limit"),
        ("paygo", "账户按量付费", "account", "paygo_usage", "paygo_limit"),
    ):
        section = data.get(group)
        if not isinstance(section, dict):
            continue
        used, limit = _number(section.get(used_key)), _number(section.get(limit_key))
        if used is None and limit is None:
            continue
        metrics.append({"id": identifier, "label": label, "unit": "credits", "used": used, "limit": limit,
                        "remaining": max(0, limit - used) if limit is not None and used is not None else None,
                        "unlimited": False, "resetAt": None})
    return {"status": "ok" if metrics else "unavailable", "checkedAt": checked_at, "attemptedAt": checked_at,
            "errorCode": "", "metrics": metrics,
            "message": "" if metrics else "服务没有返回可用的额度字段，剩余额度未知。"}


def parse_brave_usage(headers: dict, *, checked_at: int) -> dict:
    """Keep daily-or-longer quota windows separate from burst rate limits."""
    headers = {name.lower(): value for name, value in headers.items()}
    result = {"status": "unavailable", "checkedAt": checked_at, "attemptedAt": checked_at,
              "errorCode": "", "metrics": [], "message": "服务未返回完整的周期配额，剩余次数未知。"}
    try:
        parts = [str(headers.get(name, "")).split(",") for name in (
            "x-ratelimit-policy", "x-ratelimit-limit", "x-ratelimit-remaining")]
        policies, limits, remaining = parts
        if not (0 < len(policies) == len(limits) == len(remaining) <= 8):
            return result
        resets = str(headers.get("x-ratelimit-reset", "")).split(",")
        metrics = []
        for index, policy in enumerate(policies):
            match = re.fullmatch(r"\s*(\d+)\s*;\s*w=(\d+)\s*", policy)
            if match is None or not limits[index].strip().isdigit() or not remaining[index].strip().isdigit():
                return result
            policy_limit, window = map(int, match.groups())
            limit, left = int(limits[index]), int(remaining[index])
            if policy_limit != limit or not 0 < window <= 315_360_000 or max(limit, left) > 10**15 or (limit and left > limit):
                return result
            if window < 86400:
                continue
            reset = int(resets[index].strip()) if len(resets) == len(policies) and resets[index].strip().isdigit() else None
            if reset is not None and reset > 315_360_000:
                reset = None
            label = f"{window // 86400} 天配额" if window % 86400 == 0 else "周期配额"
            metrics.append({"id": f"window-{window}", "label": label, "unit": "requests", "limit": limit,
                            "remaining": left if limit else None, "used": max(0, limit - left) if limit else None,
                            "unlimited": limit == 0, "resetAt": checked_at + reset * 1000 if reset is not None else None})
        if metrics:
            result.update(status="ok", metrics=metrics, message="")
        else:
            result["message"] = "服务仅返回短期限流信息，无法确定剩余搜索次数。"
    except (TypeError, ValueError, OverflowError):
        pass
    return result


def _failure(service: str, key: str, error: WebRequestError) -> None:
    _store(service, key, {"status": "error", "attemptedAt": _now(), "stale": True,
                         "errorCode": error.code if error.code in _MESSAGES else "request_failed",
                         "message": _MESSAGES.get(error.code, "暂时无法查询额度，请稍后重试。")}, preserve=True)


def request_brave_with_usage(url: str, key: str) -> WebResponse:
    try:
        response = request_response(url, headers={"Accept": "application/json", "X-Subscription-Token": key}, timeout=12)
    except WebRequestError as error:
        if error.status == 429:
            observation = parse_brave_usage(error.headers, checked_at=_now())
            if observation["status"] == "ok":
                observation["message"] = "请求受到限流；下列周期配额来自服务响应。"
                _store("brave", key, observation)
            else:
                _failure("brave", key, error)
        elif error.code != "canceled":
            _failure("brave", key, error)
        raise
    mark_usage_stale("brave")
    _store("brave", key, parse_brave_usage(response.headers, checked_at=_now()))
    return response


def refresh_usage(service: str, key: str) -> dict:
    now = _now()
    with _database() as database:
        database.execute("BEGIN IMMEDIATE")
        identity = _identity(service, key)
        row = database.execute("SELECT next_at FROM usage_refresh WHERE id = ?", (identity,)).fetchone()
        claimed = row is None or row[0] <= now
        if claimed:
            database.execute("INSERT OR REPLACE INTO usage_refresh VALUES (?, ?)", (identity, now + REFRESH_INTERVAL_MS))
    if not claimed:
        return cached_usage(service, key)
    try:
        if service == "brave":
            # Explicit UI action only. Normal searches collect the same headers
            # without an additional request; opening settings never calls this.
            request_brave_with_usage("https://api.search.brave.com/res/v1/web/search?q=Brave+Search&count=1", key)
        else:
            response = request_response("https://api.tavily.com/usage", headers={"Authorization": "Bearer " + key}, timeout=12)
            try:
                data = json.loads(response.text)
            except ValueError as error:
                raise WebRequestError("invalid_response", "Invalid usage response.") from error
            if not isinstance(data, dict):
                raise WebRequestError("invalid_response", "Invalid usage response.")
            _store(service, key, parse_tavily_usage(data, checked_at=_now()))
    except WebRequestError as error:
        if service != "brave":  # Brave already captured success/error headers.
            _failure(service, key, error)
    return cached_usage(service, key)
