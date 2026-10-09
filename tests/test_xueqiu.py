"""AKShare 雪球适配只使用 fake DataFrame。"""

from types import SimpleNamespace

import pandas as pd
import pytest
from akshare.exceptions import APIError, NetworkError

from app.providers.xueqiu import XueqiuProvider, XueqiuSourceError


def frame(rows):
    return pd.DataFrame(rows, columns=["item", "value"])


def test_akshare_quote_and_profile_pass_explicit_token_timeout_and_map_fields():
    calls = []

    def spot(**kwargs):
        calls.append(("spot", kwargs))
        return frame([
            ("现价", 10.2), ("涨幅", 1.2), ("成交额", 500000),
            ("时间", "2026-09-28 15:00:01"), ("最低", 10), ("最高", 10.5),
            ("今开", 10.1), ("涨停", 11.2), ("跌停", 9.2),
            ("均价", 10.25), ("成交量", 10000),
            ("昨收", 10.0),
            ("资产净值/总市值", 123456789),
        ])

    def basic(**kwargs):
        calls.append(("basic", kwargs))
        return frame([
            ("affiliate_industry.ind_name", "银行"), ("listed_date", "1999-11-10")
        ])

    source = XueqiuProvider("private-token", 13, api=SimpleNamespace(
        stock_individual_spot_xq=spot, stock_individual_basic_info_xq=basic,
    ))
    assert source.quote("SH600000") == {
        "current": 10.2, "percent": 1.2, "amount": 500000,
        "time": "2026-09-28 15:00:01", "low": 10, "high": 10.5,
        "open": 10.1, "limit_up": 11.2, "limit_down": 9.2,
        "avg_price": 10.25, "volume": 10000,
        "previous_close": 10.0,
        "market_capital": 123456789,
    }
    assert source.profile("SH600000") == {"industry": "银行", "list_date": "1999-11-10"}
    assert calls == [
        ("spot", {"symbol": "SH600000", "token": "private-token", "timeout": (5, 13)}),
        ("basic", {"symbol": "SH600000", "token": "private-token", "timeout": (5, 13)}),
    ]
    assert "private-token" not in repr(source)


def test_akshare_missing_rows_remain_null():
    source = XueqiuProvider("private-token", api=SimpleNamespace(
        stock_individual_spot_xq=lambda **_kwargs: frame([("时间", "2026-09-28 15:00:01")]),
        stock_individual_basic_info_xq=lambda **_kwargs: frame([("name", "浦发银行")]),
    ))
    assert source.quote("SH600000")["low"] is None
    assert source.profile("SH600000") == {"industry": None, "list_date": None}


def test_empty_akshare_frame_is_a_source_error():
    source = XueqiuProvider("private-token", api=SimpleNamespace(
        stock_individual_basic_info_xq=lambda **_kwargs: frame([]),
    ))
    with pytest.raises(XueqiuSourceError):
        source.profile("SH600000")


@pytest.mark.parametrize("status", [401, 403, 429])
def test_akshare_rejection_starts_cooldown(status):
    def fail(**_kwargs):
        raise APIError("upstream rejected", status_code=status)

    source = XueqiuProvider("private-token", api=SimpleNamespace(stock_individual_spot_xq=fail))
    with pytest.raises(XueqiuSourceError) as error:
        source.quote("SH600000")
    assert error.value.cooldown


def test_akshare_network_error_is_classified_without_cooldown():
    def fail(**_kwargs):
        raise NetworkError("request failed")

    source = XueqiuProvider("private-token", api=SimpleNamespace(stock_individual_spot_xq=fail))
    with pytest.raises(XueqiuSourceError) as error:
        source.quote("SH600000")
    assert not error.value.cooldown


def test_akshare_token_error_without_http_status_starts_cooldown():
    def fail(**_kwargs):
        raise APIError("requires a valid xq_a_token", status_code=None)

    source = XueqiuProvider("private-token", api=SimpleNamespace(stock_individual_spot_xq=fail))
    with pytest.raises(XueqiuSourceError) as error:
        source.quote("SH600000")
    assert error.value.cooldown


def test_invalid_akshare_frame_is_rejected():
    source = XueqiuProvider("private-token", api=SimpleNamespace(
        stock_individual_spot_xq=lambda **_kwargs: pd.DataFrame({"unknown": [1]})
    ))
    with pytest.raises(XueqiuSourceError):
        source.quote("SH600000")
