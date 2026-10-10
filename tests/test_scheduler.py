"""单服务父进程轮次调度：长轮无重叠，短轮120秒间隔，不补错过点。"""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.core.config import load_settings
from app.runtime.scheduler import next_slot
from tests.test_snapshot import FakeRedis

SHANGHAI = ZoneInfo('Asia/Shanghai')


def test_next_slot_skips_missed_times_and_weekend():
    at = datetime(2026,9,25,10,5,tzinfo=SHANGHAI)
    assert next_slot(at).strftime('%Y-%m-%d %H:%M') == '2026-09-25 10:06'
    assert next_slot(at.replace(minute=10)).strftime('%H:%M') == '10:12'
    assert next_slot(at.replace(hour=15,minute=10)).strftime('%Y-%m-%d %H:%M') == '2026-09-28 09:30'


def test_serve_lifespan_starts_one_quote_loop_and_one_calendar_loop(monkeypatch):
    events = []
    async def scheduler(settings):
        events.append('start')
        try:
            await asyncio.Event().wait()
        finally:
            events.append('stop')
    monkeypatch.setattr(main_module,'run_scheduler',scheduler)
    monkeypatch.setattr(main_module,'run_calendar_scheduler',scheduler)
    monkeypatch.setattr(main_module,'run_funds_scheduler',scheduler)
    with TestClient(main_module.create_app(redis_factory=FakeRedis)) as client:
        assert client.get('/health').status_code == 200
    assert events == ['start','start','start','stop','stop','stop']


def simulate_scheduler(monkeypatch,start,durations,startup_lag=0,lane="quotes"):
    import app.runtime.scheduler as module
    clock,starts,sleeps = [start],[],[]
    running = [False]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return clock[0]
    class Done(BaseException):
        pass
    async def sleep(seconds):
        assert not running[0]
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds+(startup_lag if not starts else 0))
    async def to_thread(function,settings,cancel,lane):
        assert not running[0]
        running[0] = True
        starts.append(clock[0])
        clock[0] += timedelta(seconds=durations[len(starts)-1])
        running[0] = False
        if len(starts) == len(durations):
            raise Done()
        return {'market':'published'}
    monkeypatch.setattr(module,'datetime',Clock)
    monkeypatch.setattr(module.clock,'monotonic',lambda:clock[0].timestamp())
    monkeypatch.setattr(module.asyncio,'sleep',sleep)
    monkeypatch.setattr(module.asyncio,'to_thread',to_thread)
    with pytest.raises(Done):
        asyncio.run(module.run_scheduler(load_settings({}),lane=lane))
    return starts,sleeps


@pytest.mark.parametrize("lane",["quotes","funds"])
def test_slow_round_continues_at_actual_completion(monkeypatch,lane):
    starts,sleeps = simulate_scheduler(monkeypatch,datetime(2026,10,8,10,0,tzinfo=SHANGHAI),[181,10],lane=lane)
    assert [at.strftime('%H:%M:%S') for at in starts] == ['10:02:00','10:05:01']
    assert sleeps == [120,0]


@pytest.mark.parametrize('lane',['quotes','funds'])
@pytest.mark.parametrize('duration',[0,1,119,120])
def test_minimum_start_spacing_and_no_busy_loop(monkeypatch,duration,lane):
    starts,sleeps = simulate_scheduler(monkeypatch,datetime(2026,10,8,10,0,tzinfo=SHANGHAI),[duration,0],startup_lag=10,lane=lane)
    spacing = 230 if duration == 119 else 120
    assert (starts[1]-starts[0]).total_seconds() == spacing
    assert sleeps[1] == spacing-duration


@pytest.mark.parametrize('start,expected',[
    (datetime(2026,10,8,11,28,tzinfo=SHANGHAI),'2026-10-08 13:00'),
    (datetime(2026,10,9,15,8,tzinfo=SHANGHAI),'2026-10-12 09:30'),
])
def test_slow_round_waits_when_window_ends(monkeypatch,start,expected):
    starts,sleeps = simulate_scheduler(monkeypatch,start,[180,0])
    assert starts[1].strftime('%Y-%m-%d %H:%M') == expected
    assert sleeps[1] > 0
