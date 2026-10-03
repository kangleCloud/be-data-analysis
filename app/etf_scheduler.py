"""ETF 行情与市场同频、无重叠的服务内调度。"""

import asyncio
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduler import next_slot

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


async def run_etf_scheduler() -> None:
    while True:
        now = datetime.now(SHANGHAI)
        slot = next_slot(now)
        await asyncio.sleep(max(0, (slot - now).total_seconds()))
        if (datetime.now(SHANGHAI) - slot).total_seconds() > 60:
            LOGGER.info("跳过错过的 ETF 采样时段: %s", slot.isoformat())
            continue
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app", "etf-collect"
        )
        code = await process.wait()
        LOGGER.info("ETF 采样时段 %s 子进程退出: %s", slot.isoformat(), code)
