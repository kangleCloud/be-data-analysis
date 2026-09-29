"""服务内北京时间定点采集调度。"""

import asyncio
import logging
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
SLOT_TIMES = (
    time(9, 40), time(10, 10), time(10, 40), time(11, 10),
    time(13, 10), time(13, 40), time(14, 10), time(14, 40), time(15, 30),
)
MAX_START_LAG_SECONDS = 60


def slot_label(at: datetime) -> str:
    """将手动与定点采集映射到当天最近的时段。"""
    local = at.astimezone(SHANGHAI)
    slot = max((value for value in SLOT_TIMES if value <= local.time()), default=None)
    return f"{local:%Y%m%d}:{slot:%H%M}" if slot else f"{local:%Y%m%d}:manual"


def next_slot(after: datetime) -> datetime:
    """返回严格晚于当前时间的下一个工作日时段；节假日由采集日历跳过。"""
    local = after.astimezone(SHANGHAI)
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for clock in SLOT_TIMES:
            candidate = datetime.combine(day, clock, tzinfo=SHANGHAI)
            if candidate > local:
                return candidate
    raise AssertionError("未找到后续采集时段")


async def launch_slot(slot: datetime, now: datetime) -> bool:
    """仅在时段刚到时启动一次性采集子进程。"""
    lag = (now - slot).total_seconds()
    if lag < 0 or lag > MAX_START_LAG_SECONDS:
        LOGGER.info("跳过错过的采集时段: %s", slot.isoformat())
        return False
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app", "collect")
    exit_code = await process.wait()
    LOGGER.info("采集时段 %s 子进程退出: %s", slot.isoformat(), exit_code)
    return True


async def run_scheduler() -> None:
    """只调度未来时段；进程内仅有一个循环。"""
    while True:
        now = datetime.now(SHANGHAI)
        slot = next_slot(now)
        await asyncio.sleep(max(0, (slot - now).total_seconds()))
        await launch_slot(slot, datetime.now(SHANGHAI))
