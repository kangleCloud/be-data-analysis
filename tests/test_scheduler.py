"""服务内时段调度和一次性子进程。"""

import asyncio
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app.main as main_module
from app.scheduler import launch_slot, next_slot, slot_label

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_next_slot_skips_missed_times_and_weekend():
    at = datetime(2026, 9, 25, 10, 5, tzinfo=SHANGHAI)
    assert next_slot(at).strftime("%Y-%m-%d %H:%M") == "2026-09-25 10:10"
    assert next_slot(at.replace(minute=10)).strftime("%H:%M") == "10:40"
    assert next_slot(at.replace(hour=16)).strftime("%Y-%m-%d %H:%M") == "2026-09-28 09:40"
    assert slot_label(at) == "20260925:0940"
    assert slot_label(at.replace(hour=9, minute=35)) == "20260925:manual"


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
    with TestClient(main_module.create_app()) as client:
        assert client.get("/health").status_code == 200
    assert events == ["start", "stop"]
