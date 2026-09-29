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
    def __init__(self, flow_rows, market_rows):
        self.flows = flow_rows
        self.market = market_rows
        self.fail = set()
        self.calls = []
        self.calendar_date = date(2026, 9, 23)

    def latest_trading_date(self, today):
        self.calls.append("calendar")
        return self.calendar_date

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


def test_full_snapshot_has_three_fresh_modules(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    store = RedisSnapshotStore(FakeRedis(), 240)
    assert MarketCollector(provider, store).collect(TRADING_AT) == "published"
    snapshot = store.load()
    assert snapshot["schemaVersion"] == 1
    assert set(snapshot["modules"]) == {
        "industryTop5", "conceptTop5", "marketFundFlow"
    }
    assert all(item["status"] == "FRESH" for item in snapshot["modules"].values())
    assert snapshot["modules"]["marketFundFlow"]["tradeDate"] == "2026-09-23"
    assert snapshot["modules"]["industryTop5"]["data"]["source"] == "THS"
    assert snapshot["modules"]["industryTop5"]["tradeDateBasis"] == "CALENDAR"


def test_partial_failure_preserves_old_module_data(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT) == "published"
    before = store.load()["modules"]["industryTop5"]
    provider.fail.add("flow:industry")
    client.advance(40 * 60)
    later = TRADING_AT.replace(minute=40)
    assert collector.collect(later) == "partial"
    after = store.load()["modules"]
    assert after["industryTop5"]["status"] == "STALE"
    assert after["industryTop5"]["data"] == before["data"]
    assert after["industryTop5"]["lastSuccessAt"] == before["lastSuccessAt"]
    assert after["marketFundFlow"]["status"] == "FRESH"
    notices = [event for event in client.events if event[0] == "publish"]
    assert len(notices) == 2
    assert notices[-1][1] == UPDATES_CHANNEL


def test_ths_cooldown_skips_second_ths_request(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    provider.fail.add("flow:industry")
    store = RedisSnapshotStore(FakeRedis(), 240)
    assert MarketCollector(provider, store).collect(TRADING_AT) == "partial"
    modules = store.load()["modules"]
    assert modules["industryTop5"]["status"] == "ERROR"
    assert modules["conceptTop5"]["status"] == "ERROR"
    assert "flow:industry" in provider.calls
    assert "flow:concept" not in provider.calls


def test_force_still_obeys_slot_and_global_interval(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT) == "published"
    before_calls = list(provider.calls)
    assert collector.collect(TRADING_AT.replace(minute=5), force=True) == "throttled"
    assert provider.calls == before_calls
    client.advance(20 * 60)
    assert collector.collect(TRADING_AT.replace(minute=10), force=True) == "published"


def test_ths_cooldown_is_shared_across_rounds(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    provider.fail.add("flow:industry")
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT) == "partial"
    assert provider.calls.count("flow:industry") == 1
    assert "flow:concept" not in provider.calls
    client.advance(40 * 60)
    provider.fail.clear()
    assert collector.collect(TRADING_AT.replace(minute=40)) == "partial"
    assert provider.calls.count("flow:industry") == 1
    client.advance(2 * 60 * 60)
    assert collector.collect(TRADING_AT.replace(hour=13, minute=10)) == "published"
    assert provider.calls.count("flow:industry") == 2


def test_first_failure_is_error_without_fake_zero(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    provider.fail.add("market")
    store = RedisSnapshotStore(FakeRedis(), 240)
    assert MarketCollector(provider, store).collect(TRADING_AT) == "partial"
    result = store.load()["modules"]["marketFundFlow"]
    assert result["status"] == "ERROR"
    assert result["tradeDate"] is None
    assert result["data"] is None


def test_outside_session_and_holiday_skip_without_fetch(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT.replace(hour=12)) == "skipped"
    assert provider.calls == []
    provider.calendar_date = date(2026, 9, 22)
    assert collector.collect(TRADING_AT) == "skipped"
    assert store.load() is None
    assert not any(event[0] == "publish" for event in client.events)


def test_overlap_returns_locked(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    token = store.acquire()
    assert MarketCollector(provider, store).collect(TRADING_AT) == "locked"
    assert provider.calls == []
    assert not any(event[0] == "publish" for event in client.events)
    store.release(token)


def test_failed_snapshot_set_does_not_notify(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    client.fail_set = True
    store = RedisSnapshotStore(client, 240)
    with pytest.raises(RuntimeError, match="写入失败"):
        MarketCollector(provider, store).collect(TRADING_AT)
    assert client.get(SNAPSHOT_KEY) is None
    assert not any(event[0] == "publish" for event in client.events)


def test_failed_publish_does_not_change_collection_result(flow_rows, market_rows, caplog):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    client.fail_publish = True
    store = RedisSnapshotStore(client, 240)
    assert MarketCollector(provider, store).collect(TRADING_AT) == "published"
    assert set(store.load()["modules"]) == {
        "industryTop5", "conceptTop5", "marketFundFlow"
    }
    assert "更新通知发送失败" in caplog.text


def test_fatal_load_failure_does_not_notify(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)

    def fail_load():
        raise ConnectionError("load failed")

    store.load = fail_load
    with pytest.raises(ConnectionError, match="load failed"):
        MarketCollector(provider, store).collect(TRADING_AT)
    assert not any(event[0] == "publish" for event in client.events)


def test_market_source_date_can_lag_collection_date(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    provider.market = market_rows.iloc[[0]]
    store = RedisSnapshotStore(FakeRedis(), 240)
    MarketCollector(provider, store).collect(TRADING_AT)
    item = store.load()["modules"]["marketFundFlow"]
    assert item["status"] == "FRESH"
    assert item["tradeDate"] == "2026-09-22"
    assert item["lastSuccessAt"].startswith("2026-09-23")


def test_calendar_failure_marks_existing_modules_stale(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT) == "published"

    def fail_calendar(_today):
        raise TimeoutError()

    provider.latest_trading_date = fail_calendar
    client.advance(40 * 60)
    assert collector.collect(TRADING_AT.replace(minute=40)) == "partial"
    assert all(item["status"] == "STALE" for item in store.load()["modules"].values())


def _seed_old_v1_top5(store, *, both: bool = False):
    """模拟旧 V1 五模块快照：东财 Top5 口径与新同花顺口径不兼容。"""
    snapshot = store.load()
    snapshot["modules"]["industryTop5"]["data"] = {
        "topRise": [{"sectorName": "半导体", "sectorType": "industry",
                     "changePercent": 2.5, "mainNetInflow": 120000000}],
        "topFall": [], "topInflow": [], "topOutflow": [],
        "unmatchedFundRows": [],
    }
    if both:
        snapshot["modules"]["conceptTop5"]["data"] = {
            "topRise": [{"sectorName": "机器人", "sectorType": "concept",
                         "changePercent": 1.2, "mainNetInflow": 98000000}],
            "topFall": [], "topInflow": [], "topOutflow": [],
            "unmatchedFundRows": [],
        }
    snapshot["modules"]["industryHeatmap"] = {"status": "FRESH", "data": []}
    snapshot["modules"]["conceptHeatmap"] = {"status": "FRESH", "data": []}
    store.save(snapshot)


def test_calendar_failure_drops_incompatible_old_v1_top5(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT) == "published"
    _seed_old_v1_top5(store, both=True)

    def fail_calendar(_today):
        raise TimeoutError()

    provider.latest_trading_date = fail_calendar
    client.advance(40 * 60)
    assert collector.collect(TRADING_AT.replace(minute=40)) == "partial"
    modules = store.load()["modules"]
    assert set(modules) == {"industryTop5", "conceptTop5", "marketFundFlow"}
    assert modules["industryTop5"]["status"] == "ERROR"
    assert modules["industryTop5"]["data"] is None
    assert modules["industryTop5"]["lastSuccessAt"] is None
    assert modules["conceptTop5"]["status"] == "ERROR"
    assert modules["conceptTop5"]["data"] is None
    assert modules["marketFundFlow"]["status"] == "STALE"


def test_module_failure_drops_incompatible_old_v1_top5(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    collector = MarketCollector(provider, store)
    assert collector.collect(TRADING_AT) == "published"
    _seed_old_v1_top5(store)
    provider.fail.add("flow:industry")
    client.advance(40 * 60)

    assert collector.collect(TRADING_AT.replace(minute=40)) == "partial"
    modules = store.load()["modules"]
    assert set(modules) == {"industryTop5", "conceptTop5", "marketFundFlow"}
    assert modules["industryTop5"]["status"] == "ERROR"
    assert modules["industryTop5"]["data"] is None
    assert modules["conceptTop5"]["status"] == "STALE"
    assert modules["conceptTop5"]["data"]["source"] == "THS"
    assert modules["marketFundFlow"]["status"] == "FRESH"


def test_first_calendar_failure_publishes_error_modules(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    store = RedisSnapshotStore(FakeRedis(), 240)

    def fail_calendar(_today):
        raise TimeoutError()

    provider.latest_trading_date = fail_calendar
    assert MarketCollector(provider, store).collect(TRADING_AT) == "partial"
    modules = store.load()["modules"]
    assert len(modules) == 3
    assert all(item["status"] == "ERROR" and item["data"] is None for item in modules.values())
