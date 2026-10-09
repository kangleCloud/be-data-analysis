"""慢采集子进程等待完成，只启动后续时段，不补跑或重叠。"""

import asyncio
from datetime import datetime, timedelta
from importlib import import_module
from zoneinfo import ZoneInfo
import pytest


@pytest.mark.parametrize("module_name,function_name,command", [
    ("app.scheduler", "run_scheduler", "collect"),
    ("app.etf_scheduler", "run_etf_scheduler", "etf-collect"),
    ("app.stock_monitor_scheduler", "run_monitor_scheduler", "monitor-sample"),
])
def test_slow_child_skips_missed_slots_without_overlap(monkeypatch, module_name, function_name, command):
    module = import_module(module_name)
    clock = [datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
    starts = []
    running = [False]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    class FinishedSimulation(Exception):
        pass
    async def sleep(seconds):
        assert not running[0]
        clock[0] += timedelta(seconds=seconds)
    class Process:
        async def wait(self):
            assert running[0]
            # 三分钟任务覆盖一个两分钟时段，下一次只能等未来时段。
            clock[0] += timedelta(minutes=3)
            running[0] = False
            if len(starts) == 2:
                raise FinishedSimulation()
            return 0
    async def create(*args):
        assert not running[0]
        assert args[-1] == command
        starts.append(clock[0].strftime("%H:%M"))
        running[0] = True
        return Process()
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    with pytest.raises(FinishedSimulation):
        asyncio.run(getattr(module, function_name)())
    assert starts == ["10:02", "10:06"]
    assert not running[0]
