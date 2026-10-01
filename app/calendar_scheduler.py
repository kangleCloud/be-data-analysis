"""上海时间月初刷新共享交易日历，启动时补建缺失缓存。"""

import asyncio
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

from app.core.config import Settings
from app.workflows import calendar_service

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


def _bootstrap(settings: Settings) -> None:
    client = redis.Redis.from_url(
        settings.redis_url.get_secret_value(), decode_responses=True,
        socket_timeout=5, socket_connect_timeout=5,
    )
    try:
        now = datetime.now(SHANGHAI)
        status = calendar_service(settings, client).day_status(now.date(), now)
        LOGGER.info("交易日历启动检查: %s", "UNKNOWN" if status is None else status)
    finally:
        client.close()


async def run_calendar_scheduler(settings: Settings) -> None:
    try:
        await asyncio.to_thread(_bootstrap, settings)
    except Exception:
        LOGGER.exception("交易日历启动检查失败")
    while True:
        now = datetime.now(SHANGHAI)
        slot = next_monthly_slot(now)
        await asyncio.sleep(max(0, (slot - now).total_seconds()))
        if (datetime.now(SHANGHAI) - slot).total_seconds() > 60:
            LOGGER.info("跳过错过的交易日历月初时段: %s", slot.isoformat())
            continue
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app", "calendar-refresh"
        )
        code = await process.wait()
        LOGGER.info("交易日历月初刷新退出: %s", code)
