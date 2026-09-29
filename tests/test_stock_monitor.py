"""个股采样、去重、缺口、锁与源冷却。"""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.providers.xueqiu import XueqiuSourceError
from app.stock_monitor import (
    ENABLED_KEY, LAST_TRADE_DATE_KEY, QUOTE_PREFIX, SERIES_PREFIX,
    MonitorStore, StockMonitorSampler, normalize_quote,
)
from tests.test_snapshot import FakeRedis


SHANGHAI = ZoneInfo("Asia/Shanghai")
AT = datetime(2026, 9, 28, 9, 32, tzinfo=SHANGHAI)
STOCKS = [
    {"symbol": "SH600000", "code": "600000", "name": "浦发银行", "market": "SH"},
    {"symbol": "SZ000001", "code": "000001", "name": "平安银行", "market": "SZ"},
]


class Pipeline:
    def __init__(self, client):
        self.client = client
        self.commands = []

    def set(self, key, value):
        self.commands.append((key, value))
        return self

    def execute(self):
        for key, value in self.commands:
            self.client.set(key, value)


class RedisClient(FakeRedis):
    def pipeline(self, transaction=True):
        assert transaction
        return Pipeline(self)

    def scan_iter(self, match):
        prefix = match[:-1]
        return (key for key in list(self.values) if key.startswith(prefix))

    def delete(self, key):
        return self.values.pop(key, None) is not None


class Calendar:
    def latest_trading_date(self, today):
        return today


class QuoteSource:
    def __init__(self):
        self.calls = []
        self.time = int(AT.timestamp() * 1000)
        self.fail = False

    def quote(self, symbol):
        self.calls.append(symbol)
        if self.fail:
            raise XueqiuSourceError("限流", cooldown=True)
        return {"time": self.time, "current": 10.2, "percent": 1.2, "amount": 500000}


def test_quote_normalizes_source_timestamp_and_null_amount():
    quote = normalize_quote("SH600000", {
        "time": int(AT.timestamp() * 1000), "current": "10.2",
        "percent": None, "amount": None,
    }, AT)
    assert quote["sourceTime"] == "2026-09-28T09:32:00+08:00"
    assert quote["tradeDate"] == "2026-09-28"
    assert quote["price"] == 10.2
    assert quote["changePercent"] is None
    assert quote["amount"] is None


def test_sampler_writes_quote_and_real_points_without_duplicate_or_gap_fill(monkeypatch):
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS))
    source = QuoteSource()
    monkeypatch.setattr("app.stock_monitor.time.sleep", lambda _seconds: None)
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    assert sampler.sample(AT) == "published"
    key = f"{SERIES_PREFIX}2026-09-28:SH600000"
    assert json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))["status"] == "FRESH"
    assert len(json.loads(client.get(key))) == 1
    assert sampler.sample(AT.replace(minute=34)) == "published"
    assert len(json.loads(client.get(key))) == 1
    source.time = int(AT.replace(hour=13, minute=2).timestamp() * 1000)
    assert sampler.sample(AT.replace(hour=13, minute=2)) == "published"
    points = json.loads(client.get(key))
    assert [point["time"] for point in points] == [
        "2026-09-28T09:32:00+08:00", "2026-09-28T13:02:00+08:00"
    ]
    assert client.get(LAST_TRADE_DATE_KEY) == "2026-09-28"
    source.time = int(AT.timestamp() * 1000)
    assert sampler.sample(AT.replace(hour=13, minute=4)) == "partial"
    assert len(json.loads(client.get(key))) == 2
    assert json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))["status"] == "STALE"


def test_cooldown_stops_remaining_symbols_and_keeps_stale_data(monkeypatch):
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS))
    source = QuoteSource()
    monkeypatch.setattr("app.stock_monitor.time.sleep", lambda _seconds: None)
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    sampler.sample(AT)
    source.calls.clear()
    source.fail = True
    assert sampler.sample(AT.replace(minute=34)) == "partial"
    assert source.calls == ["SH600000"]
    for stock in STOCKS:
        quote = json.loads(client.get(f"{QUOTE_PREFIX}{stock['symbol']}"))
        assert quote["status"] == "STALE"
        assert quote["price"] == 10.2
    source.calls.clear()
    assert sampler.sample(AT.replace(minute=36)) == "cooldown"
    assert source.calls == []
    client.advance(2 * 60 * 60)
    source.fail = False
    assert sampler.sample(AT.replace(minute=38)) == "published"
    assert len(json.loads(client.get(f"{SERIES_PREFIX}2026-09-28:SH600000"))) == 1
    assert json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))["status"] == "FRESH"


def test_lock_holiday_and_enabled_limit_block_source_calls():
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS))
    source = QuoteSource()
    store = MonitorStore(client)
    first = store.acquire()
    assert StockMonitorSampler(store, source, Calendar(), xq_enabled=True).sample(AT) == "locked"
    assert source.calls == []
    store.release(first)

    class Holiday:
        def latest_trading_date(self, _today):
            return date(2026, 9, 25)

    assert StockMonitorSampler(store, source, Holiday(), xq_enabled=True).sample(AT) == "skipped"
    assert source.calls == []
    client.set(ENABLED_KEY, json.dumps(STOCKS * 6))
    try:
        store.enabled()
    except ValueError as error:
        assert "清单" in str(error)
    else:
        raise AssertionError("超过 10 只应被拒绝")


def test_new_trade_day_removes_old_series_only():
    client = RedisClient()
    store = MonitorStore(client)
    client.set(f"{SERIES_PREFIX}2026-09-25:SH600000", "[]")
    client.set(f"{SERIES_PREFIX}2026-09-28:SH600000", "[]")
    store.prepare_trade_date("2026-09-28")
    assert client.get(f"{SERIES_PREFIX}2026-09-25:SH600000") is None
    assert client.get(f"{SERIES_PREFIX}2026-09-28:SH600000") == "[]"


def test_sampler_default_off_does_not_touch_redis_calendar_or_xueqiu():
    class Forbidden:
        def __getattr__(self, _name):
            raise AssertionError("关闭总闸时不得访问外部依赖")

    assert StockMonitorSampler(Forbidden(), Forbidden(), Forbidden()).sample(AT) == "disabled"
