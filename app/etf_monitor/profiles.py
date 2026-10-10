"""同花顺 ETF 资料批次的独立锁、频率和总预算。"""

import logging
import time
from datetime import datetime
from typing import Any
from contextlib import closing
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.runtime.resources import SourceResourceError
from app.etf_monitor.normalize import ths_profile
from app.runtime.source_execution import completed, source_batch, SourceNotStartedError

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
LOCK_KEY = "stock:etf-monitor:v1:profiles:python:lock"
INTERVAL_PREFIX = "stock:etf-monitor:v1:profiles:python:min-interval:"
BATCH_SECONDS = 180
LOCK_SECONDS = 210
INTERVAL_SECONDS = 1800
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
                     symbols: list[str], *, mode: str = "auto") -> dict[str, Any]:
    """单批串行同步；此锁由 Python 持有，不使用 Java 整体刷新锁。"""
    token = uuid4().hex
    if mode == "auto" and not client.set(LOCK_KEY, token, nx=True, ex=LOCK_SECONDS):
        raise ProfileBatchError(409, "ETF 资料批次正在运行")
    try:
        deadline = time.monotonic() + BATCH_SECONDS
        states: dict[str, str] = {}
        profiles_by_symbol = {}
        errors = 0
        budget_exhausted = False
        def fetch(symbol: str) -> Any:
            remaining = deadline-time.monotonic()
            if remaining <= 2:
                return None
            interval_key = f"{INTERVAL_PREFIX}{symbol}"
            if mode == "auto" and not client.set(interval_key, token, nx=True, ex=INTERVAL_SECONDS):
                return None
            remaining = deadline-time.monotonic()
            if remaining <= 2:
                if mode == "auto":
                    client.eval(RELEASE_SCRIPT, 1, interval_key, token)
                return None
            try:
                return source.profile(symbol[2:], budget_seconds=remaining)
            except SourceNotStartedError:
                if mode == "auto":
                    client.eval(RELEASE_SCRIPT, 1, interval_key, token)
                raise
        actions = [(symbol, "ths", lambda code=symbol: fetch(code)) for symbol in symbols]
        with source_batch(source, (LOCK_KEY, token) if mode == "auto" else None, deadline), closing(
            completed(actions, source=source)
        ) as results:
            for symbol, rows, error, finished_at in results:
                if rows is None and error is None:
                    states[symbol] = "SKIPPED"
                    budget_exhausted |= deadline-time.monotonic() <= 2
                    continue
                if isinstance(error, SourceNotStartedError):
                    states[symbol] = "SKIPPED"
                    budget_exhausted = True
                    LOGGER.info("ETF 资料 %s 预算结束，未发起源请求", symbol)
                    continue
                if isinstance(error,SourceResourceError):
                    raise error
                try:
                    if error:
                        raise error
                    collected_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
                    profiles_by_symbol[symbol] = ths_profile(rows, symbol, collected_at)
                    states[symbol] = "OK"
                except Exception as exc:
                    errors += 1
                    states[symbol] = "ERROR"
                    LOGGER.warning("同花顺 ETF 资料失败，异常 %s", type(exc).__name__)
        states = {symbol: states[symbol] for symbol in symbols}
        profiles = [profiles_by_symbol[symbol] for symbol in symbols if symbol in profiles_by_symbol]
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
        if mode == "auto":
            client.eval(RELEASE_SCRIPT, 1, LOCK_KEY, token)
