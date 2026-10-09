"""服务内时段调度和一次性子进程。"""

import asyncio
import pytest
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from tests.test_snapshot import FakeRedis

import app.main as main_module
from app.scheduler import launch_slot, next_slot

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_next_slot_skips_missed_times_and_weekend():
    at = datetime(2026, 9, 25, 10, 5, tzinfo=SHANGHAI)
    assert next_slot(at).strftime("%Y-%m-%d %H:%M") == "2026-09-25 10:06"
    assert next_slot(at.replace(minute=10)).strftime("%H:%M") == "10:12"
    assert next_slot(at.replace(hour=15, minute=10)).strftime("%Y-%m-%d %H:%M") == "2026-09-28 09:30"


def test_launch_slot_skips_late_start_and_uses_child_process(monkeypatch):
    slot = datetime(2026, 9, 25, 10, 10, tzinfo=SHANGHAI)
    calls = []

    class FakeProcess:
        async def wait(self):
            return 0

    async def fake_create(*args):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr("app.scheduler.asyncio.create_subprocess_exec", fake_create)
    assert not asyncio.run(launch_slot(slot, slot + timedelta(minutes=2)))
    assert calls == []
    assert asyncio.run(launch_slot(slot, slot + timedelta(seconds=10)))
    assert calls == [(sys.executable, "-m", "app", "collect")]


def test_serve_lifespan_starts_and_stops_scheduler(monkeypatch):
    events = []

    async def fake_scheduler():
        events.append("start")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stop")

    monkeypatch.setattr(main_module, "run_scheduler", fake_scheduler)
    monkeypatch.setattr(main_module, "run_calendar_scheduler", lambda _settings: fake_scheduler())
    with TestClient(main_module.create_app(redis_factory=FakeRedis)) as client:
        assert client.get("/health").status_code == 200
    assert events == ["start", "start", "stop", "stop"]


def test_cancelled_worker_is_terminated_killed_and_reaped(monkeypatch):
    from app.process_wait import wait_worker
    events = []
    class Process:
        async def wait(self):
            events.append('wait')
            if len(events) == 1:
                raise asyncio.CancelledError()
            if 'kill' not in events:
                raise asyncio.TimeoutError()
            return -9
        def terminate(self):
            events.append('term')
        def kill(self):
            events.append('kill')
    import pytest
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(wait_worker(Process()))
    assert events == ['wait', 'term', 'wait', 'kill', 'wait']


def simulate_scheduler(monkeypatch, start, durations, startup_lag=0):
    import pytest
    import app.scheduler as module
    clock, starts, sleeps = [start], [], []
    running = [False]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    class Done(Exception):
        pass
    async def sleep(seconds):
        assert not running[0]
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds + (startup_lag if not starts else 0))
    class Process:
        async def wait(self):
            clock[0] += timedelta(seconds=durations[len(starts)-1])
            running[0] = False
            if len(starts) == len(durations):
                raise Done()
            return 0
    async def create(*args):
        assert not running[0]
        starts.append(clock[0])
        running[0] = True
        return Process()
    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module.clock, 'monotonic', lambda:clock[0].timestamp())
    monkeypatch.setattr(module.asyncio,'sleep',sleep)
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',create)
    with pytest.raises(Done):
        asyncio.run(module.run_scheduler())
    return starts, sleeps


def test_slow_market_round_continues_at_actual_completion(monkeypatch):
    starts, sleeps = simulate_scheduler(monkeypatch, datetime(2026,10,8,10,0,tzinfo=SHANGHAI), [181,10])
    assert [item.strftime('%H:%M:%S') for item in starts] == ['10:02:00','10:05:01']
    assert sleeps == [120,0]


@pytest.mark.parametrize('duration',[0,1,119,120])
def test_market_minimum_start_spacing_and_no_busy_loop(monkeypatch, duration):
    starts, sleeps = simulate_scheduler(monkeypatch, datetime(2026,10,8,10,0,tzinfo=SHANGHAI), [duration,0], startup_lag=10)
    expected_spacing = 230 if duration == 119 else 120
    assert (starts[1]-starts[0]).total_seconds() == expected_spacing
    assert sleeps[1] == expected_spacing-duration


@pytest.mark.parametrize('start,expected', [
    (datetime(2026,10,8,11,28,tzinfo=SHANGHAI),'2026-10-08 13:00'),
    (datetime(2026,10,9,15,8,tzinfo=SHANGHAI),'2026-10-12 09:30'),
])
def test_slow_market_round_waits_for_next_session_when_window_ends(monkeypatch, start, expected):
    starts, sleeps = simulate_scheduler(monkeypatch, start,[180,0])
    assert starts[1].strftime('%Y-%m-%d %H:%M') == expected
    assert sleeps[1] > 0
