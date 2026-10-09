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

    provider = AkShareMarketProvider(2, api=SimpleNamespace())
    provider._akshare = SimpleNamespace(
        stock_fund_flow_industry=capture("ths_industry"),
        stock_fund_flow_concept=capture("ths_concept"),
        stock_fund_flow_individual=capture("ths_individual"),
    )
    provider.sector_fund_flow("industry")
    provider.sector_fund_flow("concept")
    provider.market_fund_flow()
    assert calls == [
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


def test_shared_executor_preserves_deadlines_parameters_and_cooldown_keys():
    calls = []
    executor = SimpleNamespace(call=lambda call: calls.append(call) or pd.DataFrame())
    provider = AkShareMarketProvider(15, executor=executor)
    provider.market_fund_flow()
    provider.sector_fund_flow("industry")
    provider.sector_fund_flow("concept")
    provider.index_spot()
    assert [call.budget_seconds for call in calls] == [900,120,120,120]
    assert [call.group for call in calls] == ["ths","ths","ths","sina"]
    assert all(call.parameters == {"symbol":"即时"} for call in calls[:3])
    assert calls[-1].parameters == {}


def test_cooling_provider_logs_only_info_with_ttl(caplog):
    from app.source_execution import SourceCoolingError
    def cool(call):
        raise SourceCoolingError(300)
    provider = AkShareMarketProvider(15, executor=SimpleNamespace(call=cool))
    with caplog.at_level("INFO"), pytest.raises(SourceCoolingError):
        provider.market_fund_flow()
    assert "剩余 TTL 300 秒" in caplog.text
    assert all(record.levelno < 30 for record in caplog.records)
