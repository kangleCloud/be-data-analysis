"""服务内北京时间定点采集调度。"""

import asyncio
import logging
import time as clock

from app.collector import _in_collection_window
from app.stock_monitor_scheduler import next_sample_slot
from app.core.config import get_settings
from app.core.logging import log_failure
from app.workflows import run_auto
import threading
import redis
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
SLOT_TIMES = tuple(
    time(hour, minute)
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60 + 10))
    for minute_of_day in range(start, end + 1, 2)
    for hour, minute in (divmod(minute_of_day, 60),)
)
MAX_START_LAG_SECONDS = 60
MIN_START_SECONDS = 120


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


def next_auto_slot(after: datetime) -> datetime:
    return min(next_slot(after),next_sample_slot(after))


def _round(settings,cancel):
    client = redis.Redis.from_url(settings.redis_url.get_secret_value(),decode_responses=True,
                                  socket_timeout=1,socket_connect_timeout=1)
    try:
        return run_auto(settings,client,cancel=cancel)
    finally:
        client.close()


async def run_scheduler(settings=None) -> None:
    """单服务父进程复用业务函数，轮内串行、跨轮至少120秒、长轮短任务优先。"""
    settings = settings or get_settings()
    last_started = last_monotonic = None
    continue_now = False
    cancel = threading.Event()
    try:
        while True:
            now = datetime.now(SHANGHAI)
            slot = now if continue_now and _in_collection_window(now) else next_auto_slot(now)
            if last_started is not None:
                earliest = last_started+timedelta(seconds=MIN_START_SECONDS)
                if slot < earliest:
                    slot = earliest if _in_collection_window(earliest) else next_auto_slot(earliest)
            delay = max(0,(slot-now).total_seconds())
            if last_monotonic is not None:
                delay = max(delay,MIN_START_SECONDS-(clock.monotonic()-last_monotonic))
            await asyncio.sleep(delay)
            actual = datetime.now(SHANGHAI)
            if (actual-slot).total_seconds() > MAX_START_LAG_SECONDS:
                continue_now = False
                continue
            last_started,last_monotonic = actual,clock.monotonic()
            work = asyncio.create_task(asyncio.to_thread(_round,settings,cancel))
            try:
                outcome = await asyncio.shield(work)
                LOGGER.info('串行行情轮完成: %s',outcome)
            except asyncio.CancelledError:
                cancel.set()
                try:
                    await work
                except Exception as exc:
                    log_failure(LOGGER,'auto-round-shutdown',exc)
                raise
            except Exception as exc:
                log_failure(LOGGER,'auto-round',exc)
            continue_now = clock.monotonic()-last_monotonic >= MIN_START_SECONDS
    finally:
        cancel.set()
