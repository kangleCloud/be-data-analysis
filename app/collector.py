"""市场快照采集编排、交易时段约束和模块独立降级。"""

import logging
import math
import time as clock
from datetime import datetime, time, timedelta
from typing import Any
from contextlib import closing
from zoneinfo import ZoneInfo

from app.normalize import SourceDataError, normalize_core_indices, normalize_individual_batch, normalize_sectors
from app.core.logging import log_failure, redis_failure_kind
from app.providers.akshare_market import MarketSource
from app.providers.http import error_metadata
from app.source_execution import SourceControlError, SourceCoolingError, SourceNotStartedError, completed, source_batch, market_cooldown
from app.snapshot import LOCK_KEY
from app.snapshot import SnapshotStore
from app.trading_calendar import CalendarService

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
MODULE_KEYS = ("industrySectors", "conceptSectors", "marketFundFlow", "coreIndices")
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
    if key == "coreIndices":
        items = data.get("items")
        return (
            data.get("source") == "SINA_INDEX"
            and isinstance(items, list) and len(items) == 5
            and all(isinstance(item, dict) and isinstance(item.get("code"), str)
                    and _finite(item.get("price")) and isinstance(item.get("series"), list)
                    for item in items)
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
                if isinstance(exc, SourceControlError) or redis_failure_kind(exc):
                    raise
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
            modules = {key: old_modules.get(key) if _reusable_prior(key, old_modules.get(key))
                       else _error_module(timestamp, "暂无可用数据") for key in MODULE_KEYS}
            failures = 0
            actions = [
                ("marketFundFlow", "ths", self._provider.market_fund_flow),
                ("industrySectors", "ths", lambda: self._provider.sector_fund_flow("industry")),
                ("conceptSectors", "ths", lambda: self._provider.sector_fund_flow("concept")),
                ("coreIndices", "sina", self._provider.index_spot),
            ]
            def fetch(key: str, group: str, function: Any) -> Any:
                source = "ths" if group == "ths" else "sina-index"
                for scope in (source, f"module:{key}"):
                    if self._store.cooldown_active(scope):
                        raise SourceCoolingError(self._store.cooldown_remaining(scope))
                return function()
            guarded = [(key, group, lambda k=key,g=group,f=function: fetch(k,g,f))
                       for key,group,function in actions]
            with source_batch(self._provider, (LOCK_KEY, token), started+1380), closing(
                completed(guarded, source=self._provider)
            ) as results:
                for key, frame, error, finished_at in results:
                    source = "sina-index" if key == "coreIndices" else "ths"
                    fund_points = None
                    try:
                        if error:
                            raise error
                        source_finished = (local + timedelta(seconds=finished_at-started)).isoformat(timespec="seconds")
                        if key in {"industrySectors", "conceptSectors"}:
                            data = normalize_sectors(frame, "industry" if key == "industrySectors" else "concept")
                        elif key == "marketFundFlow":
                            data, by_code = normalize_individual_batch(frame, source_finished)
                            selected = self._store.enabled_symbols()
                            fund_points = {symbol: by_code[symbol[2:]] for symbol in selected if symbol[2:] in by_code}
                            data = self._append_market_series(data, old_modules.get(key), day)
                        else:
                            data = self._append_index_series(normalize_core_indices(frame, source_finished),
                                                             old_modules.get(key), day)
                        modules[key] = {"status": "FRESH", "tradeDate": day, "tradeDateBasis": "CALENDAR",
                                        "lastSuccessAt": source_finished, "lastAttemptAt": timestamp,
                                        "message": None, "data": data}
                        LOGGER.info("模块 %s 成功，批次已耗时 %.2f 秒", key, clock.monotonic()-started)
                    except SourceNotStartedError:
                        failures += 1
                        LOGGER.info("模块 %s 预算结束，未发起源请求", key)
                        modules[key] = _fallback(key, old_modules.get(key), timestamp,
                                                 "本轮源预算结束，展示上次成功数据")
                    except SourceCoolingError as exc:
                        failures += 1
                        LOGGER.info("模块 %s 冷却跳过，剩余 TTL %d 秒", key, exc.ttl)
                        modules[key] = _fallback(key, old_modules.get(key), timestamp,
                                                 "数据源冷却中，展示上次成功数据")
                    except Exception as exc:
                        if isinstance(exc, SourceControlError) or redis_failure_kind(exc):
                            raise
                        failures += 1
                        shared, seconds = market_cooldown(error_metadata(exc))
                        self._store.start_cooldown(source if shared else f"module:{key}", seconds=seconds)
                        if not isinstance(exc, SourceDataError) and error_metadata(exc)["category"] == "UNEXPECTED":
                            log_failure(LOGGER, key, exc)
                        else:
                            LOGGER.warning("模块 %s 失败，异常 %s，诊断 %s", key, type(exc).__name__,
                                           exc.diagnostic() if isinstance(exc, SourceDataError) else error_metadata(exc))
                        modules[key] = _fallback(key, old_modules.get(key), timestamp,
                                                 "本轮采集失败，展示上次成功数据")
                        fund_points = None
                    # 每个完成模块仅发布一次；尚未完成模块保持原数据/时间。
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

    @staticmethod
    def _append_index_series(data: dict[str, Any], prior: Any, day: str) -> dict[str, Any]:
        if not _reusable_prior("coreIndices", prior) or prior["tradeDate"] != day:
            return data
        old_by_code = {item["code"]: item for item in prior["data"]["items"]}
        for item in data["items"]:
            previous = old_by_code.get(item["code"])
            if previous is None:
                continue
            point = item["series"][0]
            series = [old for old in previous["series"]
                      if old.get("collectedAt") != point["collectedAt"]]
            series.append(point)
            series.sort(key=lambda old: old["collectedAt"])
            item["series"] = series
        return data
