"""共享交易日历的缓存、覆盖边界与刷新风控。"""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.calendar.scheduler import next_monthly_slot
from app.calendar.service import (
    CACHE_KEY, LOCK_KEY, AkShareCalendarSource, CalendarService, normalize_dates,
)
from tests.test_snapshot import FakeRedis

SHANGHAI = ZoneInfo("Asia/Shanghai")
AT = datetime(2026, 10, 1, 0, 10, tzinfo=SHANGHAI)


class Source:
    def __init__(self, dates):
        self.values = dates
        self.calls = 0
        self.fail = False

    def dates(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("calendar source failed")
        return self.values


def large_fake_worker(queue, url, call, keys, token, guard, deadline, read, parent, started):
    import pandas as pd
    queue.put(("ok", pd.DataFrame({"trade_date": ["2026-10-02"] * 9000})))


def test_calendar_source_reads_large_child_result_before_join():
    from app.runtime.source_execution import SourceExecutor
    from tests.test_source_execution import ControlRedis
    backend = ControlRedis()
    executor = SourceExecutor('redis://offline', client=backend, worker=large_fake_worker)
    assert len(AkShareCalendarSource(1, executor=executor).dates()) == 9000
    assert backend.metrics()[-1] == []


def test_normalize_validates_every_date_and_sorts_unique():
    payload = normalize_dates([date(2026, 10, 2), "2026-09-30", "2026-10-02"], AT)
    assert payload == {
        "schemaVersion": 1, "source": "AKShare.tool_trade_date_hist_sina",
        "year": 2026,
        "refreshedAt": "2026-10-01T00:10:00+08:00",
        "firstDate": "2026-09-30", "lastDate": "2026-10-02",
        "dates": ["2026-09-30", "2026-10-02"],
    }
    for rows in ([], ["2026-02-30"], ["2026-10-02", "bad"]):
        with pytest.raises(ValueError):
            normalize_dates(rows, AT)


def test_full_source_is_checked_before_other_years_are_removed():
    payload = normalize_dates([
        "2025-12-31", "2026-10-02", "2027-01-04", "2026-01-05",
        "2026-10-02",
    ], AT)
    assert payload["year"] == 2026
    assert payload["dates"] == ["2026-01-05", "2026-10-02"]
    assert (payload["firstDate"], payload["lastDate"]) == ("2026-01-05", "2026-10-02")
    with pytest.raises(ValueError, match="无当年"):
        normalize_dates(["2025-12-31", "2027-01-04"], AT)
    with pytest.raises(ValueError, match="无效日期"):
        normalize_dates(["2026-10-02", "2025-02-30"], AT)


def test_cold_start_builds_once_and_holiday_is_false_not_unknown():
    client = FakeRedis()
    source = Source(["2026-09-30", "2026-10-02"])
    calendar = CalendarService(client, source)
    assert calendar.day_status(date(2026, 10, 1), AT) is False
    assert calendar.day_status(date(2026, 10, 2), AT) is True
    assert source.calls == 1
    assert json.loads(client.get(CACHE_KEY))["dates"] == ["2026-09-30", "2026-10-02"]
    assert CACHE_KEY not in client.expiry


def test_out_of_range_is_unknown_and_retry_is_daily_not_every_120_seconds():
    client = FakeRedis()
    source = Source(["2026-09-30"])
    calendar = CalendarService(client, source)
    assert calendar.day_status(date(2026, 10, 1), AT) is None
    assert calendar.day_status(date(2026, 10, 1), AT.replace(minute=12)) is None
    assert source.calls == 1
    assert client.get(CACHE_KEY) is None
    client.advance(24 * 60 * 60)
    source.values = ["2026-09-30", "2026-10-02"]
    assert calendar.day_status(date(2026, 10, 2), AT.replace(day=2)) is True
    assert source.calls == 2


def test_refresh_failure_preserves_covered_old_cache_and_monthly_retry():
    client = FakeRedis()
    source = Source(["2026-09-30", "2026-10-02"])
    calendar = CalendarService(client, source)
    previous = normalize_dates(source.values, AT.replace(month=9, day=30))
    client.set(CACHE_KEY, json.dumps(previous))
    source.fail = True
    assert calendar.day_status(date(2026, 10, 1), AT) is False
    assert client.get(CACHE_KEY) == json.dumps(previous)
    assert calendar.day_status(date(2026, 10, 1), AT.replace(minute=12)) is False
    assert source.calls == 1


def test_manual_refresh_obeys_lock_and_interval():
    client = FakeRedis()
    source = Source(["2026-09-30", "2026-10-02"])
    calendar = CalendarService(client, source)
    client.set(LOCK_KEY, "other", nx=True, ex=120)
    assert calendar.refresh(AT, manual=True) == "locked"
    assert source.calls == 0
    client.advance(120)
    assert calendar.refresh(AT, manual=True) == "refreshed"
    assert calendar.refresh(AT, manual=True) == "throttled"
    assert source.calls == 1
    client.advance(600)
    assert calendar.refresh(AT, manual=True) == "refreshed"
    assert source.calls == 2


def test_invalid_stored_payload_is_not_trusted():
    client = FakeRedis()
    client.set(CACHE_KEY, '{"schemaVersion":1,"dates":["2026-10-02"]}')
    source = Source(["2026-09-30", "2026-10-02"])
    assert CalendarService(client, source).day_status(date(2026, 10, 1), AT) is False
    assert source.calls == 1


def test_monthly_slot_and_next_month_boundary():
    assert next_monthly_slot(AT.replace(month=9, day=30)).isoformat() == "2026-10-01T00:10:00+08:00"
    assert next_monthly_slot(AT).isoformat() == "2026-11-01T00:10:00+08:00"
    assert next_monthly_slot(AT.replace(month=12)).isoformat() == "2027-01-01T00:10:00+08:00"


def test_year_end_cache_is_valid_but_new_year_requires_new_year_dates():
    client = FakeRedis()
    source = Source(["2025-12-31", "2026-01-02", "2026-12-31", "2027-01-04"])
    calendar = CalendarService(client, source)
    year_end = datetime(2026, 12, 31, 10, 0, tzinfo=SHANGHAI)
    assert calendar.day_status(year_end.date(), year_end) is True
    stored = json.loads(client.get(CACHE_KEY))
    assert stored["year"] == 2026
    assert stored["dates"] == ["2026-01-02", "2026-12-31"]
    assert calendar.covered(stored, date(2027, 1, 1)) is False
    client.advance(24 * 60 * 60)
    new_year = datetime(2027, 1, 1, 0, 10, tzinfo=SHANGHAI)
    assert calendar.day_status(new_year.date(), new_year) is None
    new_cache = json.loads(client.get(CACHE_KEY))
    assert new_cache["year"] == 2027
    assert new_cache["dates"] == ["2027-01-04"]
    assert (new_cache["firstDate"], new_cache["lastDate"]) == (
        "2027-01-04", "2027-01-04"
    )


def test_new_year_source_failure_keeps_old_key_but_never_uses_it():
    client = FakeRedis()
    source = Source(["2026-01-02", "2026-12-31"])
    calendar = CalendarService(client, source)
    year_end = datetime(2026, 12, 31, 10, 0, tzinfo=SHANGHAI)
    assert calendar.day_status(year_end.date(), year_end) is True
    old_raw = client.get(CACHE_KEY)
    client.advance(24 * 60 * 60)
    source.fail = True
    new_year = datetime(2027, 1, 1, 0, 10, tzinfo=SHANGHAI)
    assert calendar.day_status(new_year.date(), new_year) is None
    assert client.get(CACHE_KEY) == old_raw
    assert calendar.day_status(new_year.date(), new_year.replace(minute=12)) is None
    assert source.calls == 2


def test_old_multiyear_payload_without_year_is_rejected():
    client = FakeRedis()
    old = {
        "schemaVersion": 1, "source": "AKShare.tool_trade_date_hist_sina",
        "refreshedAt": "2026-12-31T10:00:00+08:00",
        "firstDate": "2026-12-31", "lastDate": "2027-01-04",
        "dates": ["2026-12-31", "2027-01-04"],
    }
    client.set(CACHE_KEY, json.dumps(old))
    source = Source(["2026-12-31"])
    new_year = datetime(2027, 1, 1, 0, 10, tzinfo=SHANGHAI)
    assert CalendarService(client, source).day_status(new_year.date(), new_year) is None
    assert client.get(CACHE_KEY) == json.dumps(old)


def test_calendar_resource_error_is_distinct_and_preserves_cache():
    from app.runtime.resources import SourceResourceError
    from app.calendar.service import CACHE_KEY
    from tests.test_snapshot import FakeRedis
    client=FakeRedis()
    client.set(CACHE_KEY,'previous')
    class Source:
        def dates(self):
            raise SourceResourceError('PROCESS_EXIT',exitcode=-9)
    with pytest.raises(SourceResourceError):
        CalendarService(client,Source()).refresh(datetime(2026,10,9,10,tzinfo=SHANGHAI),manual=True)
    assert client.get(CACHE_KEY)=='previous'
