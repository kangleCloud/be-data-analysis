"""采集编排、交易时段约束和失败降级。"""

import logging
import math
import time as clock
from datetime import datetime, time
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from app.normalize import (
    normalize_market_fund_flow,
    normalize_top5,
)
from app.providers.akshare_market import MarketSource
from app.snapshot import SnapshotStore

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
MODULE_KEYS = (
    "industryTop5",
    "conceptTop5",
    "marketFundFlow",
)
TOP_LIST_NAMES = ("topRise", "topFall", "topInflow", "topOutflow")
TOP_DATA_FIELDS = {"source", "period", *TOP_LIST_NAMES}
TOP_ITEM_FIELDS = {"sectorName", "sectorType", "changePercent", "netFlowAmount"}


class SourceCooldownError(RuntimeError):
    """源接口仍处于 Redis 共享冷却期。"""


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _reusable_prior(key: str, prior: Any) -> bool:
    """旧快照只有符合当前 V1 读取契约时才能作为降级数据。"""
    if not isinstance(prior, dict) or prior.get("status") not in ("FRESH", "STALE"):
        return False
    if not isinstance(prior.get("tradeDate"), str) or not isinstance(
        prior.get("lastSuccessAt"), str
    ):
        return False
    if not all(
        field in prior and (prior[field] is None or isinstance(prior[field], str))
        for field in ("lastAttemptAt", "message")
    ):
        return False
    data = prior.get("data")
    if key == "marketFundFlow":
        return (prior.get("tradeDateBasis") == "SOURCE"
                and isinstance(data, dict)
                and isinstance(data.get("latest"), dict)
                and isinstance(data.get("series"), list)
                and len(data["series"]) <= 20)
    if prior.get("tradeDateBasis") != "CALENDAR" or not isinstance(data, dict):
        return False
    if (set(data) != TOP_DATA_FIELDS or data.get("source") != "THS"
            or data.get("period") != "INTRADAY"):
        return False
    sector_type = "industry" if key == "industryTop5" else "concept"
    for name in TOP_LIST_NAMES:
        entries = data[name]
        if not isinstance(entries, list) or len(entries) > 5:
            return False
        for item in entries:
            if (not isinstance(item, dict) or set(item) != TOP_ITEM_FIELDS
                    or not isinstance(item["sectorName"], str)
                    or not item["sectorName"].strip()
                    or item["sectorType"] != sector_type
                    or not _finite_number(item["changePercent"])
                    or not _finite_number(item["netFlowAmount"])):
                return False
    return True


def _needs_cooldown(exc: Exception, source: str) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout, TimeoutError)):
        return True
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in {403, 429}
    return source == "ths" and isinstance(exc, (AttributeError, IndexError))


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
        """运行一轮采集；返回 published、partial、skipped、locked 或 throttled。"""
        if at.tzinfo is None:
            raise ValueError("采集时间必须带时区")
        local = at.astimezone(SHANGHAI)
        if not force and not _in_collection_window(local):
            return "skipped"
        token = self._store.acquire()
        if token is None:
            return "locked"
        started = clock.monotonic()
        try:
            if not self._store.reserve_slot(local):
                return "throttled"
            try:
                calendar_started = clock.monotonic()
                if self._store.cooldown_active("sina"):
                    raise SourceCooldownError("交易日历源冷却中")
                trading_date = self._provider.latest_trading_date(local.date())
                LOGGER.info("交易日历模块耗时 %.2f 秒", clock.monotonic() - calendar_started)
            except Exception as exc:
                if _needs_cooldown(exc, "sina"):
                    self._store.start_cooldown("sina")
                LOGGER.warning(
                    "交易日历获取失败，耗时 %.2f 秒，异常 %s",
                    clock.monotonic() - calendar_started, type(exc).__name__,
                )
                previous = self._store.load() or {}
                timestamp = local.isoformat(timespec="seconds")
                modules = {}
                for key in MODULE_KEYS:
                    prior = previous.get("modules", {}).get(key)
                    if _reusable_prior(key, prior):
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
                    "schemaVersion": 1,
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
                source: str,
            ) -> Any | None:
                nonlocal failures
                module_started = clock.monotonic()
                try:
                    if self._store.cooldown_active(source):
                        raise SourceCooldownError("源接口冷却中")
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
                    LOGGER.info("模块 %s 成功，耗时 %.2f 秒", key, clock.monotonic() - module_started)
                    return data
                except Exception as exc:
                    failures += 1
                    if _needs_cooldown(exc, source):
                        self._store.start_cooldown(source)
                    LOGGER.warning(
                        "模块 %s 失败，耗时 %.2f 秒，异常 %s",
                        key, clock.monotonic() - module_started, type(exc).__name__,
                    )
                    prior = old_modules.get(key)
                    if _reusable_prior(key, prior):
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
                    f"{sector_type}Top5",
                    lambda kind=sector_type: normalize_top5(
                        self._provider.sector_fund_flow(kind), kind
                    ),
                    source_date=day,
                    source="ths",
                )

            update(
                "marketFundFlow",
                lambda: normalize_market_fund_flow(self._provider.market_fund_flow()),
                source_date_basis="SOURCE",
                source="eastmoney",
            )
            self._store.save({
                "schemaVersion": 1,
                "provider": "akshare",
                "generatedAt": timestamp,
                "modules": modules,
            })
            return "partial" if failures else "published"
        finally:
            self._store.release(token)
            LOGGER.info("采集总耗时 %.2f 秒", clock.monotonic() - started)
