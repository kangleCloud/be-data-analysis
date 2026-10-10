"""跨语言 Redis JSON 快照存储。"""

import json
import redis
from app.runtime.gates import ACQUIRE, ENTRY_KEYS, entry_guard
import logging
import threading
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from app.stock_monitor.events import monitor_event_lock
from app.runtime.cooldown import remaining
from app.core.logging import log_failure

SNAPSHOT_KEY = "stock:market:v1:snapshot"
UPDATES_CHANNEL = "stock:market:v1:updates"
FUND_SERIES_PREFIX = "stock:monitor:v1:fund-series:"
FUND_DATES_KEY = "stock:monitor:v1:fund-series:dates"
FUND_SYMBOLS_PREFIX = "stock:monitor:v1:fund-series:symbols:"
LOCK_KEY = "stock:market:v1:lock"
MIN_INTERVAL_KEY = "stock:market:v1:min-interval"
COOLDOWN_KEY_PREFIX = "stock:market:v1:cooldown:"
MIN_INTERVAL_SECONDS = 120
LOGGER = logging.getLogger(__name__)
RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
RENEW_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class SnapshotStore(Protocol):
    def acquire(self) -> str | None: ...

    def release(self, token: str) -> None: ...

    def start_renewal(self, token: str) -> None: ...

    def stop_renewal(self) -> None: ...

    def renew(self, token: str) -> bool: ...

    def load(self) -> dict[str, Any] | None: ...

    def save(self, snapshot: dict[str, Any], *, trade_date: str | None = None,
             fund_points: dict[str, dict[str, Any]] | None = None) -> None: ...

    def enabled_symbols(self) -> list[str]: ...

    def reserve_slot(self, at: datetime) -> bool: ...

    def cooldown_active(self, source: str) -> bool: ...

    def cooldown_remaining(self, source: str) -> int: ...


def _fund_state(module):
    if not isinstance(module,dict):
        return None
    # 仅重试时刻变化不改变资金状态，不重复唤醒；真实结果/日期/诊断变化则通知。
    return tuple(module.get(key) for key in ('status','tradeDate','lastSuccessAt','message'))


def reusable_module(key, module):
    from app.market.collector import _reusable_prior
    return _reusable_prior(key, module)


def undated_module(key, module):
    """没有可靠交易日期的刷新只提供快照；旧降级数据也不能带日内曲线。"""
    data = module.get("data")
    if isinstance(data, dict):
        if key == "marketFundFlow":
            data = {**data, "series": []}
        elif key == "coreIndices":
            data = {**data, "items": [{**item, "series": []} for item in data.get("items", [])]}
    return {**module, "tradeDate": None, "data": data}


def merge_series(key, old, incoming):
    if not isinstance(old, dict) or not isinstance(incoming, dict):
        return incoming
    def points(previous, current):
        merged = {point["collectedAt"]: point for point in previous}
        merged.update({point["collectedAt"]: point for point in current})
        return sorted(merged.values(), key=lambda point: point["collectedAt"])
    if old.get("source") != incoming.get("source"):
        return incoming
    if key == "marketFundFlow":
        series = points(old.get("series", []), incoming.get("series", []))
        return {**incoming, "series": [{**point, "netAmount": point["inflow"] - point["outflow"]} for point in series]}
    if key == "coreIndices":
        prior = {item["code"]: item for item in old.get("items", [])}
        return {**incoming, "items": [{**item, "series": points(prior.get(item["code"], {}).get("series", []), item.get("series", []))} for item in incoming.get("items", [])]}
    return incoming


class RedisSnapshotStore:
    """以 Redis 事务发布快照、资金点和通知；保留旧成功数据供降级。"""

    def __init__(self, client: Any, lock_seconds: int, *, lane: str = "market") -> None:
        self._client = client
        self.lane = lane
        self.lock_key = LOCK_KEY if lane == "market" else ENTRY_KEYS[lane][0]
        self._lock_seconds = lock_seconds
        self._renew_stop = threading.Event()
        self._renew_thread: threading.Thread | None = None

    def acquire(self) -> str | None:
        if self.lane != "market":
            return entry_guard(self.lane)[1]
        token = uuid4().hex
        acquired = self._client.set(LOCK_KEY, token, nx=True, ex=self._lock_seconds)
        return token if acquired else None

    def release(self, token: str) -> None:
        if self.lane != "market":
            return
        self._client.eval(RELEASE_LOCK_SCRIPT, 1, LOCK_KEY, token)

    def renew(self, token: str) -> bool:
        if self.lane != "market":
            return self._client.get(self.lock_key) == token
        return bool(self._client.eval(RENEW_LOCK_SCRIPT, 1, LOCK_KEY, token, self._lock_seconds))

    def start_renewal(self, token: str) -> None:
        if self.lane != "market":
            return
        self._renew_stop.clear()

        def keep_alive() -> None:
            interval = max(1, min(30, self._lock_seconds / 3))
            while not self._renew_stop.wait(interval):
                try:
                    if not self.renew(token):
                        LOGGER.error("市场采集锁已失效，停止续租")
                        return
                except Exception as exc:
                    log_failure(LOGGER, "market-lock-renewal", exc)
                    return

        self._renew_thread = threading.Thread(target=keep_alive, daemon=True)
        self._renew_thread.start()

    def stop_renewal(self) -> None:
        self._renew_stop.set()
        if self._renew_thread is not None:
            self._renew_thread.join(timeout=2)
            self._renew_thread = None

    def reserve_slot(self, at: datetime) -> bool:
        """Redis 原子占位，手动采集也遵守两分钟间隔。"""
        keys = [f"stock:source-control:v1:interval:{lane}" for lane in
                (("quotes","funds") if self.lane == "market" else (self.lane,))]
        if self.lane == "market":
            keys.append(MIN_INTERVAL_KEY)
        return bool(self._client.eval(ACQUIRE,len(keys),*keys,"1",MIN_INTERVAL_SECONDS))


    def cooldown_active(self, source: str) -> bool:
        return remaining(self._client, (f"{COOLDOWN_KEY_PREFIX}{source}",), "sina" if source in {"sina-index", "module:coreIndices"} else "ths", "auto") is not None

    def cooldown_remaining(self, source: str) -> int:
        return remaining(self._client, (f"{COOLDOWN_KEY_PREFIX}{source}",), "sina" if source in {"sina-index", "module:coreIndices"} else "ths", "auto") or 0

    def load(self) -> dict[str, Any] | None:
        raw = self._client.get(SNAPSHOT_KEY)
        if raw is None:
            return None
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
            raise ValueError("Redis 快照版本不受支持")
        return snapshot

    def enabled_symbols(self) -> list[str]:
        from app.stock_monitor.service import MonitorStore

        return [stock["symbol"] for stock in MonitorStore(self._client).enabled()]

    def save(self, snapshot: dict[str, Any], *, trade_date: str | None = None,
             fund_points: dict[str, dict[str, Any]] | None = None) -> None:
        if fund_points or 'marketFundFlow' in (snapshot.get('modules') or {}):
            with monitor_event_lock(self._client):
                self._save_with_retry(snapshot,trade_date,fund_points)
        else:
            self._save_with_retry(snapshot,trade_date,fund_points)

    def _save_with_retry(self,snapshot,trade_date,fund_points):
        for attempt in range(4):
            pipe = self._client.pipeline(transaction=True)
            try:
                pipe.watch(SNAPSHOT_KEY)
                self._save_transaction(pipe,snapshot,trade_date=trade_date,fund_points=fund_points)
                return
            except redis.WatchError:
                LOGGER.info('市场快照冲突，重新合并 attempt=%d/4',attempt+1)
            finally:
                pipe.reset()
        raise RuntimeError('市场快照并发冲突重试3次后仍失败')

    def _save_transaction(self, pipe, snapshot: dict[str, Any], *, trade_date: str | None = None,
                          fund_points: dict[str, dict[str, Any]] | None = None) -> None:
        """WATCH读取最新快照，合并本次模块；资金点与两类通知仍同事务。"""
        if snapshot.get("schemaVersion") != 1 or not isinstance(
            snapshot.get("generatedAt"), str
        ):
            raise ValueError("Redis 快照字段无效")
        previous_raw = self._client.get(SNAPSHOT_KEY)
        previous = json.loads(previous_raw) if previous_raw else {}
        if not isinstance(previous, dict):
            raise ValueError("Redis 旧快照格式无效")
        previous_id = previous.get("snapshotId")
        if not isinstance(previous_id, str):
            previous_id = None
        patch_modules = snapshot.get("modules") or {}
        old_modules = previous.get("modules") or {}
        patch_modules = dict(patch_modules)
        for key, incoming in list(patch_modules.items()):
            prior = old_modules.get(key)
            if not isinstance(prior, dict) or not isinstance(incoming, dict):
                continue
            if incoming.get("status") == "FRESH":
                if (prior.get("lastSuccessAt") or "") > (incoming.get("lastSuccessAt") or ""):
                    patch_modules[key] = ({**prior, "data": merge_series(key, incoming.get("data"), prior.get("data"))}
                                          if incoming.get("tradeDate") and prior.get("tradeDate") == incoming.get("tradeDate") else prior)
                    continue
                if incoming.get("tradeDate") and prior.get("tradeDate") == incoming.get("tradeDate"):
                    incoming = {**incoming, "data": merge_series(key, prior.get("data"), incoming.get("data"))}
                    patch_modules[key] = incoming
            elif (prior.get("lastAttemptAt") or "") > (incoming.get("lastAttemptAt") or "") or (prior.get("lastSuccessAt") or "") > (incoming.get("lastAttemptAt") or ""):
                patch_modules[key] = prior
            elif prior.get("data") is not None and reusable_module(key, prior):
                # 降级时只修改最新有效数据的状态，不用采集开始前的副本覆盖并发成功。
                patch_modules[key] = {**prior, "status": "STALE", "lastAttemptAt": incoming.get("lastAttemptAt"), "message": incoming.get("message")}
                if incoming.get("tradeDate") is None:
                    patch_modules[key] = undated_module(key, patch_modules[key])
        new_modules = {**old_modules,**patch_modules} if isinstance(old_modules,dict) and isinstance(patch_modules,dict) else None
        if not isinstance(new_modules, dict) or not isinstance(old_modules, dict):
            raise ValueError("Redis 快照模块格式无效")
        changed_modules = sorted(
            key for key in set(old_modules) | set(new_modules)
            if old_modules.get(key) != new_modules.get(key)
        )
        snapshot_id = uuid4().hex
        current = {**previous,**snapshot,"modules":new_modules,"snapshotId":snapshot_id}
        current["generatedAt"] = max(snapshot["generatedAt"],previous.get("generatedAt",snapshot["generatedAt"]))
        payload = json.dumps(current, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        market_notice = json.dumps({
            "schemaVersion": 1, "snapshotId": snapshot_id,
            "previousSnapshotId": previous_id, "changedModules": changed_modules,
        }, ensure_ascii=False, separators=(",", ":"))
        pipe.multi()
        if fund_points:
            if not trade_date:
                raise ValueError("个股资金点缺少交易日期")
            old_dates = json.loads(self._client.get(FUND_DATES_KEY) or "[]")
            if not isinstance(old_dates, list) or not all(isinstance(day, str) for day in old_dates):
                raise ValueError("个股资金历史日期索引无效")
            dates = sorted(set(old_dates) | {trade_date})
            keep_dates, remove_dates = dates[-2:], dates[:-2]
            symbols_key = f"{FUND_SYMBOLS_PREFIX}{trade_date}"
            old_symbols = json.loads(self._client.get(symbols_key) or "[]")
            if not isinstance(old_symbols, list) or not all(isinstance(item, str) for item in old_symbols):
                raise ValueError("个股资金历史股票索引无效")
            for symbol, point in fund_points.items():
                key = f"{FUND_SERIES_PREFIX}{trade_date}:{symbol}"
                old_points = json.loads(self._client.get(key) or "[]")
                if not isinstance(old_points, list):
                    raise ValueError("个股资金曲线格式无效")
                points = [item for item in old_points if item.get("collectedAt") != point["collectedAt"]]
                points.append(point)
                points.sort(key=lambda item: item["collectedAt"])
                pipe.set(key, json.dumps(points, ensure_ascii=False, allow_nan=False))
            pipe.set(symbols_key, json.dumps(sorted(set(old_symbols) | set(fund_points))))
            pipe.set(FUND_DATES_KEY, json.dumps(keep_dates))
            for day in remove_dates:
                index_key = f"{FUND_SYMBOLS_PREFIX}{day}"
                symbols = json.loads(self._client.get(index_key) or "[]")
                if not isinstance(symbols, list):
                    raise ValueError("个股资金历史股票索引无效")
                for symbol in symbols:
                    pipe.delete(f"{FUND_SERIES_PREFIX}{day}:{symbol}")
                pipe.delete(index_key)
        pipe.set(SNAPSHOT_KEY, payload)
        state_changed = _fund_state(old_modules.get('marketFundFlow')) != _fund_state(new_modules.get('marketFundFlow'))
        changed_symbols = set(fund_points or {})
        if state_changed:
            changed_symbols.update(self.enabled_symbols())
        if changed_symbols:
            from app.stock_monitor.service import MONITOR_STATE_KEY, MONITOR_UPDATES_CHANNEL

            base_id = self._client.get(MONITOR_STATE_KEY)
            state_id = uuid4().hex
            monitor_notice = json.dumps({
                "baseStateId": base_id, "stateId": state_id,
                "changedSymbols": sorted(changed_symbols),
            }, ensure_ascii=False, separators=(",", ":"))
            pipe.set(MONITOR_STATE_KEY, state_id)
            pipe.publish(MONITOR_UPDATES_CHANNEL, monitor_notice)
        pipe.publish(UPDATES_CHANNEL, market_notice)
        pipe.execute()
