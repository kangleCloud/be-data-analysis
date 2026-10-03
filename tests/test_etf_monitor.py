"""ETF 采样与 Redis 发布的离线回归。"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.etf_monitor import (
    ENABLED_KEY, PRICE_PREFIX, SNAPSHOT_KEY, UPDATES_CHANNEL, EtfCollector, EtfStore,
)
from app.core.config import load_settings
from app.workflows import run_etf
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
    assert collector.collect(AT.replace(minute=36)) == "partial"
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
    monkeypatch.setattr("app.workflows.AkShareEtfProvider", lambda _timeout: source)
    monkeypatch.setattr("app.workflows.calendar_service", lambda _settings, _client: Calendar())
    settings = load_settings({"STOCK_MONITOR_XQ_ENABLED": "false"})
    assert run_etf(settings, client, at=AT) == "published"
    assert source.calls == 1
    item = json.loads(client.get(SNAPSHOT_KEY))["items"][0]
    assert item["quote"]["source"] == "SINA_ETF"
    assert item["quote"]["price"] == 2.5
    assert item["priceSeries"]
