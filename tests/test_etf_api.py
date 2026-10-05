"""ETF 同步接口的鉴权、数据边界与总闸。"""

from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.main import create_app
from tests.test_snapshot import FakeRedis

HEADERS = {"X-Internal-Token": "service-secret"}


class RedisClient(FakeRedis):
    def close(self):
        pass


class Source:
    def __init__(self):
        self.calls = []

    def quotes(self):
        self.calls.append("quotes")
        return [{"代码": "sh510050", "名称": "50ETF", "最新价": "2.5"}]

    def profile(self, code, *, budget_seconds):
        self.calls.append(("ths", code))
        assert 0 < budget_seconds <= 180
        return [{"字段": "基金代码", "值": code},
                {"字段": "基金全称", "值": "上证50交易型开放式指数基金"},
                {"字段": "成立日期", "值": "2004-12-30"}]

    def asset_allocation(self, code, period):
        self.calls.append(("allocation", code, period))
        return [{"资产类型": "股票", "仓位占比": "95.2"}]


def test_etf_dictionary_and_profiles_use_audited_fields():
    source = Source()
    settings = load_settings({"STOCK_MONITOR_INTERNAL_TOKEN": "service-secret"})
    client = TestClient(create_app(scheduler_enabled=False, settings=settings,
                                   etf_factory=lambda: source, redis_factory=RedisClient))
    assert client.post("/internal/etf-monitor/v1/dictionary").status_code == 401
    response = client.post("/internal/etf-monitor/v1/dictionary", headers=HEADERS)
    assert response.status_code == 200
    etf = response.json()["etfs"][0]
    assert etf["symbol"] == "SH510050"
    assert etf["listingStatus"] is None
    assert etf["trackingIndexCode"] is None
    response = client.post("/internal/etf-monitor/v1/profiles", headers=HEADERS,
                           json={"symbols": ["SH510050"]})
    assert response.status_code == 200
    profile = response.json()["profiles"][0]
    assert profile["fullName"] == "上证50交易型开放式指数基金"
    assert profile["establishedDate"] == "2004-12-30"
    assert profile["source"] == "THS"
    assert profile["collectedAt"].endswith("+08:00")
    assert response.json()["sourceStatus"] == {"SH510050": "OK"}
    assert not {"listingDate", "listingStatus", "shareCount", "shareDate"} & profile.keys()
    assert "price" not in profile
    assert source.calls == ["quotes", ("ths", "510050")]


def test_asset_allocation_disabled_gate_does_not_call_source():
    source = Source()
    settings = load_settings({"STOCK_MONITOR_INTERNAL_TOKEN": "service-secret"})
    client = TestClient(create_app(scheduler_enabled=False, settings=settings,
                                   etf_factory=lambda: source))
    response = client.post("/internal/etf-monitor/v1/asset-allocation", headers=HEADERS,
                           json={"symbol": "SH510050", "reportPeriod": "20260630"})
    assert response.status_code == 503
    assert source.calls == []


def test_asset_allocation_only_reports_asset_categories():
    source = Source()
    settings = load_settings({"STOCK_MONITOR_INTERNAL_TOKEN": "service-secret",
                              "STOCK_MONITOR_XQ_ENABLED": "true", "XUEQIU_TOKEN": "test"})
    client = TestClient(create_app(scheduler_enabled=False, settings=settings,
                                   etf_factory=lambda: source))
    response = client.post("/internal/etf-monitor/v1/asset-allocation", headers=HEADERS,
                           json={"symbol": "SH510050", "reportPeriod": "20260630"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["categories"] == [{"category": "股票", "percent": 95.2}]
    assert payload["requestedReportPeriod"] == "2026-06-30"
    assert "constituents" not in payload
    assert source.calls == [("allocation", "510050", "20260630")]
