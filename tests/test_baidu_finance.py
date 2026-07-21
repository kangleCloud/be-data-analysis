"""百度财经客户端、Provider 和解析器测试。"""

from datetime import date
from decimal import Decimal

import pytest
import httpx
from pydantic import SecretStr

from app.models.domain import AssetType
from app.providers.baidu_finance.client import BaiduFinanceClient
from app.providers.baidu_finance.processing import parse_daily_kline
from app.providers.baidu_finance.provider import BaiduFinanceProvider
from app.providers.base import ProviderError

KLINE_KEYS = [
    "time",
    "open",
    "close",
    "volume",
    "high",
    "low",
    "amount",
    "range",
    "ratio",
    "turnoverratio",
    "preClose",
    "ma5avgprice",
    "ma5volume",
    "ma10avgprice",
    "ma10volume",
    "ma20avgprice",
    "ma20volume",
]


def _payload(rows: str, result_code=0):
    return {
        "ResultCode": result_code,
        "Result": {"newMarketData": {"keys": KLINE_KEYS, "marketData": rows}},
    }


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "upstream detail",
                request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.cookies = httpx.Cookies()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_parse_kline_maps_dynamic_fields_and_deduplicates():
    rows = ";".join(
        [
            "2024-01-03,10.1,10.3,120,10.5,10.0,1236,0.2,1.98,2.1,10.1,10.0,100,9.9,90,--,--",
            "broken,row",
            "2024-01-02,10.0,10.1,100,10.2,9.9,1005,0.1,1.0,2.0,10.0,--,--,--,--,--,--",
            "2024-01-03,10.2,10.4,130,10.6,10.1,1300,0.3,2.97,2.2,10.1,10.1,110,10.0,95,9.8,80",
        ]
    )

    items = parse_daily_kline(_payload(rows))

    assert [item.trade_date for item in items] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert items[0].ma5 is None
    assert items[1].close == Decimal("10.4")
    assert items[1].change_percent == Decimal("2.97")
    assert items[1].ma20_volume == 80


def test_parse_kline_accepts_empty_result_and_rejects_all_bad_rows():
    assert parse_daily_kline(_payload("")) == ()
    with pytest.raises(ProviderError, match="无法解析"):
        parse_daily_kline(_payload("broken,row"))


@pytest.mark.parametrize("result_code", [0, "0"])
def test_client_accepts_integer_and_string_success_code(result_code):
    client_session = _Client(_Response(_payload("", result_code=result_code)))
    client = BaiduFinanceClient(
        "https://finance.pae.baidu.com",
        timeout_seconds=10,
        retry_count=0,
        client=client_session,
    )

    payload = client.fetch_daily_kline(AssetType.STOCK, "603339")

    assert payload["ResultCode"] == result_code
    assert client_session.calls[0][1]["params"]["code"] == "603339"
    assert client_session.calls[0][1]["follow_redirects"] is False


def test_client_cookie_is_optional_and_limited_to_baidu_host():
    no_cookie_client = _Client(_Response(_payload("")))
    secret_client = _Client(_Response(_payload("")))
    custom_host_client = _Client(_Response(_payload("")))

    BaiduFinanceClient(
        "https://finance.pae.baidu.com",
        10,
        0,
        client=no_cookie_client,
    )
    BaiduFinanceClient(
        "https://finance.pae.baidu.com",
        10,
        0,
        ab_sr=SecretStr("test-sensitive-cookie"),
        client=secret_client,
    )
    BaiduFinanceClient(
        "https://example.test",
        10,
        0,
        ab_sr=SecretStr("test-sensitive-cookie"),
        client=custom_host_client,
    )

    assert dict(no_cookie_client.cookies) == {}
    assert dict(secret_client.cookies) == {"ab_sr": "test-sensitive-cookie"}
    assert dict(custom_host_client.cookies) == {}


def test_client_maps_http_and_business_failures_to_provider_error():
    http_client = BaiduFinanceClient(
        "https://finance.pae.baidu.com",
        10,
        0,
        client=_Client(_Response({}, status_code=404)),
    )
    business_client = BaiduFinanceClient(
        "https://finance.pae.baidu.com",
        10,
        0,
        client=_Client(_Response({"ResultCode": "1"})),
    )

    with pytest.raises(ProviderError, match="请求失败"):
        http_client.fetch_daily_kline(AssetType.STOCK, "603339")
    with pytest.raises(ProviderError, match="失败状态"):
        business_client.fetch_daily_kline(AssetType.STOCK, "603339")


def test_provider_filters_requested_date_range():
    rows = ";".join(
        [
            "2024-01-02,10,10,100,11,9,1000,--,--,--,--,--,--,--,--,--,--",
            "2024-01-03,11,11,110,12,10,1100,--,--,--,--,--,--,--,--,--,--",
        ]
    )
    client = BaiduFinanceClient(
        "https://finance.pae.baidu.com",
        10,
        0,
        client=_Client(_Response(_payload(rows))),
    )
    provider = BaiduFinanceProvider(client)

    items = provider.fetch_history(
        AssetType.FUND,
        "510300",
        date(2024, 1, 3),
        date(2024, 1, 3),
    )

    assert [item.trade_date for item in items] == [date(2024, 1, 3)]
