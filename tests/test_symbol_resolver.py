"""东方财富股票名称解析器测试。"""

import pytest
import httpx

from app.providers.base import AmbiguousSymbolError, ProviderError, SymbolNotFoundError
from app.providers.eastmoney_symbol.resolver import EastmoneyStockSymbolResolver


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self.response = _Response(payload)
        self.headers = {}
        self.cookies = httpx.Cookies()
        self.call_count = 0
        self.last_kwargs = None

    def get(self, url, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        return self.response


def _payload(data, status=0):
    return {"QuotationCodeTable": {"Status": status, "Data": data}}


def _resolver(payload, client=None):
    resolved_client = client or _Client(payload)
    return (
        EastmoneyStockSymbolResolver(
            "https://searchapi.eastmoney.com",
            timeout_seconds=10,
            retry_count=0,
            cache_ttl_seconds=60,
            client=resolved_client,
        ),
        resolved_client,
    )


def test_resolver_exactly_matches_fourtech_and_caches_result():
    resolver, client = _resolver(
        _payload(
            [
                {"Code": "603339", "Name": "四方科技", "SecurityTypeName": "沪A"},
                {"Code": "00339", "Name": "四方科技", "SecurityTypeName": "港股"},
            ]
        )
    )

    first = resolver.resolve("四方科技")
    second = resolver.resolve(" 四方科技 ")

    assert first == second
    assert first.symbol == "603339"
    assert first.exchange == "沪A"
    assert client.call_count == 1
    assert dict(client.cookies) == {}
    assert client.last_kwargs["follow_redirects"] is False


def test_resolver_reports_not_found_and_ambiguous_names():
    missing, _ = _resolver(_payload([]))
    ambiguous, _ = _resolver(
        _payload(
            [
                {"Code": "600001", "Name": "同名股票", "SecurityTypeName": "沪A"},
                {"Code": "000001", "Name": "同名股票", "SecurityTypeName": "深A"},
            ]
        )
    )

    with pytest.raises(SymbolNotFoundError):
        missing.resolve("不存在")
    with pytest.raises(AmbiguousSymbolError):
        ambiguous.resolve("同名股票")


def test_resolver_rejects_failed_business_status():
    resolver, _ = _resolver(_payload([], status=1))

    with pytest.raises(ProviderError, match="失败状态"):
        resolver.resolve("四方科技")
