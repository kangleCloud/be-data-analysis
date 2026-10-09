"""ETF 同步接口的鉴权、数据边界与总闸。"""

import pytest
from app.resources import SourceResourceError
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
                                   etf_factory=lambda: source, redis_factory=RedisClient))
    response = client.post("/internal/etf-monitor/v1/asset-allocation", headers=HEADERS,
                           json={"symbol": "SH510050", "reportPeriod": "20260630"})
    assert response.status_code == 503
    assert source.calls == []


def test_asset_allocation_only_reports_asset_categories():
    source = Source()
    settings = load_settings({"STOCK_MONITOR_INTERNAL_TOKEN": "service-secret",
                              "STOCK_MONITOR_XQ_ENABLED": "true", "XUEQIU_TOKEN": "test"})
    client = TestClient(create_app(scheduler_enabled=False, settings=settings,
                                   etf_factory=lambda: source, redis_factory=RedisClient))
    response = client.post("/internal/etf-monitor/v1/asset-allocation", headers=HEADERS,
                           json={"symbol": "SH510050", "reportPeriod": "20260630"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["categories"] == [{"category": "股票", "percent": 95.2}]
    assert payload["requestedReportPeriod"] == "2026-06-30"
    assert "constituents" not in payload
    assert source.calls == [("allocation", "510050", "20260630")]


def test_dictionary_uses_today_cache_and_refreshes_previous_day():
    import json
    from datetime import datetime,timedelta
    from zoneinfo import ZoneInfo
    from app.etf_dictionary import KEY,save_dictionary
    source,backend=Source(),RedisClient()
    settings=load_settings({'STOCK_MONITOR_INTERNAL_TOKEN':'service-secret'})
    client=TestClient(create_app(scheduler_enabled=False,settings=settings,etf_factory=lambda:source,redis_factory=lambda:backend))
    save_dictionary(backend,[{'代码':'sh510050','名称':'缓存50ETF','最新价':'2.5'},
                             {'代码':'sz159919','名称':'缓存300ETF','最新价':'4'}],datetime.now(ZoneInfo('Asia/Shanghai')))
    response=client.post('/internal/etf-monitor/v1/dictionary',headers=HEADERS)
    assert response.status_code==200 and len(response.json()['etfs'])==2 and source.calls==[]
    cached=json.loads(backend.get(KEY))
    cached['collectedAt']=(datetime.now(ZoneInfo('Asia/Shanghai'))-timedelta(days=1)).isoformat()
    backend.set(KEY,json.dumps(cached),ex=86400)
    assert client.post('/internal/etf-monitor/v1/dictionary',headers=HEADERS).status_code==200
    assert source.calls==['quotes']


@pytest.mark.parametrize('error,status,reason',[
    ([],502,'NO_DATA'),([{'资产类型':'股票','仓位占比':None}],502,'NO_DATA'),
    ([{'changed':'field'}],502,'SOURCE'),(KeyError('changed'),502,'SOURCE'),
    (SourceResourceError('PROCESS_EXIT',exitcode=-9),503,'RESOURCE'),
])
def test_allocation_reason_is_frozen_and_does_not_scan_periods(error,status,reason):
    calls=[]
    class Allocation:
        def asset_allocation(self,code,period):
            calls.append((code,period))
            if isinstance(error,Exception):
                raise error
            return error
    settings=load_settings({'STOCK_MONITOR_INTERNAL_TOKEN':'service-secret','STOCK_MONITOR_XQ_ENABLED':'true','XUEQIU_TOKEN':'offline'})
    client=TestClient(create_app(scheduler_enabled=False,settings=settings,etf_factory=Allocation,redis_factory=RedisClient))
    response=client.post('/internal/etf-monitor/v1/asset-allocation',headers=HEADERS,json={'symbol':'SH510050','reportPeriod':'20260630'})
    assert response.status_code==status and response.json()['detail']['reason']==reason
    assert calls==[('510050','20260630')]


def test_allocation_shared_busy_entry_does_not_call_source():
    from app.collection_gate import ENTRY_KEY
    backend,source=RedisClient(),Source()
    backend.set(ENTRY_KEY,'auto',ex=30)
    settings=load_settings({'STOCK_MONITOR_INTERNAL_TOKEN':'service-secret','STOCK_MONITOR_XQ_ENABLED':'true','XUEQIU_TOKEN':'offline'})
    client=TestClient(create_app(scheduler_enabled=False,settings=settings,etf_factory=lambda:source,redis_factory=lambda:backend))
    response=client.post('/internal/etf-monitor/v1/asset-allocation',headers=HEADERS,json={'symbol':'SH510050','reportPeriod':'20260630'})
    assert response.status_code==409 and source.calls==[]
