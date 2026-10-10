"""ETF 采样与 Redis 发布的离线回归。"""

from app.runtime.cooldown import risk_key, ordinary_key

import json
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.etf_monitor.collector import (
    ENABLED_KEY, PRICE_PREFIX, SNAPSHOT_KEY, UPDATES_CHANNEL, EtfCollector, EtfStore,
)
from app.core.config import load_settings
from app.runtime.workflows import run_etf
from tests.test_snapshot import FakeRedis

AT = datetime(2026, 9, 28, 9, 32, tzinfo=ZoneInfo("Asia/Shanghai"))


class Calendar:
    def __init__(self, status=True):
        self.status = status

    def day_status(self, _day, _at):
        return self.status


class Quotes:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.rows = [{"代码": "sh510050", "名称": "50ETF", "最新价": "2.50",
                      "涨跌幅": "1.0", "成交额": "1000"}]

    def quotes(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("源不可用")
        return self.rows


def _setup():
    client = FakeRedis()
    client.set(ENABLED_KEY, json.dumps([{"symbol": "SH510050", "code": "510050",
                                         "name": "50ETF", "market": "SH"}]))
    return client, Quotes()


def test_etf_price_is_trading_quote_and_event_links_state_ids():
    client, source = _setup()
    collector = EtfCollector(source, EtfStore(client), Calendar())
    assert collector.collect(AT) == "published"
    snapshot = json.loads(client.get(SNAPSHOT_KEY))
    item = snapshot["items"][0]
    assert item["quote"]["price"] == 2.5
    assert item["quote"]["source"] == "SINA_ETF"
    assert item["quote"]["sourceTime"] is None
    assert item["fundSeries"] == []
    assert item["fundFlowStatus"] == "NO_RELIABLE_SOURCE"
    assert len(json.loads(client.get(f"{PRICE_PREFIX}2026-09-28:SH510050"))) == 1
    events = [json.loads(value) for action, key, value in client.events
              if action == "publish" and key == UPDATES_CHANNEL]
    assert events[-1] == {"baseStateId": None, "stateId": snapshot["stateId"],
                          "changedSymbols": ["SH510050"]}

    client.advance(120)
    assert collector.collect(AT.replace(minute=34)) == "published"
    next_snapshot = json.loads(client.get(SNAPSHOT_KEY))
    events = [json.loads(value) for action, key, value in client.events
              if action == "publish" and key == UPDATES_CHANNEL]
    assert events[-1]["baseStateId"] == snapshot["stateId"]
    assert events[-1]["stateId"] == next_snapshot["stateId"]
    assert len(next_snapshot["items"][0]["priceSeries"]) == 2


def test_etf_source_failure_preserves_previous_quote_without_new_point():
    client, source = _setup()
    collector = EtfCollector(source, EtfStore(client), Calendar())
    assert collector.collect(AT) == "published"
    source.fail = True
    client.advance(120)
    assert collector.collect(AT.replace(minute=34)) == "partial"
    item = json.loads(client.get(SNAPSHOT_KEY))["items"][0]
    assert item["quote"]["status"] == "STALE"
    assert item["quote"]["price"] == 2.5
    assert len(item["priceSeries"]) == 1
    client.advance(120)
    assert collector.collect(AT.replace(minute=36)) == "cooldown"
    assert source.calls == 2


def test_etf_lock_interval_holiday_and_disabled_list_do_not_call_source():
    client, source = _setup()
    store = EtfStore(client)
    token = store.acquire()
    assert EtfCollector(source, store, Calendar()).collect(AT) == "locked"
    store.release(token)
    assert EtfCollector(source, store, Calendar(False)).collect(AT) == "skipped"
    client.advance(120)
    client.set(ENABLED_KEY, "[]")
    assert EtfCollector(source, store, Calendar()).collect(AT) == "skipped"
    assert source.calls == 0


def test_etf_partial_symbol_does_not_invent_price():
    client, source = _setup()
    client.set(ENABLED_KEY, json.dumps([
        {"symbol": "SH510050", "code": "510050", "name": "50ETF", "market": "SH"},
        {"symbol": "SZ159919", "code": "159919", "name": "300ETF", "market": "SZ"},
    ]))
    assert EtfCollector(source, EtfStore(client), Calendar()).collect(AT) == "partial"
    items = json.loads(client.get(SNAPSHOT_KEY))["items"]
    assert [item["quote"]["status"] if item["quote"] else "ERROR" for item in items] == [
        "FRESH", "ERROR",
    ]
    assert items[1]["quote"] is None


def test_etf_workflow_collects_sina_quotes_with_xueqiu_gate_disabled(monkeypatch):
    client, source = _setup()
    monkeypatch.setattr("app.runtime.workflows.AkShareEtfProvider", lambda _timeout, **_kwargs: source)
    monkeypatch.setattr("app.runtime.workflows.calendar_service", lambda _settings, _client, **kwargs: Calendar())
    settings = load_settings({"STOCK_MONITOR_XQ_ENABLED": "false"})
    assert run_etf(settings, client, at=AT) == "published"
    assert source.calls == 1
    item = json.loads(client.get(SNAPSHOT_KEY))["items"][0]
    assert item["quote"]["source"] == "SINA_ETF"
    assert item["quote"]["price"] == 2.5
    assert item["priceSeries"]


@pytest.mark.parametrize("kind,seconds", [
    ("403", 7200), ("429", 7200), ("network", 300), ("timeout", 300),
    ("format", 300), ("normalize", 300),
])
def test_etf_cooldown_classification_skip_and_recovery(monkeypatch, caplog, kind, seconds):
    from app.etf_monitor.collector import COOLDOWN_KEY
    from app.providers.akshare_etf import EtfSourceError
    client, source = _setup()
    collector = EtfCollector(source, EtfStore(client), Calendar())
    assert collector.collect(AT) == "published"
    original = source.quotes
    def fail():
        source.calls += 1
        if kind == "normalize":
            return [{"代码": "sh510050", "最新价": "-"}]
        if kind in {"403", "429"}:
            raise EtfSourceError({"exception_type": "HTTPError", "root_type": "HTTPError",
                                  "http_status": int(kind), "category": "HTTP_REJECTED"})
        raise {"network": ConnectionError, "timeout": TimeoutError,
               "format": AttributeError}[kind]("private-source-token")
    monkeypatch.setattr(source, "quotes", fail)
    client.advance(120)
    assert collector.collect(AT.replace(minute=34)) == "partial"
    cooling_key = risk_key("sina") if kind in {"403", "429"} else ordinary_key(COOLDOWN_KEY)
    assert client.ttl(cooling_key) == seconds
    assert "private-source-token" not in caplog.text
    assert "分类" in caplog.text
    before = source.calls
    client.advance(120)
    caplog.clear()
    with caplog.at_level("INFO"):
        assert collector.collect(AT.replace(minute=36)) == "cooldown"
    assert source.calls == before and client.ttl(cooling_key) == seconds - 120
    assert "冷却跳过，剩余 TTL" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= 30]
    item = json.loads(client.get(SNAPSHOT_KEY))["items"][0]
    assert item["quote"]["status"] == "STALE"
    assert len(item["priceSeries"]) == 1
    monkeypatch.setattr(source, "quotes", original)
    client.advance(seconds - 120)
    assert collector.collect(AT.replace(hour=12, minute=0)) == "skipped"
    # 普通失败可在同日盘中恢复，长冷却在后续盘中恢复。
    assert collector.collect(AT.replace(hour=13, minute=0)) == "published"
    item = json.loads(client.get(SNAPSHOT_KEY))["items"][0]
    assert item["quote"]["status"] == "FRESH" and len(item["priceSeries"]) == 2


def test_etf_collected_at_reflects_source_completion(monkeypatch):
    client, source = _setup()
    clock = [0]
    monkeypatch.setattr("app.etf_monitor.collector.time.monotonic", lambda: clock[0])
    original = source.quotes
    def slow():
        clock[0] += 20
        return original()
    monkeypatch.setattr(source, "quotes", slow)
    assert EtfCollector(source, EtfStore(client), Calendar()).collect(AT) == "published"
    item = json.loads(client.get(SNAPSHOT_KEY))["items"][0]
    assert item["quote"]["collectedAt"] == "2026-09-28T09:32:20+08:00"


@pytest.mark.parametrize("wrapped", [False, True])
def test_etf_unexpected_program_error_keeps_traceback(monkeypatch, caplog, wrapped):
    from app.providers.akshare_etf import EtfSourceError
    client, source = _setup()
    collector = EtfCollector(source, EtfStore(client), Calendar())
    assert collector.collect(AT) == "published"
    private_value = "private-raw-response-or-password"
    def bug():
        if wrapped:
            raise EtfSourceError({"exception_type": "RuntimeError", "root_type": "RuntimeError",
                                  "http_status": None, "category": "UNEXPECTED"})
        raise RuntimeError(private_value)
    monkeypatch.setattr(source, "quotes", bug)
    client.advance(120)
    assert collector.collect(AT.replace(minute=34)) == "partial"
    record = next(record for record in caplog.records if "非预期程序异常" in record.message)
    assert record.levelname == "ERROR" and record.exc_info is not None
    assert "Traceback" in caplog.text and "in bug" in caplog.text
    assert "private-raw-response-or-password" not in caplog.text
    item = json.loads(client.get(SNAPSHOT_KEY))["items"][0]
    assert item["quote"]["status"] == "STALE" and len(item["priceSeries"]) == 1


def test_mid_call_cooldown_is_info_and_keeps_original_ttl(monkeypatch, caplog):
    from app.runtime.source_execution import SourceCoolingError
    from app.etf_monitor.collector import COOLDOWN_KEY
    client, source = _setup()
    collector = EtfCollector(source,EtfStore(client),Calendar())
    assert collector.collect(AT) == 'published'
    client.advance(120)
    def cool():
        client.set(COOLDOWN_KEY,'1',ex=7080,nx=True)
        raise SourceCoolingError(7080)
    monkeypatch.setattr(source,'quotes',cool)
    caplog.clear()
    with caplog.at_level('INFO'):
        assert collector.collect(AT.replace(minute=34)) == 'cooldown'
    assert client.ttl(COOLDOWN_KEY) == 7080
    assert '剩余 TTL 7080 秒' in caplog.text
    assert 'SourceCooldownError' not in caplog.text
    assert all(record.levelno < 30 for record in caplog.records)
    item = json.loads(client.get(SNAPSHOT_KEY))['items'][0]
    assert item['quote']['status'] == 'STALE' and len(item['priceSeries']) == 1


def test_one_full_sina_table_feeds_enabled_quotes_and_dictionary():
    from app.etf_monitor.dictionary import KEY
    client,source = _setup()
    source.rows.append({'代码':'sz159919','名称':'300ETF','最新价':'4.1'})
    assert EtfCollector(source,EtfStore(client),Calendar()).collect(AT)=='published'
    assert source.calls==1
    cached=json.loads(client.get(KEY))
    assert [row['symbol'] for row in cached['etfs']]==['SH510050','SZ159919']
    assert client.ttl(KEY)==86400 and cached['source']=='SINA'
    assert set(cached['etfs'][0])=={'symbol','code','name','market'}
    assert len(json.loads(client.get(SNAPSHOT_KEY))['items'])==1


def test_dictionary_redis_failure_preserves_previous_snapshot_without_source_cooldown():
    import redis
    from app.etf_monitor.dictionary import KEY
    from app.etf_monitor.collector import COOLDOWN_KEY
    client,source=_setup()
    collector=EtfCollector(source,EtfStore(client),Calendar())
    assert collector.collect(AT)=='published'
    previous=client.get(SNAPSHOT_KEY)
    client.advance(120)
    old_set=client.set
    def fail(key,*args,**kwargs):
        if key==KEY:
            raise redis.TimeoutError('private-password')
        return old_set(key,*args,**kwargs)
    client.set=fail
    with pytest.raises(redis.TimeoutError):
        collector.collect(AT.replace(minute=34))
    assert client.get(SNAPSHOT_KEY)==previous and client.get(COOLDOWN_KEY) is None
