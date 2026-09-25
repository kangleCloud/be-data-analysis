"""采集编排、交易时段约束和失败降级。"""

import logging
from datetime import datetime, time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.normalize import (
    normalize_market_fund_flow,
    normalize_sectors,
    normalize_top5,
)
from app.providers.akshare_market import MarketSource
from app.snapshot import SnapshotStore

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
MODULE_KEYS = (
    "industryHeatmap",
    "conceptHeatmap",
    "industryTop5",
    "conceptTop5",
    "marketFundFlow",
)


def _in_collection_window(at: datetime) -> bool:
    local = at.astimezone(SHANGHAI)
    clock = local.time()
    return local.weekday() < 5 and (
        time(9, 30) <= clock <= time(11, 30)
        or time(13) <= clock <= time(16)
    )


class MarketCollector:
    def __init__(self, provider: MarketSource, store: SnapshotStore) -> None:
        self._provider = provider
        self._store = store

    def collect(self, at: datetime, *, force: bool = False) -> str:
        """运行一轮采集；返回 published、partial、skipped 或 locked。"""
        if at.tzinfo is None:
            raise ValueError("采集时间必须带时区")
        local = at.astimezone(SHANGHAI)
        if not force and not _in_collection_window(local):
            return "skipped"
        token = self._store.acquire()
        if token is None:
            return "locked"
        try:
            try:
                trading_date = self._provider.latest_trading_date(local.date())
            except Exception as exc:
                LOGGER.warning("交易日历获取失败: %s", type(exc).__name__)
                previous = self._store.load() or {}
                timestamp = local.isoformat(timespec="seconds")
                modules = {}
                for key in MODULE_KEYS:
                    prior = previous.get("modules", {}).get(key)
                    if prior and prior.get("lastSuccessAt"):
                        modules[key] = {
                            **prior,
                            "status": "STALE",
                            "lastAttemptAt": timestamp,
                            "message": "交易日历获取失败，展示上次成功数据",
                        }
                    else:
                        modules[key] = {
                            "status": "ERROR",
                            "tradeDate": None,
                            "tradeDateBasis": "SOURCE" if key == "marketFundFlow" else "CALENDAR",
                            "lastSuccessAt": None,
                            "lastAttemptAt": timestamp,
                            "message": "交易日历获取失败，暂无可用数据",
                            "data": None,
                        }
                self._store.save({
                    "schemaVersion": 2,
                    "provider": "akshare",
                    "generatedAt": timestamp,
                    "modules": modules,
                })
                return "partial"
            if trading_date is None or (not force and trading_date != local.date()):
                return "skipped"
            timestamp = local.isoformat(timespec="seconds")
            previous = self._store.load() or {}
            old_modules = previous.get("modules", {})
            modules: dict[str, Any] = {}
            failures = 0

            def update(
                key: str,
                action: Callable[[], Any],
                *,
                source_date: str | None = None,
                source_date_basis: str = "CALENDAR",
            ) -> Any | None:
                nonlocal failures
                try:
                    result = action()
                    if isinstance(result, tuple):
                        actual_date, data = result
                    else:
                        actual_date, data = source_date, result
                    modules[key] = {
                        "status": "FRESH",
                        "tradeDate": actual_date,
                        "tradeDateBasis": source_date_basis,
                        "lastSuccessAt": timestamp,
                        "lastAttemptAt": timestamp,
                        "message": None,
                        "data": data,
                    }
                    return data
                except Exception as exc:
                    failures += 1
                    LOGGER.warning("%s 采集失败: %s", key, type(exc).__name__)
                    prior = old_modules.get(key)
                    if prior and prior.get("lastSuccessAt"):
                        modules[key] = {
                            **prior,
                            "status": "STALE",
                            "lastAttemptAt": timestamp,
                            "message": "本轮采集失败，展示上次成功数据",
                        }
                    else:
                        modules[key] = {
                            "status": "ERROR",
                            "tradeDate": None,
                            "tradeDateBasis": source_date_basis,
                            "lastSuccessAt": None,
                            "lastAttemptAt": timestamp,
                            "message": "暂无可用数据",
                            "data": None,
                        }
                    return None

            day = trading_date.isoformat()
            for sector_type in ("industry", "concept"):
                update(
                    f"{sector_type}Heatmap",
                    lambda kind=sector_type: normalize_sectors(
                        self._provider.sector_quotes(kind), kind
                    ),
                    source_date=day,
                )
                update(
                    f"{sector_type}Top5",
                    lambda kind=sector_type: normalize_top5(
                        self._provider.sector_fund_flow(kind), kind
                    ),
                    source_date=day,
                )

            update(
                "marketFundFlow",
                lambda: normalize_market_fund_flow(self._provider.market_fund_flow()),
                source_date_basis="SOURCE",
            )
            self._store.save({
                "schemaVersion": 2,
                "provider": "akshare",
                "generatedAt": timestamp,
                "modules": modules,
            })
            return "partial" if failures else "published"
        finally:
            self._store.release(token)
