"""上海时间月初刷新共享交易日历，启动时补建缺失缓存。"""

import asyncio
import logging
import threading

from datetime import datetime
from zoneinfo import ZoneInfo

import redis

from app.core.config import Settings
from app.core.logging import log_failure
from app.workflows import calendar_service, run_calendar
from app.collection_gate import collection_entry

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def next_monthly_slot(after: datetime) -> datetime:
    local = after.astimezone(SHANGHAI)
    year, month = local.year, local.month
    candidate = datetime(year, month, 1, 0, 10, tzinfo=SHANGHAI)
    if candidate <= local:
        year += month // 12
        month = month % 12 + 1
        candidate = datetime(year, month, 1, 0, 10, tzinfo=SHANGHAI)
    return candidate


def _bootstrap(settings: Settings, cancel=None) -> None:
    client = redis.Redis.from_url(
        settings.redis_url.get_secret_value(), decode_responses=True,
        socket_timeout=5, socket_connect_timeout=5,
    )
    try:
        now = datetime.now(SHANGHAI)
        with collection_entry(client,cancel) as entered:
            if not entered:
                return
            status = calendar_service(settings, client).day_status(now.date(), now)
        LOGGER.info("交易日历启动检查: %s", "UNKNOWN" if status is None else status)
    finally:
        client.close()


async def run_calendar_scheduler(settings: Settings) -> None:
    try:
        await _run_cancellable(_bootstrap,settings)
    except Exception as exc:
        log_failure(LOGGER, "calendar-bootstrap", exc)
    while True:
        now = datetime.now(SHANGHAI)
        slot = next_monthly_slot(now)
        await asyncio.sleep(max(0, (slot - now).total_seconds()))
        if (datetime.now(SHANGHAI) - slot).total_seconds() > 60:
            LOGGER.info("跳过错过的交易日历月初时段: %s", slot.isoformat())
            continue
        try:
            await _run_cancellable(_refresh,settings)
        except Exception as exc:
            log_failure(LOGGER,'calendar-monthly',exc)


def _refresh(settings,cancel=None):
    client = redis.Redis.from_url(settings.redis_url.get_secret_value(),decode_responses=True,
                                 socket_timeout=1,socket_connect_timeout=1)
    try:
        with collection_entry(client,cancel) as entered:
            if entered:
                LOGGER.info('交易日历月初刷新: %s',run_calendar(settings,client))
    finally:
        client.close()


async def _run_cancellable(function,settings):
    """停服时取消当前日历源，并等待子进程回收。"""
    cancel = threading.Event()
    work = asyncio.create_task(asyncio.to_thread(function,settings,cancel))
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        cancel.set()
        try:
            await work
        except Exception as exc:
            log_failure(LOGGER,'calendar-shutdown',exc)
        raise
