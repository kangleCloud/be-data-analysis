"""市场快照采集编排、交易时段约束和模块独立降级。"""

import logging
import math
import time as clock
from datetime import datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from app.normalize import normalize_individual_batch, normalize_sectors
from app.providers.akshare_market import MarketSource
from app.snapshot import SnapshotStore
from app.trading_calendar import CalendarService

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
MODULE_KEYS = ("industrySectors", "conceptSectors", "marketFundFlow")
SECTOR_FIELDS = {
    "code", "name", "type", "indexValue", "changePct", "inflow", "outflow",
    "netAmount", "netFlowRate", "companyCount", "leader", "leaderChangePct",
    "leaderPrice",
}
SECTOR_NUMBERS = {
    "indexValue", "changePct", "inflow", "outflow", "netAmount", "netFlowRate",
    "leaderChangePct", "leaderPrice",
}
MARKET_POINT_FIELDS = {"collectedAt", "inflow", "outflow", "netAmount"}
MARKET_LATEST_FIELDS = MARKET_POINT_FIELDS | {
    "riseCount", "fallCount", "flatCount", "stockCount",
}


class SourceCooldownError(RuntimeError):
    """源接口仍处于 Redis 共享冷却期。"""


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _reusable_prior(key: str, prior: Any) -> bool:
    """旧快照须符合新契约，避免把 Top5 或东财数据误当成可用历史。"""
    if not isinstance(prior, dict) or prior.get("status") not in {"FRESH", "STALE"}:
        return False
    if prior.get("tradeDateBasis") != "CALENDAR" or not all(
        isinstance(prior.get(field), str) for field in ("tradeDate", "lastSuccessAt")
    ):
        return False
    data = prior.get("data")
    if not isinstance(data, dict):
        return False
    if key == "marketFundFlow":
        latest, series = data.get("latest"), data.get("series")
        return (
            data.get("source") == "THS_INDIVIDUAL_AGGREGATE"
            and isinstance(latest, dict) and set(latest) == MARKET_LATEST_FIELDS
            and isinstance(latest["collectedAt"], str)
            and all(_finite(latest[field]) for field in MARKET_LATEST_FIELDS - {"collectedAt"})
            and isinstance(series, list)
            and all(
                isinstance(point, dict) and set(point) == MARKET_POINT_FIELDS
                and isinstance(point["collectedAt"], str)
                and all(_finite(point[field]) for field in MARKET_POINT_FIELDS - {"collectedAt"})
                for point in series
            )
        )
    sector_type = "industry" if key == "industrySectors" else "concept"
    items = data.get("items")
    return (
        data.get("source") == "THS" and data.get("period") == "INTRADAY"
        and isinstance(items, list) and bool(items)
        and all(
            isinstance(item, dict) and set(item) == SECTOR_FIELDS
            and isinstance(item["name"], str) and bool(item["name"].strip())
            and item["type"] == sector_type
            and (item["code"] is None or isinstance(item["code"], str))
            and (item["leader"] is None or isinstance(item["leader"], str))
            and (item["companyCount"] is None or (
                isinstance(item["companyCount"], int)
                and not isinstance(item["companyCount"], bool)
                and item["companyCount"] >= 0
            ))
            and all(item[field] is None or _finite(item[field]) for field in SECTOR_NUMBERS)
            for item in items
        )
    )


def _needs_cooldown(exc: Exception, source: str) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout, TimeoutError)):
        return True
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in {403, 429}
    return source == "ths" and isinstance(exc, (AttributeError, IndexError))


def _in_collection_window(at: datetime) -> bool:
    local = at.astimezone(SHANGHAI)
    now = local.time()
    return local.weekday() < 5 and (
        time(9, 30) <= now < time(11, 31)
        or time(13) <= now < time(15, 11)
    )


def _error_module(timestamp: str, message: str) -> dict[str, Any]:
    return {
        "status": "ERROR", "tradeDate": None, "tradeDateBasis": "CALENDAR",
        "lastSuccessAt": None, "lastAttemptAt": timestamp,
        "message": message, "data": None,
    }


def _fallback(key: str, prior: Any, timestamp: str, message: str) -> dict[str, Any]:
    if _reusable_prior(key, prior):
        return {**prior, "status": "STALE", "lastAttemptAt": timestamp, "message": message}
    return _error_module(timestamp, "暂无可用数据")


class MarketCollector:
    def __init__(self, provider: MarketSource, store: SnapshotStore,
                 calendar: CalendarService) -> None:
        self._provider = provider
        self._store = store
        self._calendar = calendar

    def collect(self, at: datetime, *, force: bool = False) -> str:
        """手动 force 只允许主动触发，不绕过交易日、时段、锁、间隔或冷却。"""
        if at.tzinfo is None:
            raise ValueError("采集时间必须带时区")
        local = at.astimezone(SHANGHAI)
        if not _in_collection_window(local):
            return "skipped"
        token = self._store.acquire()
        if token is None:
            return "locked"
        started = clock.monotonic()
        def collected_time() -> str:
            return (local + timedelta(seconds=clock.monotonic() - started)).isoformat(
                timespec="seconds"
            )
        try:
            self._store.start_renewal(token)
            if not self._store.reserve_slot(local):
                return "throttled"
            timestamp = local.isoformat(timespec="seconds")
            previous = self._store.load() or {}
            old_modules = previous.get("modules", {})
            try:
                trading_status = self._calendar.day_status(local.date(), local)
            except Exception as exc:
                LOGGER.warning("交易日历失败，异常 %s", type(exc).__name__)
                trading_status = None
            if trading_status is None:
                modules = {
                    key: _fallback(key, old_modules.get(key), timestamp, "交易日历未知，展示上次成功数据")
                    for key in MODULE_KEYS
                }
                self._publish(token, collected_time(), modules)
                return "partial"
            if not trading_status:
                return "skipped"
            day = local.date().isoformat()
            modules: dict[str, Any] = {}
            failures = 0
            fund_points: dict[str, dict[str, Any]] = {}

            def update(key: str, action: Callable[[], dict[str, Any]]) -> None:
                nonlocal failures
                module_started = clock.monotonic()
                try:
                    if self._store.cooldown_active("ths"):
                        raise SourceCooldownError("同花顺源冷却中")
                    data = action()
                    if key == "marketFundFlow":
                        data = self._append_market_series(data, old_modules.get(key), day)
                    success_at = collected_time()
                    modules[key] = {
                        "status": "FRESH", "tradeDate": day,
                        "tradeDateBasis": "CALENDAR", "lastSuccessAt": success_at,
                        "lastAttemptAt": timestamp, "message": None, "data": data,
                    }
                    LOGGER.info("模块 %s 成功，耗时 %.2f 秒", key, clock.monotonic() - module_started)
                except Exception as exc:
                    failures += 1
                    if _needs_cooldown(exc, "ths"):
                        self._store.start_cooldown("ths")
                    LOGGER.warning(
                        "模块 %s 失败，耗时 %.2f 秒，异常 %s",
                        key, clock.monotonic() - module_started, type(exc).__name__,
                    )
                    modules[key] = _fallback(
                        key, old_modules.get(key), timestamp, "本轮采集失败，展示上次成功数据"
                    )

            update("industrySectors", lambda: normalize_sectors(
                self._provider.sector_fund_flow("industry"), "industry"
            ))
            update("conceptSectors", lambda: normalize_sectors(
                self._provider.sector_fund_flow("concept"), "concept"
            ))
            def market_action() -> dict[str, Any]:
                nonlocal fund_points
                data, by_code = normalize_individual_batch(
                    self._provider.market_fund_flow(), collected_time()
                )
                selected = self._store.enabled_symbols()
                fund_points = {
                    symbol: by_code[symbol[2:]]
                    for symbol in selected if symbol[2:] in by_code
                }
                return data

            update("marketFundFlow", market_action)
            if modules["marketFundFlow"]["status"] != "FRESH":
                fund_points = {}
            self._publish(token, collected_time(), modules, day, fund_points)
            return "partial" if failures else "published"
        finally:
            self._store.stop_renewal()
            self._store.release(token)
            LOGGER.info("采集总耗时 %.2f 秒", clock.monotonic() - started)

    def _publish(
        self, token: str, timestamp: str, modules: dict[str, Any],
        trade_date: str | None = None, fund_points: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if not self._store.renew(token):
            raise RuntimeError("市场采集锁已失效，拒绝发布快照")
        self._store.save({
            "schemaVersion": 1, "provider": "akshare",
            "generatedAt": timestamp, "modules": modules,
        }, trade_date=trade_date, fund_points=fund_points)

    @staticmethod
    def _append_market_series(data: dict[str, Any], prior: Any, day: str) -> dict[str, Any]:
        point = data["series"][0]
        if _reusable_prior("marketFundFlow", prior) and prior["tradeDate"] == day:
            series = [
                {**old, "netAmount": old["inflow"] - old["outflow"]}
                for old in prior["data"]["series"]
                if old["collectedAt"] != point["collectedAt"]
            ]
            series.append(point)
            series.sort(key=lambda old: old["collectedAt"])
            data["series"] = series
        return data
