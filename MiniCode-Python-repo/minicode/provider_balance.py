"""Read the official DeepSeek account balance without exposing credentials."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx


class BalanceQueryError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_official_deepseek_url(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url.strip())
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api.deepseek.com"
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not (parsed.params or parsed.query or parsed.fragment)
            and parsed.path.rstrip("/").removesuffix("/chat/completions") in {"", "/v1", "/beta"}
        )
    except ValueError:
        return False


def parse_deepseek_balance(payload: Any) -> dict[str, Any]:
    """Keep decimal strings intact and return only the documented balance fields."""
    invalid = "DeepSeek 返回了无效的余额数据，请稍后刷新。"
    if not isinstance(payload, dict) or type(payload.get("is_available")) is not bool:
        raise BalanceQueryError(invalid)
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        raise BalanceQueryError(invalid)
    balances = []
    for info in infos:
        if not isinstance(info, dict) or info.get("currency") not in ("CNY", "USD"):
            raise BalanceQueryError(invalid)
        entry = {"currency": info["currency"]}
        for source, target in (
            ("total_balance", "totalBalance"),
            ("granted_balance", "grantedBalance"),
            ("topped_up_balance", "toppedUpBalance"),
        ):
            value = info.get(source)
            if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
                raise BalanceQueryError(invalid)
            entry[target] = value
        balances.append(entry)
    return {"provider": "deepseek", "isAvailable": payload["is_available"], "balances": balances}


def query_deepseek_balance(api_key: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            response = client.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
    except httpx.TimeoutException:
        raise BalanceQueryError("查询 DeepSeek 余额超时，请稍后刷新。", 504) from None
    except httpx.HTTPError:
        raise BalanceQueryError("无法连接 DeepSeek 查询余额，请检查网络后重试。") from None
    # Provider error bodies and exception strings can echo credentials.
    if response.status_code == 401:
        raise BalanceQueryError("DeepSeek API Key 无效，请检查并保存密钥后重试。")
    if response.status_code == 429:
        raise BalanceQueryError("DeepSeek 余额查询过于频繁，请稍后刷新。", 429)
    if response.status_code != 200:
        raise BalanceQueryError(f"DeepSeek 余额查询失败（HTTP {response.status_code}），请稍后刷新。")
    try:
        result = parse_deepseek_balance(response.json())
    except ValueError:
        raise BalanceQueryError("DeepSeek 返回了无效的余额数据，请稍后刷新。") from None
    return {**result, "checkedAt": int(time.time() * 1000)}
