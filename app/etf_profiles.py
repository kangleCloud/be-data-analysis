"""同花顺 ETF 资料批次的独立锁、频率和总预算。"""

import logging
import time
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.etf_normalize import ths_profile

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
LOCK_KEY = "stock:etf-monitor:v1:profiles:python:lock"
INTERVAL_PREFIX = "stock:etf-monitor:v1:profiles:python:min-interval:"
BATCH_SECONDS = 180
LOCK_SECONDS = 210
INTERVAL_SECONDS = 1800
REQUEST_INTERVAL_SECONDS = 2
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class ProfileBatchError(RuntimeError):
    def __init__(self, status_code: int, message: str,
                 states: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.states = states or {}


def collect_profiles(source: Any, client: Any,
                     symbols: list[str]) -> dict[str, Any]:
    """单批串行同步；此锁由 Python 持有，不使用 Java 整体刷新锁。"""
    token = uuid4().hex
    if not client.set(LOCK_KEY, token, nx=True, ex=LOCK_SECONDS):
        raise ProfileBatchError(409, "ETF 资料批次正在运行")
    try:
        deadline = time.monotonic() + BATCH_SECONDS
        last_call: float | None = None
        states: dict[str, str] = {}
        profiles = []
        errors = 0
        budget_exhausted = False
        for symbol in symbols:
            if last_call is not None:
                delay = max(0, REQUEST_INTERVAL_SECONDS - (time.monotonic() - last_call))
                if time.monotonic() + delay + 2 >= deadline:
                    states[symbol] = "SKIPPED"
                    budget_exhausted = True
                    continue
                time.sleep(delay)
            remaining = deadline - time.monotonic()
            if remaining <= 2:
                states[symbol] = "SKIPPED"
                budget_exhausted = True
                continue
            interval_key = f"{INTERVAL_PREFIX}{symbol}"
            if not client.set(interval_key, token, nx=True,
                              ex=INTERVAL_SECONDS):
                states[symbol] = "SKIPPED"
                continue
            if deadline - time.monotonic() <= 2:
                client.eval(RELEASE_SCRIPT, 1, interval_key, token)
                states[symbol] = "SKIPPED"
                budget_exhausted = True
                continue
            last_call = time.monotonic()
            try:
                rows = source.profile(symbol[2:], budget_seconds=deadline - last_call)
                collected_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
                profiles.append(ths_profile(rows, symbol, collected_at))
                states[symbol] = "OK"
            except Exception as exc:
                errors += 1
                states[symbol] = "ERROR"
                LOGGER.warning("同花顺 ETF 资料失败，异常 %s", type(exc).__name__)
        if not profiles:
            if not errors and not budget_exhausted:
                raise ProfileBatchError(429, "ETF 资料刷新未满足 30 分钟间隔", states)
            raise ProfileBatchError(502, "ETF 资料未取得有效结果，保留原资料", states)
        return {
            "schemaVersion": 1, "source": "THS",
            "collectedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "profiles": profiles, "sourceStatus": states,
        }
    finally:
        client.eval(RELEASE_SCRIPT, 1, LOCK_KEY, token)
