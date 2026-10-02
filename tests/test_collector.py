"""两分钟采集、独立降级、交易日和同日曲线。"""

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
        self.flows, self.market = flow_rows, market_rows
        self.fail = set()
        self.calls = []
        self.calendar_date = date(2026, 9, 23)

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


class FakeCalendar:
    def __init__(self, provider):
        self.provider = provider

    def day_status(self, day, _at):
        self.provider.calls.append("calendar")
        if "calendar" in self.provider.fail:
            return None
        return self.provider.calendar_date == day


def setup(flow_rows, market_rows):
    provider = FakeProvider(flow_rows, market_rows)
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    return provider, client, store, MarketCollector(provider, store, FakeCalendar(provider))


def test_snapshot_has_new_three_modules_and_calendar_basis(flow_rows, market_rows):
    provider, client, store, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT) == "published"
    snapshot = store.load()
    assert snapshot["schemaVersion"] == 1 and snapshot["provider"] == "akshare"
    assert set(snapshot["modules"]) == {"industrySectors", "conceptSectors", "marketFundFlow"}
    assert all(module["status"] == "FRESH" and module["tradeDateBasis"] == "CALENDAR"
               for module in snapshot["modules"].values())
    assert snapshot["modules"]["industrySectors"]["data"]["items"][0]["name"] == "半导体"
    assert snapshot["modules"]["marketFundFlow"]["data"]["source"] == "THS_INDIVIDUAL_AGGREGATE"
    assert provider.calls == ["calendar", "flow:industry", "flow:concept", "market"]
    assert [event[1] for event in client.events if event[0] == "publish"] == [UPDATES_CHANNEL]


def test_two_minute_interval_and_force_keep_safety_gates(flow_rows, market_rows):
    provider, client, store, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT) == "published"
    before = list(provider.calls)
    assert collector.collect(TRADING_AT.replace(minute=1), force=True) == "throttled"
    assert provider.calls == before
    client.advance(120)
    assert collector.collect(TRADING_AT.replace(minute=2), force=True) == "published"
    assert collector.collect(TRADING_AT.replace(hour=15, minute=12), force=True) == "skipped"
    assert provider.calls.count("market") == 2


def test_final_1510_slot_allows_process_startup_seconds(flow_rows, market_rows):
    _, _, _, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT.replace(hour=15, minute=10, second=8)) == "published"
    assert collector.collect(TRADING_AT.replace(hour=15, minute=11)) == "skipped"


def test_market_curve_only_adds_actual_success_points_same_day(flow_rows, market_rows):
    provider, client, store, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT) == "published"
    first = store.load()["modules"]["marketFundFlow"]["data"]["series"]
    assert len(first) == 1
    client.advance(120)
    provider.fail.add("market")
    assert collector.collect(TRADING_AT.replace(minute=2)) == "partial"
    failed = store.load()["modules"]["marketFundFlow"]
    assert failed["status"] == "STALE" and failed["data"]["series"] == first
    provider.fail.clear()
    client.advance(2 * 60 * 60)
    assert collector.collect(TRADING_AT.replace(hour=13)) == "published"
    series = store.load()["modules"]["marketFundFlow"]["data"]["series"]
    assert [point["collectedAt"] for point in series] == [
        "2026-09-23T10:00:00+08:00", "2026-09-23T13:00:00+08:00"
    ]


def test_module_failure_retains_only_that_module_valid_history(flow_rows, market_rows):
    provider, client, store, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT) == "published"
    old = store.load()["modules"]["industrySectors"]
    provider.fail.add("flow:industry")
    client.advance(120)
    assert collector.collect(TRADING_AT.replace(minute=2)) == "partial"
    modules = store.load()["modules"]
    assert modules["industrySectors"]["status"] == "STALE"
    assert modules["industrySectors"]["data"] == old["data"]
    assert modules["conceptSectors"]["status"] == "STALE"
    assert modules["marketFundFlow"]["status"] == "STALE"
    assert provider.calls.count("flow:concept") == 1


def test_invalid_one_module_does_not_block_other_two(flow_rows, market_rows):
    provider, _, store, collector = setup(flow_rows, market_rows)
    bad = flow_rows.drop(columns="行业指数")
    original = provider.sector_fund_flow

    def sector(kind):
        return bad if kind == "industry" else original(kind)

    provider.sector_fund_flow = sector
    assert collector.collect(TRADING_AT) == "partial"
    modules = store.load()["modules"]
    assert modules["industrySectors"]["status"] == "ERROR"
    assert modules["conceptSectors"]["status"] == "FRESH"
    assert modules["marketFundFlow"]["status"] == "FRESH"


def test_partial_individual_data_is_not_published_as_market_success(flow_rows, market_rows):
    provider, _, store, collector = setup(flow_rows, market_rows)
    provider.market = market_rows.assign(净额=None)
    assert collector.collect(TRADING_AT) == "partial"
    module = store.load()["modules"]["marketFundFlow"]
    assert module["status"] == "ERROR" and module["data"] is None


def test_calendar_failure_marks_previous_modules_stale(flow_rows, market_rows):
    provider, client, store, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT) == "published"
    provider.fail.add("calendar")
    client.advance(120)
    assert collector.collect(TRADING_AT.replace(minute=2)) == "partial"
    assert all(item["status"] == "STALE" for item in store.load()["modules"].values())
    assert provider.calls.count("flow:industry") == 1
    assert provider.calls.count("market") == 1


def test_holiday_closed_market_and_overlap_do_not_fetch(flow_rows, market_rows):
    provider, client, store, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT.replace(hour=12)) == "skipped"
    assert provider.calls == []
    provider.calendar_date = date(2026, 9, 22)
    assert collector.collect(TRADING_AT, force=True) == "skipped"
    assert store.load() is None
    client.advance(120)
    provider.calendar_date = date(2026, 9, 23)
    token = store.acquire()
    assert collector.collect(TRADING_AT) == "locked"
    store.release(token)
    assert provider.calls == ["calendar"]


def test_old_top5_and_eastmoney_data_are_not_reused(flow_rows, market_rows):
    provider, client, store, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT) == "published"
    previous = store.load()
    previous["modules"]["industrySectors"]["data"] = {"topRise": []}
    previous["modules"]["marketFundFlow"]["data"]["source"] = "EASTMONEY"
    store.save(previous)
    provider.fail.add("flow:industry")
    client.advance(120)
    assert collector.collect(TRADING_AT.replace(minute=2)) == "partial"
    modules = store.load()["modules"]
    assert modules["industrySectors"]["status"] == "ERROR"
    assert modules["marketFundFlow"]["status"] == "ERROR"


def test_old_float_company_count_is_not_republished_on_failure(flow_rows, market_rows):
    provider, client, store, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT) == "published"
    old = store.load()
    old["modules"]["industrySectors"]["data"]["items"][0]["companyCount"] = 55.0
    store.save(old)
    provider.fail.add("flow:industry")
    client.advance(120)
    assert collector.collect(TRADING_AT.replace(minute=2)) == "partial"
    module = store.load()["modules"]["industrySectors"]
    assert module["status"] == "ERROR"
    assert module["data"] is None


def test_lost_lock_rejects_publication(flow_rows, market_rows):
    _, client, store, collector = setup(flow_rows, market_rows)
    store.renew = lambda _token: False
    with pytest.raises(RuntimeError, match="锁已失效"):
        collector.collect(TRADING_AT)
    assert client.get(SNAPSHOT_KEY) is None


def test_snapshot_set_failure_does_not_notify(flow_rows, market_rows):
    _, client, store, collector = setup(flow_rows, market_rows)
    client.fail_set = True
    with pytest.raises(RuntimeError, match="事务失败"):
        collector.collect(TRADING_AT)
    assert client.get(SNAPSHOT_KEY) is None
    assert not any(event[0] == "publish" for event in client.events)


def test_same_market_batch_writes_only_enabled_fund_points(flow_rows, market_rows):
    import json
    import pandas as pd
    from app.snapshot import FUND_SERIES_PREFIX
    from app.stock_monitor import ENABLED_KEY

    extra = pd.DataFrame([
        {"股票代码": f"{600001 + index:06d}", "股票简称": f"股票{index}",
         "涨跌幅": 0, "流入资金": 0, "流出资金": 0, "净额": 0}
        for index in range(9)
    ])
    provider, client, store, collector = setup(
        flow_rows, pd.concat([market_rows, extra], ignore_index=True)
    )
    symbols = ["SH600000"] + [f"SH{600001 + index:06d}" for index in range(9)]
    client.set(ENABLED_KEY, json.dumps([
        {"symbol": symbol, "code": symbol[2:], "name": symbol, "market": "SH"}
        for symbol in symbols
    ]))
    assert collector.collect(TRADING_AT) == "published"
    assert provider.calls.count("market") == 1
    assert store.load()["modules"]["marketFundFlow"]["data"]["latest"]["stockCount"] == 11
    fund_keys = [key for key in client.values if key.startswith(
        f"{FUND_SERIES_PREFIX}2026-09-23:"
    )]
    assert len(fund_keys) == 10
    assert client.get(f"{FUND_SERIES_PREFIX}2026-09-23:SZ000001") is None
    point = json.loads(client.get(f"{FUND_SERIES_PREFIX}2026-09-23:SH600001"))[0]
    assert point == {
        "collectedAt": "2026-09-23T10:00:00+08:00",
        "inflow": 0, "outflow": 0, "netAmount": 0,
    }
    assert len(client.transactions) == 1
    assert ("set", SNAPSHOT_KEY) in [command[:2] for command in client.transactions[0]]
    assert client.transactions[0][-1][:2] == ("publish", UPDATES_CHANNEL)


def test_market_source_failure_leaves_fund_series_and_snapshot_stale(flow_rows, market_rows):
    import json
    from app.snapshot import FUND_SERIES_PREFIX
    from app.stock_monitor import ENABLED_KEY

    provider, client, store, collector = setup(flow_rows, market_rows)
    client.set(ENABLED_KEY, json.dumps([{
        "symbol": "SH600000", "code": "600000", "name": "浦发银行", "market": "SH",
    }]))
    assert collector.collect(TRADING_AT) == "published"
    key = f"{FUND_SERIES_PREFIX}2026-09-23:SH600000"
    first = client.get(key)
    client.advance(120)
    provider.fail.add("market")
    assert collector.collect(TRADING_AT.replace(minute=2)) == "partial"
    assert client.get(key) == first
    assert store.load()["modules"]["marketFundFlow"]["status"] == "STALE"
    assert len(client.transactions) == 2
    assert not any(command[:2] == ("publish", "stock:monitor:v1:updates")
                   for command in client.transactions[1])


def test_fund_history_keeps_two_data_days_and_holiday_does_not_prune(flow_rows, market_rows):
    import json
    from app.snapshot import FUND_DATES_KEY, FUND_SERIES_PREFIX
    from app.stock_monitor import ENABLED_KEY

    provider, client, _, collector = setup(flow_rows, market_rows)
    client.set(ENABLED_KEY, json.dumps([{
        "symbol": "SH600000", "code": "600000", "name": "浦发银行", "market": "SH",
    }]))
    for day in (23, 24, 25):
        provider.calendar_date = date(2026, 9, day)
        client.advance(120)
        assert collector.collect(TRADING_AT.replace(day=day)) == "published"
    assert json.loads(client.get(FUND_DATES_KEY)) == ["2026-09-24", "2026-09-25"]
    assert client.get(f"{FUND_SERIES_PREFIX}2026-09-23:SH600000") is None
    assert client.get(f"{FUND_SERIES_PREFIX}2026-09-24:SH600000") is not None
    before = client.get(FUND_DATES_KEY)
    client.advance(120)
    assert collector.collect(TRADING_AT.replace(month=10, day=1)) == "skipped"
    assert client.get(FUND_DATES_KEY) == before


def test_snapshot_transaction_failure_writes_neither_snapshot_nor_fund_point(flow_rows, market_rows):
    import json
    from app.snapshot import FUND_SERIES_PREFIX
    from app.stock_monitor import ENABLED_KEY

    _, client, _, collector = setup(flow_rows, market_rows)
    client.set(ENABLED_KEY, json.dumps([{
        "symbol": "SH600000", "code": "600000", "name": "浦发银行", "market": "SH",
    }]))
    client.fail_set = True
    with pytest.raises(RuntimeError, match="事务失败"):
        collector.collect(TRADING_AT)
    assert client.get(SNAPSHOT_KEY) is None
    assert client.get(f"{FUND_SERIES_PREFIX}2026-09-23:SH600000") is None


def test_new_snapshot_reconciles_legacy_market_curve_points(flow_rows, market_rows):
    provider, client, store, collector = setup(flow_rows, market_rows)
    assert collector.collect(TRADING_AT) == "published"
    legacy = store.load()
    old_point = legacy["modules"]["marketFundFlow"]["data"]["series"][0]
    old_point["netAmount"] = -999
    legacy["modules"]["marketFundFlow"]["data"].pop("reconciledFromLegacy")
    store.save(legacy)
    client.advance(120)
    assert collector.collect(TRADING_AT.replace(minute=2)) == "published"
    data = store.load()["modules"]["marketFundFlow"]["data"]
    assert data["reconciledFromLegacy"] is False
    assert all(point["netAmount"] == point["inflow"] - point["outflow"]
               for point in data["series"])


def test_enabled_stock_missing_from_next_batch_leaves_fund_gap(flow_rows, market_rows):
    import json
    from app.snapshot import FUND_SERIES_PREFIX
    from app.stock_monitor import ENABLED_KEY

    provider, client, store, collector = setup(flow_rows, market_rows)
    client.set(ENABLED_KEY, json.dumps([
        {"symbol": "SH600000", "code": "600000", "name": "浦发银行", "market": "SH"},
        {"symbol": "SZ000001", "code": "000001", "name": "平安银行", "market": "SZ"},
    ]))
    assert collector.collect(TRADING_AT) == "published"
    client.advance(120)
    provider.market = market_rows.iloc[[0]]
    assert collector.collect(TRADING_AT.replace(minute=2)) == "published"
    first = json.loads(client.get(f"{FUND_SERIES_PREFIX}2026-09-23:SH600000"))
    missing = json.loads(client.get(f"{FUND_SERIES_PREFIX}2026-09-23:SZ000001"))
    assert len(first) == 2
    assert len(missing) == 1
    assert store.load()["modules"]["marketFundFlow"]["data"]["latest"]["stockCount"] == 1
