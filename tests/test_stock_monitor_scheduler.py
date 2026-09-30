"""雪球采样开关与盘中两分钟时段。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app.main as main_module
import app.cli as cli_module
from app.core.config import load_settings
from app.stock_monitor_scheduler import next_sample_slot


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_next_sample_slot_skips_lunch_and_weekend():
    assert next_sample_slot(datetime(2026, 9, 28, 9, 31, tzinfo=SHANGHAI)).strftime("%H:%M") == "09:32"
    assert next_sample_slot(datetime(2026, 9, 28, 11, 30, tzinfo=SHANGHAI)).strftime("%H:%M") == "13:00"
    assert next_sample_slot(datetime(2026, 9, 25, 15, 1, tzinfo=SHANGHAI)).strftime("%Y-%m-%d %H:%M") == "2026-09-25 15:02"
    assert next_sample_slot(datetime(2026, 9, 25, 15, 10, tzinfo=SHANGHAI)).strftime("%Y-%m-%d %H:%M") == "2026-09-28 09:30"


def test_serve_starts_monitor_scheduler_only_when_enabled(monkeypatch):
    events = []

    async def fake_market():
        import asyncio
        await asyncio.Event().wait()

    async def fake_monitor():
        import asyncio
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    monkeypatch.setattr(main_module, "run_scheduler", fake_market)
    monkeypatch.setattr(main_module, "run_monitor_scheduler", fake_monitor)
    with TestClient(main_module.create_app(settings=load_settings({}))) as client:
        assert client.get("/health").status_code == 200
    assert events == []
    with TestClient(main_module.create_app(settings=load_settings({"STOCK_MONITOR_XQ_ENABLED": "true"}))) as client:
        assert client.get("/health").status_code == 200
    assert events == ["started", "stopped"]


def test_manual_monitor_sample_does_not_construct_xueqiu_when_disabled(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["app", "monitor-sample"])
    monkeypatch.setattr(cli_module, "get_settings", lambda: load_settings({}))
    monkeypatch.setattr(cli_module, "XueqiuProvider", lambda *_args: (_ for _ in ()).throw(
        AssertionError("关闭开关时不得创建雪球客户端")
    ))
    assert cli_module.main() == 0
