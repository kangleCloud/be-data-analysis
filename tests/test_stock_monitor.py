"""个股采样、去重、缺口、锁与源冷却。"""

import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from app.providers.xueqiu import XueqiuProvider, XueqiuSourceError
from app.stock_monitor import (
    ENABLED_KEY, LAST_TRADE_DATE_KEY, QUOTE_PREFIX, SERIES_PREFIX,
    MonitorStore, QuoteValidationError, StockMonitorSampler, normalize_profile,
    normalize_quote,
)
from app.trading_calendar import CACHE_KEY, CalendarService, normalize_dates
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
    def day_status(self, _today, _at):
        return True


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
    assert all(quote[field] is None for field in (
        "low", "high", "open", "limitUp", "limitDown", "averagePrice", "volume", "previousClose"
    ))


def test_quote_maps_optional_price_and_volume_fields():
    quote = normalize_quote("SH600000", {
        "time": "2026-09-28 15:00:01", "current": 10.2,
        "low": "9.8", "high": "10.5", "open": "10.0",
        "limit_up": "11", "limit_down": "9", "avg_price": "10.1",
        "volume": "12345", "previous_close": "10.05", "amount": None,
    }, AT)
    assert quote["sourceTime"] == "2026-09-28T15:00:01+08:00"
    assert {field: quote[field] for field in (
        "low", "high", "open", "limitUp", "limitDown", "averagePrice", "volume", "previousClose"
    )} == {
        "low": 9.8, "high": 10.5, "open": 10.0,
        "limitUp": 11.0, "limitDown": 9.0, "averagePrice": 10.1,
        "volume": 12345.0,
        "previousClose": 10.05,
    }


def test_profile_accepts_akshare_numeric_listing_date_and_quote_capital():
    profile = normalize_profile(
        "SH600000", {"industry": "银行", "list_date": 19991110},
        {"market_capital": 123456789}, AT,
    )
    assert profile["listingDate"] == "1999-11-10"
    assert profile["marketCap"] == 123456789


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
    client.advance(120)
    assert sampler.sample(AT.replace(minute=34)) == "published"
    assert len(json.loads(client.get(key))) == 1
    source.time = int(AT.replace(hour=13, minute=2).timestamp() * 1000)
    client.advance(120)
    assert sampler.sample(AT.replace(hour=13, minute=2)) == "published"
    points = json.loads(client.get(key))
    assert [point["time"] for point in points] == [
        "2026-09-28T09:32:00+08:00", "2026-09-28T13:02:00+08:00"
    ]
    assert client.get(LAST_TRADE_DATE_KEY) == "2026-09-28"
    source.time = int(AT.timestamp() * 1000)
    client.advance(120)
    assert sampler.sample(AT.replace(hour=13, minute=4)) == "published"
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
    client.advance(120)
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
    assert json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))["status"] == "STALE"


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
        def day_status(self, _today, _at):
            return False

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


def test_close_retries_require_source_time_strictly_after_1500_and_stop_after_confirmation(monkeypatch):
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS[:1]))
    source = QuoteSource()
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    monkeypatch.setattr("app.stock_monitor.time.sleep", lambda _seconds: None)
    source.time = int(AT.replace(hour=15, minute=0).timestamp() * 1000)
    assert sampler.sample(AT.replace(hour=15, minute=0)) == "published"
    key = f"{SERIES_PREFIX}2026-09-28:SH600000"
    assert len(json.loads(client.get(key))) == 1
    client.advance(120)
    assert sampler.sample(AT.replace(hour=15, minute=2)) == "published"
    assert len(json.loads(client.get(key))) == 1
    assert json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))["status"] == "STALE"
    source.time += 1000
    client.advance(120)
    assert sampler.sample(AT.replace(hour=15, minute=4)) == "published"
    points = json.loads(client.get(key))
    assert len(points) == 2
    assert points[-1]["time"] == "2026-09-28T15:00:01+08:00"
    assert json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))["status"] == "FRESH"
    source.calls.clear()
    assert sampler.sample(AT.replace(hour=15, minute=6)) == "published"
    assert source.calls == []
    assert len(json.loads(client.get(key))) == 2


def test_close_retries_throttle_same_symbol_and_expire_at_1510(monkeypatch):
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS[:1]))
    source = QuoteSource()
    source.time = int(AT.replace(hour=15, minute=0).timestamp() * 1000)
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    monkeypatch.setattr("app.stock_monitor.time.sleep", lambda _seconds: None)
    assert sampler.sample(AT.replace(hour=15, minute=2)) == "published"
    assert sampler.sample(AT.replace(hour=15, minute=2, second=30)) == "published"
    assert source.calls == ["SH600000"]
    for minute in (4, 6, 8, 10):
        client.advance(120)
        assert sampler.sample(AT.replace(hour=15, minute=minute)) == "published"
    assert len(source.calls) == 5
    quote = json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))
    assert quote["status"] == "STALE"
    assert client.get(f"{SERIES_PREFIX}2026-09-28:SH600000") is None
    assert sampler.sample(AT.replace(hour=15, minute=12)) == "skipped"
    assert len(source.calls) == 5


def test_close_cooldown_preserves_history_without_network(monkeypatch):
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS[:1]))
    source = QuoteSource()
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    monkeypatch.setattr("app.stock_monitor.time.sleep", lambda _seconds: None)
    assert sampler.sample(AT) == "published"
    key = f"{SERIES_PREFIX}2026-09-28:SH600000"
    client.set("stock:monitor:v1:xq:cooldown", "1", ex=7200)
    source.calls.clear()
    assert sampler.sample(AT.replace(hour=15, minute=10)) == "cooldown"
    assert source.calls == []
    assert json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))["status"] == "STALE"
    assert len(json.loads(client.get(key))) == 1


def test_close_rejects_previous_trade_date_and_preserves_quote():
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS[:1]))
    source = QuoteSource()
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    assert sampler.sample(AT) == "published"
    source.time = int(AT.replace(day=25, hour=15, minute=3).timestamp() * 1000)
    client.advance(120)
    assert sampler.sample(AT.replace(hour=15, minute=2)) == "published"
    quote = json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))
    assert quote["sourceTime"] == "2026-09-28T09:32:00+08:00"
    assert quote["status"] == "STALE"
    assert len(json.loads(client.get(f"{SERIES_PREFIX}2026-09-28:SH600000"))) == 1


def test_close_holiday_and_empty_list_do_not_request_source():
    client = RedisClient()
    source = QuoteSource()
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    assert sampler.sample(AT.replace(hour=15, minute=2)) == "skipped"
    client.set(ENABLED_KEY, json.dumps(STOCKS[:1]))

    class Holiday:
        def day_status(self, _today, _at):
            return False

    assert StockMonitorSampler(MonitorStore(client), source, Holiday(), xq_enabled=True).sample(
        AT.replace(hour=15, minute=2)
    ) == "skipped"
    assert source.calls == []


def test_unknown_calendar_does_not_request_xueqiu():
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS[:1]))
    source = QuoteSource()

    class UnknownCalendar:
        def day_status(self, _today, _at):
            return None

    sampler = StockMonitorSampler(
        MonitorStore(client), source, UnknownCalendar(), xq_enabled=True,
    )
    assert sampler.sample(AT) == "skipped"
    assert source.calls == []


def test_october_first_cached_holiday_blocks_all_four_xueqiu_requests():
    at = datetime(2026, 10, 1, 14, 30, tzinfo=SHANGHAI)
    symbols = ("SH601991", "SH600410", "SH601179", "SH600036")
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps([
        {"symbol": symbol, "code": symbol[2:], "name": symbol, "market": "SH"}
        for symbol in symbols
    ]))
    client.set(CACHE_KEY, json.dumps(normalize_dates([
        "2026-01-05", "2026-09-30", "2026-10-08", "2026-12-31",
    ], at.replace(hour=0, minute=10))))

    class NoRefresh:
        def dates(self):
            raise AssertionError("有效缓存不应刷新")

    class NoQuote:
        def quote(self, _symbol):
            raise AssertionError("休市日不得请求雪球")

    calendar = CalendarService(client, NoRefresh())
    assert calendar.day_status(at.date(), at) is False
    sampler = StockMonitorSampler(MonitorStore(client), NoQuote(), calendar, xq_enabled=True)
    assert sampler.sample(at) == "skipped"
    assert client.get(LAST_TRADE_DATE_KEY) is None


def test_four_symbols_with_akshare_dataframe_previous_day_are_stale_without_points(monkeypatch, caplog):
    at = datetime(2026, 10, 1, 14, 30, tzinfo=SHANGHAI)
    symbols = ("SH601991", "SH600410", "SH601179", "SH600036")
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps([
        {"symbol": symbol, "code": symbol[2:], "name": symbol, "market": "SH"}
        for symbol in symbols
    ]))
    calls = []

    def spot(**kwargs):
        calls.append(kwargs["symbol"])
        return pd.DataFrame([
            ("时间", "2026-09-30 15:00:00"),
            ("现价", 10.2), ("涨幅", 1.2), ("成交额", 500000),
        ], columns=["item", "value"])

    source = XueqiuProvider("fake-token", api=SimpleNamespace(stock_individual_spot_xq=spot))
    monkeypatch.setattr("app.stock_monitor.time.sleep", lambda _seconds: None)
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    with caplog.at_level("INFO"):
        assert sampler.sample(at) == "published"
    assert calls == list(symbols)
    assert caplog.text.count("原因 source_previous_date") == 4
    assert "fake-token" not in caplog.text
    for symbol in symbols:
        quote = json.loads(client.get(f"{QUOTE_PREFIX}{symbol}"))
        assert quote["status"] == "STALE"
        assert quote["sourceTime"] == "2026-09-30T15:00:00+08:00"
        assert client.get(f"{SERIES_PREFIX}2026-10-01:{symbol}") is None


def test_new_trading_day_delayed_quote_keeps_history_until_current_day_point():
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS[:1]))
    source = QuoteSource()
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    assert sampler.sample(AT) == "published"
    old_key = f"{SERIES_PREFIX}2026-09-28:SH600000"
    old_series = client.get(old_key)
    next_day = AT.replace(day=29)
    client.advance(120)
    assert sampler.sample(next_day) == "published"
    quote = json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))
    assert quote["status"] == "STALE"
    assert quote["sourceTime"] == "2026-09-28T09:32:00+08:00"
    assert client.get(old_key) == old_series
    assert client.get(f"{SERIES_PREFIX}2026-09-29:SH600000") is None
    source.time = int(next_day.replace(minute=34).timestamp() * 1000)
    client.advance(120)
    assert sampler.sample(next_day.replace(minute=34)) == "published"
    assert client.get(old_key) is None
    assert len(json.loads(client.get(f"{SERIES_PREFIX}2026-09-29:SH600000"))) == 1
    assert json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))["status"] == "FRESH"


def test_invalid_quote_logs_safe_reason_and_preserves_previous_point(caplog):
    client = RedisClient()
    client.set(ENABLED_KEY, json.dumps(STOCKS[:1]))
    source = QuoteSource()
    sampler = StockMonitorSampler(MonitorStore(client), source, Calendar(), xq_enabled=True)
    assert sampler.sample(AT) == "published"
    key = f"{SERIES_PREFIX}2026-09-28:SH600000"
    points = client.get(key)
    client.advance(120)
    source.quote = lambda _symbol: {
        "time": "invalid", "current": "bad", "cookie": "private-cookie"
    }
    with caplog.at_level("WARNING"):
        assert sampler.sample(AT.replace(minute=34)) == "partial"
    assert "原因 invalid_source_time，字段 time" in caplog.text
    assert "private-cookie" not in caplog.text
    assert client.get(key) == points
    assert json.loads(client.get(f"{QUOTE_PREFIX}SH600000"))["status"] == "STALE"
    try:
        normalize_quote("SH600000", {"time": "invalid", "current": 1}, AT)
    except QuoteValidationError as exc:
        assert (exc.reason, exc.field) == ("invalid_source_time", "time")
    else:
        raise AssertionError("无效时间应被拒绝")
