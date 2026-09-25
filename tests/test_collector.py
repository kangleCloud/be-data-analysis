"""单次采集、交易时段与降级行为。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.collector import MarketCollector
from app.snapshot import SNAPSHOT_KEY, UPDATES_CHANNEL, RedisSnapshotStore
from tests.test_snapshot import FakeRedis

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADING_AT = datetime(2026, 9, 23, 10, 0, tzinfo=SHANGHAI)


class FakeProvider:
    def __init__(self, sector_rows, flow_rows, market_rows):
        self.sectors = sector_rows
        self.flows = flow_rows
        self.market = market_rows
        self.fail = set()
        self.calls = []
        self.calendar_date = date(2026, 9, 23)

    def latest_trading_date(self, today):
        self.calls.append("calendar")
        return self.calendar_date

    def sector_quotes(self, sector_type):
        self.calls.append(f"quotes:{sector_type}")
        if f"quotes:{sector_type}" in self.fail:
            raise TimeoutError()
        return self.sectors

    def sector_fund_flow(self, sector_type):
        self.calls.append(f"flow:{sector_type}")
        if f"flow:{sector_type}" in self.fail:
            raise TimeoutError()
        return self.flows

    def market_fund_flow(self):
        self.calls.append("market")
        if "market" in self.fail:
            raise TimeoutError()
        return self.market


def test_full_snapshot_has_five_fresh_modules(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    store = RedisSnapshotStore(FakeRedis(), 240)
    assert MarketCollector(provider, store).collect(TRADING_AT) == "published"
    snapshot = store.load()
    assert snapshot["schemaVersion"] == 2
    assert set(snapshot["modules"]) == {
        "industryHeatmap", "conceptHeatmap", "industryTop5", "conceptTop5", "marketFundFlow"
    }
    assert all(item["status"] == "FRESH" for item in snapshot["modules"].values())
    assert snapshot["modules"]["marketFundFlow"]["tradeDate"] == "2026-09-23"
    assert snapshot["modules"]["industryHeatmap"]["tradeDateBasis"] == "CALENDAR"
    assert snapshot["modules"]["industryTop5"]["data"]["source"] == "THS"
    assert snapshot["modules"]["industryTop5"]["tradeDateBasis"] == "CALENDAR"


def test_partial_failure_preserves_old_module_data(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT) == "published"
    before = store.load()["modules"]["industryTop5"]
    provider.fail.add("flow:industry")
    later = TRADING_AT.replace(minute=5)
    assert collector.collect(later) == "partial"
    after = store.load()["modules"]
    assert after["industryTop5"]["status"] == "STALE"
    assert after["industryTop5"]["data"] == before["data"]
    assert after["industryTop5"]["lastSuccessAt"] == before["lastSuccessAt"]
    assert after["industryHeatmap"]["status"] == "FRESH"
    notices = [event for event in client.events if event[0] == "publish"]
    assert len(notices) == 2
    assert notices[-1][1] == UPDATES_CHANNEL


def test_heatmap_failure_does_not_block_ths_top5(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    provider.fail.add("quotes:industry")
    store = RedisSnapshotStore(FakeRedis(), 240)
    assert MarketCollector(provider, store).collect(TRADING_AT) == "partial"
    modules = store.load()["modules"]
    assert modules["industryHeatmap"]["status"] == "ERROR"
    assert modules["industryTop5"]["status"] == "FRESH"
    assert modules["industryTop5"]["data"]["topInflow"][0]["sectorName"] == "半导体"
    assert "flow:industry" in provider.calls


def test_first_failure_is_error_without_fake_zero(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    provider.fail.add("market")
    store = RedisSnapshotStore(FakeRedis(), 240)
    assert MarketCollector(provider, store).collect(TRADING_AT) == "partial"
    result = store.load()["modules"]["marketFundFlow"]
    assert result["status"] == "ERROR"
    assert result["tradeDate"] is None
    assert result["data"] is None


def test_outside_session_and_holiday_skip_without_fetch(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT.replace(hour=12)) == "skipped"
    assert provider.calls == []
    provider.calendar_date = date(2026, 9, 22)
    assert collector.collect(TRADING_AT) == "skipped"
    assert store.load() is None
    assert not any(event[0] == "publish" for event in client.events)


def test_overlap_returns_locked(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    token = store.acquire()
    assert MarketCollector(provider, store).collect(TRADING_AT) == "locked"
    assert provider.calls == []
    assert not any(event[0] == "publish" for event in client.events)
    store.release(token)


def test_failed_snapshot_set_does_not_notify(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    client = FakeRedis()
    client.fail_set = True
    store = RedisSnapshotStore(client, 240)
    with pytest.raises(RuntimeError, match="写入失败"):
        MarketCollector(provider, store).collect(TRADING_AT)
    assert client.get(SNAPSHOT_KEY) is None
    assert not any(event[0] == "publish" for event in client.events)


def test_failed_publish_does_not_change_collection_result(sector_rows, flow_rows, market_rows, caplog):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    client = FakeRedis()
    client.fail_publish = True
    store = RedisSnapshotStore(client, 240)
    assert MarketCollector(provider, store).collect(TRADING_AT) == "published"
    assert set(store.load()["modules"]) == {
        "industryHeatmap", "conceptHeatmap", "industryTop5", "conceptTop5", "marketFundFlow"
    }
    assert "更新通知发送失败" in caplog.text


def test_fatal_load_failure_does_not_notify(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)

    def fail_load():
        raise ConnectionError("load failed")

    store.load = fail_load
    with pytest.raises(ConnectionError, match="load failed"):
        MarketCollector(provider, store).collect(TRADING_AT)
    assert not any(event[0] == "publish" for event in client.events)


def test_market_source_date_can_lag_collection_date(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    provider.market = market_rows.iloc[[0]]
    store = RedisSnapshotStore(FakeRedis(), 240)
    MarketCollector(provider, store).collect(TRADING_AT)
    item = store.load()["modules"]["marketFundFlow"]
    assert item["status"] == "FRESH"
    assert item["tradeDate"] == "2026-09-22"
    assert item["lastSuccessAt"].startswith("2026-09-23")


def test_calendar_failure_marks_existing_modules_stale(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    store = RedisSnapshotStore(FakeRedis(), 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT) == "published"

    def fail_calendar(_today):
        raise TimeoutError()

    provider.latest_trading_date = fail_calendar
    assert collector.collect(TRADING_AT.replace(minute=5)) == "partial"
    assert all(item["status"] == "STALE" for item in store.load()["modules"].values())


def test_first_calendar_failure_publishes_error_modules(sector_rows, flow_rows, market_rows):
    provider = FakeProvider(sector_rows, flow_rows, market_rows)
    store = RedisSnapshotStore(FakeRedis(), 240)

    def fail_calendar(_today):
        raise TimeoutError()

    provider.latest_trading_date = fail_calendar
    assert MarketCollector(provider, store).collect(TRADING_AT) == "partial"
    modules = store.load()["modules"]
    assert len(modules) == 5
    assert all(item["status"] == "ERROR" and item["data"] is None for item in modules.values())
