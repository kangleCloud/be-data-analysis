"""AKShare 接口选择和交易日历适配。"""

from datetime import date
from http.client import RemoteDisconnected
from types import SimpleNamespace
from contextlib import contextmanager

import pandas as pd
import pytest
import requests

from app.providers.akshare_market import AkShareMarketProvider


def test_adapter_uses_expected_akshare_functions():
    calls = []

    def capture(name):
        def invoke(**kwargs):
            calls.append((name, kwargs))
            return pd.DataFrame([{"trade_date": date(2026, 9, 23)}])
        return invoke

    provider = AkShareMarketProvider(2)
    provider._akshare = SimpleNamespace(
        tool_trade_date_hist_sina=capture("calendar"),
        stock_fund_flow_industry=capture("ths_industry"),
        stock_fund_flow_concept=capture("ths_concept"),
        stock_fund_flow_individual=capture("ths_individual"),
    )
    assert provider.latest_trading_date(date(2026, 9, 23)) == date(2026, 9, 23)
    provider.sector_fund_flow("industry")
    provider.sector_fund_flow("concept")
    provider.market_fund_flow()
    assert calls == [
        ("calendar", {}),
        ("ths_industry", {"symbol": "即时"}),
        ("ths_concept", {"symbol": "即时"}),
        ("ths_individual", {"symbol": "即时"}),
    ]


def test_failed_interface_is_not_retried():
    provider = AkShareMarketProvider(15)
    calls = []

    def fail(**_kwargs):
        calls.append(1)
        raise requests.ConnectionError("断连")

    provider._akshare = SimpleNamespace(stock_fund_flow_individual=fail)
    with pytest.raises(requests.ConnectionError):
        provider.market_fund_flow()
    assert calls == [1]


def test_connection_failure_logs_root_type_without_request_details(caplog):
    provider = AkShareMarketProvider(15)

    def fail(**_kwargs):
        try:
            raise RemoteDisconnected("sensitive request details")
        except RemoteDisconnected as cause:
            raise requests.ConnectionError("sensitive request details") from cause

    provider._akshare = SimpleNamespace(stock_fund_flow_individual=fail)
    with pytest.raises(requests.ConnectionError):
        provider.market_fund_flow()
    assert "ConnectionError" in caplog.text
    assert "RemoteDisconnected" in caplog.text
    assert "sensitive request details" not in caplog.text


def test_ths_long_pagination_deadlines(monkeypatch):
    deadlines = []

    @contextmanager
    def fake_deadline(seconds):
        deadlines.append(seconds)
        yield

    monkeypatch.setattr("app.providers.akshare_market._deadline", fake_deadline)
    provider = AkShareMarketProvider(15)
    provider._akshare = SimpleNamespace(
        stock_fund_flow_industry=lambda **_kwargs: pd.DataFrame(),
        stock_fund_flow_individual=lambda **_kwargs: pd.DataFrame(),
    )
    provider.sector_fund_flow("industry")
    provider.market_fund_flow()
    assert deadlines == [120, 900]


def test_ths_pagination_requests_are_spaced_in_collect_process(monkeypatch):
    virtual_time = [0.0]
    sleeps = []
    requests_seen = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        virtual_time[0] += seconds

    def fake_get(url, **kwargs):
        requests_seen.append((url, virtual_time[0], kwargs.get("timeout")))
        return object()

    monkeypatch.setattr("app.providers.akshare_market.time.monotonic", lambda: virtual_time[0])
    monkeypatch.setattr("app.providers.akshare_market.time.sleep", fake_sleep)
    monkeypatch.setattr(requests, "get", fake_get)

    def fetch(**_kwargs):
        requests.get("http://data.10jqka.com.cn/funds/hyzjl/page/1")
        requests.get("http://data.10jqka.com.cn/funds/hyzjl/page/2")
        return pd.DataFrame()

    provider = AkShareMarketProvider(15)
    provider._akshare = SimpleNamespace(stock_fund_flow_industry=fetch)
    provider.sector_fund_flow("industry")
    assert [(at, timeout) for _url, at, timeout in requests_seen] == [(0.0, 15), (1.0, 15)]
    assert sleeps == [1.0]
    assert requests.get is fake_get
