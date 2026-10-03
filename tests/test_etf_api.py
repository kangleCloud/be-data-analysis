"""ETF 同步接口的鉴权、数据边界与总闸。"""

from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.main import create_app

HEADERS = {"X-Internal-Token": "service-secret"}


class Source:
    def __init__(self):
        self.calls = []

    def quotes(self):
        self.calls.append("quotes")
        return [{"代码": "sh510050", "名称": "50ETF", "最新价": "2.5"}]

    def sse_scale(self, date):
        self.calls.append(("sse", date))
        return [{"基金代码": "510050", "基金简称": "50ETF", "ETF类型": "股票型",
                 "统计日期": "2026-09-30", "基金份额": 120000}]

    def szse_scale(self):
        self.calls.append("szse")
        return []

    def asset_allocation(self, code, period):
        self.calls.append(("allocation", code, period))
        return [{"资产类型": "股票", "仓位占比": "95.2"}]


def test_etf_dictionary_and_profiles_use_audited_fields():
    source = Source()
    settings = load_settings({"STOCK_MONITOR_INTERNAL_TOKEN": "service-secret"})
    client = TestClient(create_app(scheduler_enabled=False, settings=settings,
                                   etf_factory=lambda: source))
    assert client.post("/internal/etf-monitor/v1/dictionary").status_code == 401
    response = client.post("/internal/etf-monitor/v1/dictionary", headers=HEADERS)
    assert response.status_code == 200
    etf = response.json()["etfs"][0]
    assert etf["symbol"] == "SH510050"
    assert etf["listingStatus"] is None
    assert etf["trackingIndexCode"] is None
    response = client.post("/internal/etf-monitor/v1/profiles", headers=HEADERS,
                           json={"symbols": ["SH510050"], "asOfDate": "20260930"})
    assert response.status_code == 200
    profile = response.json()["profiles"][0]
    assert profile["shareCount"] == 120000
    assert "price" not in profile
    assert source.calls == ["quotes", ("sse", "20260930"), "szse"]


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
