"""内部接口鉴权、开关与跨服务 JSON 契约。"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.main import create_app
from tests.test_snapshot import FakeRedis


class RedisClient(FakeRedis):
    def close(self):
        pass


def test_exchange_dictionary_is_independent_of_xueqiu_switch():
    calls = []

    class Exchange:
        def all_a_stocks(self):
            calls.append("exchange")
            return [{"symbol": "SH600000", "code": "600000", "name": "浦发银行", "market": "SH"}]

    settings = load_settings({"STOCK_MONITOR_INTERNAL_TOKEN": "service-secret"})
    app = create_app(
        scheduler_enabled=False, settings=settings,
        exchange_factory=Exchange,
        xq_factory=lambda: calls.append("xq"), redis_factory=RedisClient,
    )
    client = TestClient(app)
    path = "/internal/stock-monitor/v1/exchange-dictionary"
    assert client.post(path, json={}).status_code == 401
    response = client.post(path, json={}, headers={"X-Internal-Token": "service-secret"})
    assert response.status_code == 200
    assert response.json() == {"schemaVersion": 1, "stocks": [
        {"symbol": "SH600000", "code": "600000", "name": "浦发银行", "market": "SH"}
    ]}
    assert calls == ["exchange"]


def test_disabled_profiles_reject_before_xueqiu_or_redis():
    calls = []
    settings = load_settings({
        "STOCK_MONITOR_INTERNAL_TOKEN": "service-secret",
        "XUEQIU_TOKEN": "some-token",
    })
    app = create_app(
        scheduler_enabled=False, settings=settings,
        xq_factory=lambda: calls.append("xq"),
        redis_factory=lambda: calls.append("redis"),
    )
    response = TestClient(app).post(
        "/internal/stock-monitor/v1/profiles",
        json={"symbols": ["SH600000"]},
        headers={"X-Internal-Token": "service-secret"},
    )
    assert response.status_code == 503
    assert calls == []


def test_enabled_profiles_return_only_limited_basic_fields(monkeypatch):
    calls = []
    monkeypatch.setattr("app.stock_monitor_api.time.sleep", lambda _seconds: None)

    class Xueqiu:
        def profile(self, symbol):
            calls.append(("profile", symbol))
            return {"industry": "银行", "list_date": "1999-11-10", "secret": "ignored"}

        def quote(self, symbol):
            calls.append(("quote", symbol))
            return {"market_capital": 123456789, "current": 10.2}

    settings = load_settings({
        "STOCK_MONITOR_INTERNAL_TOKEN": "service-secret",
        "STOCK_MONITOR_XQ_ENABLED": "true",
        "XUEQIU_TOKEN": "some-token",
    })
    app = create_app(
        scheduler_enabled=False, settings=settings,
        xq_factory=Xueqiu, redis_factory=RedisClient,
    )
    client = TestClient(app)
    path = "/internal/stock-monitor/v1/profiles"
    headers = {"X-Internal-Token": "service-secret"}
    assert client.post(path, json={"symbols": ["SH600000"] * 11}, headers=headers).status_code == 422
    response = client.post(path, json={"symbols": ["SH600000"]}, headers=headers)
    assert response.status_code == 200
    profile = response.json()["profiles"][0]
    assert response.json()["schemaVersion"] == 1
    assert profile["symbol"] == "SH600000"
    assert profile["industry"] == "银行"
    assert profile["listingDate"] == "1999-11-10"
    assert profile["marketCap"] == 123456789
    assert profile["updatedAt"].endswith("+08:00")
    assert set(profile) == {"symbol", "industry", "listingDate", "marketCap", "updatedAt"}
    assert calls == [("profile", "SH600000"), ("quote", "SH600000")]


def test_profiles_keep_request_order_and_reject_entire_batch_on_one_failure():
    import threading
    class Source:
        def profile(self, symbol):
            return {'industry':'银行', 'list_date':19991110}
        def quote(self, symbol):
            if fail and symbol == 'SZ000001':
                raise ValueError('无效资料')
            return {'market_capital':100}
    second, fail = threading.Event(), False
    settings = load_settings({'STOCK_MONITOR_INTERNAL_TOKEN':'service-secret',
                              'STOCK_MONITOR_XQ_ENABLED':'true','XUEQIU_TOKEN':'fake'})
    client = TestClient(create_app(scheduler_enabled=False, settings=settings,
                                 xq_factory=Source, redis_factory=RedisClient))
    kwargs = dict(json={'symbols':['SH600000','SZ000001']}, headers={'X-Internal-Token':'service-secret'})
    response = client.post('/internal/stock-monitor/v1/profiles', **kwargs)
    assert response.status_code == 200
    assert [item['symbol'] for item in response.json()['profiles']] == ['SH600000','SZ000001']
    fail = True
    response = client.post('/internal/stock-monitor/v1/profiles', **kwargs)
    assert response.status_code == 502
    assert 'profiles' not in response.json()


@pytest.mark.parametrize('age,full,expected',[(0,True,[]),(120,True,[]),(121,True,['profile','quote']),(0,False,['profile'])])
def test_profiles_reuse_only_fresh_same_symbol_quote_fields(age,full,expected):
    import json
    from datetime import datetime,timedelta
    from zoneinfo import ZoneInfo
    from app.stock_monitor import QUOTE_PREFIX
    backend,calls=RedisClient(),[]
    quote={'symbol':'SH600000','source':'XQ','status':'FRESH','marketCap':100,
           'collectedAt':(datetime.now(ZoneInfo('Asia/Shanghai'))-timedelta(seconds=age-.5)).isoformat()}
    if age==0:
        quote['collectedAt']=(datetime.now(ZoneInfo('Asia/Shanghai'))-timedelta(seconds=.1)).isoformat()
    if full:
        quote.update(industry='银行',listingDate='1999-11-10')
    backend.set(QUOTE_PREFIX+'SH600000',json.dumps(quote))
    class Source:
        def profile(self,symbol):
            calls.append('profile')
            return {'industry':'银行','list_date':'1999-11-10'}
        def quote(self,symbol):
            calls.append('quote')
            return {'market_capital':200}
    settings=load_settings({'STOCK_MONITOR_INTERNAL_TOKEN':'service-secret','STOCK_MONITOR_XQ_ENABLED':'true','XUEQIU_TOKEN':'offline'})
    response=TestClient(create_app(scheduler_enabled=False,settings=settings,xq_factory=Source,redis_factory=lambda:backend)).post('/internal/stock-monitor/v1/profiles',headers={'X-Internal-Token':'service-secret'},json={'symbols':['SH600000']})
    assert response.status_code==200 and calls==expected
    assert response.json()['profiles'][0]['marketCap']==(200 if age>120 else 100)


@pytest.mark.parametrize('field,value',[('source','SINA'),('symbol','SZ000001'),('status','STALE'),('marketCap',float('nan'))])
def test_profile_quote_reuse_rejects_wrong_source_symbol_status_or_capital(field,value):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.stock_monitor_api import fresh_profile_quote
    now=datetime.now(ZoneInfo('Asia/Shanghai'))
    quote={'symbol':'SH600000','source':'XQ','status':'FRESH','marketCap':100,'collectedAt':now.isoformat()}
    quote[field]=value
    assert fresh_profile_quote(quote,'SH600000',now) is None
