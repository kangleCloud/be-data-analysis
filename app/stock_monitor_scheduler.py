"""个股监控每两分钟盘中采样调度。"""

import asyncio
import logging
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
SAMPLE_TIMES = tuple(
    time(hour, minute)
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
    for minute_of_day in range(start, end + 1, 2)
    for hour, minute in (divmod(minute_of_day, 60),)
) + tuple(time(15, minute) for minute in (2, 4, 6, 8, 10))


def next_sample_slot(after: datetime) -> datetime:
    local = after.astimezone(SHANGHAI)
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for slot in SAMPLE_TIMES:
            candidate = datetime.combine(day, slot, tzinfo=SHANGHAI)
            if candidate > local:
                return candidate
    raise AssertionError("未找到后续个股采样时段")


async def run_monitor_scheduler() -> None:
    while True:
        now = datetime.now(SHANGHAI)
        slot = next_sample_slot(now)
        await asyncio.sleep(max(0, (slot - now).total_seconds()))
        if (datetime.now(SHANGHAI) - slot).total_seconds() > 60:
            LOGGER.info("跳过错过的个股采样时段: %s", slot.isoformat())
            continue
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app", "monitor-sample")
        code = await process.wait()
        LOGGER.info("个股采样时段 %s 子进程退出: %s", slot.isoformat(), code)
