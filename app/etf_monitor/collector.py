"""ETF 监控的交易日采样、Redis 快照及增量通知。"""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4
from contextlib import ExitStack
from zoneinfo import ZoneInfo

from app.etf_monitor.dictionary import save_dictionary
from app.stock_monitor.events import monitor_event_lock
from app.runtime.cooldown import remaining, record
from app.runtime.resources import SourceResourceError
from app.market.collector import _in_collection_window
from app.core.logging import log_failure, redis_failure_kind
from app.etf_monitor.normalize import etf_symbol, quote_rows
from app.providers.akshare_etf import EtfSourceError
from app.providers.http import error_metadata
from app.runtime.source_execution import SourceControlError, SourceCoolingError, SourceNotStartedError, source_batch
from app.calendar.service import CalendarService

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
ENABLED_KEY = "stock:etf-monitor:v1:enabled"
SNAPSHOT_KEY = "stock:etf-monitor:v1:snapshot"
STATE_KEY = "stock:etf-monitor:v1:state-id"
UPDATES_CHANNEL = "stock:etf-monitor:v1:updates"
PRICE_PREFIX = "stock:etf-monitor:v1:price-series:"
PRICE_DATES_KEY = "stock:etf-monitor:v1:price-series:dates"
PRICE_SYMBOLS_PREFIX = "stock:etf-monitor:v1:price-series:symbols:"
LOCK_KEY = "stock:etf-monitor:v1:lock"
INTERVAL_KEY = "stock:etf-monitor:v1:min-interval"
COOLDOWN_KEY = "stock:etf-monitor:v1:sina:cooldown"
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class EtfQuoteSource(Protocol):
    def quotes(self) -> list[dict[str, Any]]: ...


class EtfStore:
    def __init__(self, client: Any) -> None:
        self.client = client

    def enabled(self) -> list[dict[str, str]]:
        raw = self.client.get(ENABLED_KEY)
        if raw is None:
            return []
        entries = json.loads(raw)
        if not isinstance(entries, list) or len(entries) > 10:
            raise ValueError("ETF 监控清单格式无效")
        symbols = [etf_symbol(entry.get("symbol")) if isinstance(entry, dict) else None
                   for entry in entries]
        if any(symbol is None for symbol in symbols) or len(set(symbols)) != len(symbols):
            raise ValueError("ETF 监控代码无效或重复")
        enabled = []
        for entry, symbol in zip(entries, symbols):
            if (entry.get("code") != symbol[2:] or entry.get("market") != symbol[:2]
                    or not isinstance(entry.get("name"), str)
                    or not entry["name"].strip()):
                raise ValueError("ETF 监控基础资料无效")
            enabled.append({"symbol": symbol, "code": symbol[2:],
                            "name": entry["name"].strip(), "market": symbol[:2]})
        return enabled

    def acquire(self) -> str | None:
        token = uuid4().hex
        return token if self.client.set(LOCK_KEY, token, nx=True, ex=240) else None

    def release(self, token: str) -> None:
        self.client.eval(RELEASE_SCRIPT, 1, LOCK_KEY, token)

    def reserve_slot(self) -> bool:
        return bool(self.client.set(INTERVAL_KEY, "1", nx=True, ex=120))

    def load(self) -> dict[str, Any] | None:
        raw = self.client.get(SNAPSHOT_KEY)
        if raw is None:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
            raise ValueError("ETF 快照版本无效")
        return payload

    def series(self, trade_date: str, symbol: str) -> list[dict[str, Any]]:
        raw = self.client.get(f"{PRICE_PREFIX}{trade_date}:{symbol}")
        return json.loads(raw) if raw else []

    def publish(self, snapshot: dict[str, Any]) -> None:
        with monitor_event_lock(self.client, key=f"{SNAPSHOT_KEY}:write-lock"):
            self._publish_transaction(snapshot)

    def _publish_transaction(self, snapshot: dict[str, Any]) -> None:
        previous = self.load() or {}
        if previous.get("generatedAt", "") > snapshot["generatedAt"]:
            return
        previous_items = {item["symbol"]: item for item in previous.get("items", [])}
        current_items = {item["symbol"]: item for item in snapshot["items"]}
        for symbol, item in list(current_items.items()):
            old = previous_items.get(symbol)
            if old and old.get("quote"):
                quote = item.get("quote")
                if quote is None or old["quote"].get("collectedAt", "") > quote.get("collectedAt", ""):
                    current_items[symbol] = old
                elif quote.get("status") == "STALE":
                    current_items[symbol] = {**old, **item, "quote": {**old["quote"], "status": "STALE"},
                                             "priceSeries": old.get("priceSeries", [])}
        if snapshot.get("tradeDate") is None:
            # 窗口外成功及失败降级都只展示快照，保留历史Redis曲线键而不带入响应。
            current_items = {symbol: {**item, "priceSeries": [], "fundSeries": [],
                                     "quote": {**item["quote"], "tradeDate": None} if item.get("quote") else None}
                             for symbol, item in current_items.items()}
        snapshot = {**snapshot, "items": list(current_items.values()),
                    "generatedAt": max(snapshot["generatedAt"], previous.get("generatedAt", ""))}
        changed = sorted(symbol for symbol in set(previous_items) | set(current_items)
                         if previous_items.get(symbol) != current_items.get(symbol))
        base_id = self.client.get(STATE_KEY)
        state_id = uuid4().hex
        payload = {**snapshot, "stateId": state_id}
        pipe = self.client.pipeline(transaction=True)
        fresh = [item for item in snapshot["items"]
                 if isinstance(item.get("quote"), dict)
                 and item["quote"].get("status") == "FRESH"
                 and item["quote"].get("price") is not None
                 and snapshot.get("tradeDate") is not None
                 and item["quote"].get("tradeDate") == snapshot["tradeDate"]]
        if fresh:
            day = snapshot["tradeDate"]
            old_dates = json.loads(self.client.get(PRICE_DATES_KEY) or "[]")
            dates = sorted(set(old_dates) | {day})
            symbols_key = f"{PRICE_SYMBOLS_PREFIX}{day}"
            old_symbols = json.loads(self.client.get(symbols_key) or "[]")
            for item in fresh:
                points = {p["collectedAt"]: p for p in self.series(day, item["symbol"])}
                points.update({p["collectedAt"]: p for p in item["priceSeries"]})
                item["priceSeries"] = sorted(points.values(), key=lambda p: p["collectedAt"])
                pipe.set(f"{PRICE_PREFIX}{day}:{item['symbol']}",
                         json.dumps(item["priceSeries"], ensure_ascii=False, allow_nan=False))
            pipe.set(symbols_key, json.dumps(sorted(set(old_symbols) | {
                item["symbol"] for item in fresh
            })))
            pipe.set(PRICE_DATES_KEY, json.dumps(dates[-2:]))
            for old_day in dates[:-2]:
                index_key = f"{PRICE_SYMBOLS_PREFIX}{old_day}"
                for symbol in json.loads(self.client.get(index_key) or "[]"):
                    pipe.delete(f"{PRICE_PREFIX}{old_day}:{symbol}")
                pipe.delete(index_key)
        pipe.set(SNAPSHOT_KEY, json.dumps(payload, ensure_ascii=False, allow_nan=False))
        pipe.set(STATE_KEY, state_id)
        pipe.publish(UPDATES_CHANNEL, json.dumps({
            "baseStateId": base_id, "stateId": state_id,
            "changedSymbols": changed,
        }, ensure_ascii=False, separators=(",", ":")))
        pipe.execute()


class EtfCollector:
    def __init__(self, source: EtfQuoteSource, store: EtfStore,
                 calendar: CalendarService, *, mode: str = "auto") -> None:
        self.source, self.store, self.calendar = source, store, calendar
        self.mode = mode

    def collect(self, at: datetime) -> str:
        local = at.astimezone(SHANGHAI)
        if self.mode == "auto" and not _in_collection_window(local):
            return "skipped"
        token = self.store.acquire() if self.mode == "auto" else None
        if self.mode == "auto" and token is None:
            return "locked"
        started = time.monotonic()
        try:
            with ExitStack() as stack:
                stack.enter_context(source_batch(self.source, (LOCK_KEY, token) if self.mode == "auto" else None, started+120))
                return self._collect_locked(local, started, token)
        finally:
            if self.mode == "auto":
                self.store.release(token)
            LOGGER.info("ETF 采集总耗时 %.2f 秒", time.monotonic() - started)

    def _collect_locked(self, local: datetime, started: float, token: str | None) -> str:
        if self.mode == "auto" and not self.store.reserve_slot():
            return "throttled"
        trading = self.calendar.day_status(local.date(), local)
        if self.mode == "auto" and trading is not True:
            return "skipped"
        day = local.date().isoformat() if trading is True and _in_collection_window(local) else None
        enabled = self.store.enabled()
        if not enabled:
            return "skipped"
        symbols = [entry["symbol"] for entry in enabled]
        previous = self.store.load() or {}
        old_items = {item["symbol"]: item for item in previous.get("items", [])}
        failed = 0
        resource_failed = False
        source_finished = None
        cooldown_ttl = remaining(self.store.client, (COOLDOWN_KEY,), "sina", self.mode)
        cooling = cooldown_ttl is not None
        if cooling:
            LOGGER.info("ETF 新浪源冷却跳过，剩余 TTL %d 秒", cooldown_ttl)
            quotes = {}
            failed = len(symbols)
        else:
            try:
                rows = self.source.quotes()
                source_finished = time.monotonic()
                save_dictionary(self.store.client,rows,local+timedelta(seconds=source_finished-started))
                quotes = quote_rows(rows)
                del rows
            except SourceResourceError as exc:
                resource_failed = True
                LOGGER.warning("ETF资源失败 reason=%s memory=%s exitcode=%s",exc.reason,exc.state,exc.exitcode)
                quotes, failed = {}, len(symbols)
            except SourceNotStartedError:
                cooling = True
                LOGGER.info("ETF 行情预算结束，未发起源请求")
                quotes, failed = {}, len(symbols)
            except SourceCoolingError as exc:
                cooling = True
                LOGGER.info("ETF 新浪源冷却跳过，剩余 TTL %d 秒", exc.ttl)
                quotes, failed = {}, len(symbols)
            except Exception as exc:
                if isinstance(exc, SourceControlError) or redis_failure_kind(exc):
                    raise
                metadata = ({"exception_type": exc.exception_type, "root_type": exc.root_type,
                             "http_status": exc.http_status, "category": exc.category}
                            if isinstance(exc, EtfSourceError) else error_metadata(exc))
                if redis_failure_kind(exc) or metadata["category"] == "UNEXPECTED":
                    log_failure(LOGGER, (
                        f"etf type={metadata['exception_type']} root={metadata['root_type']} "
                        f"http={metadata['http_status']} category={metadata['category']}"
                    ), exc)
                else:
                    LOGGER.warning("ETF 行情批次失败，类型 %s，底层 %s，HTTP %s，分类 %s",
                                   metadata["exception_type"], metadata["root_type"],
                                   metadata["http_status"], metadata["category"])
                record(self.store.client, (COOLDOWN_KEY,), "sina", self.mode, metadata)
                quotes = {}
                failed = len(symbols)
        collected_at = (local + timedelta(seconds=(source_finished if source_finished is not None else time.monotonic()) - started)).isoformat(
            timespec="seconds"
        )
        items = []
        for entry in enabled:
            symbol = entry["symbol"]
            quote = quotes.get(symbol)
            if quote is None:
                if quotes:
                    failed += 1
                old = old_items.get(symbol)
                if old and isinstance(old.get("quote"), dict):
                    items.append({**old, **entry,
                                  "quote": {**old["quote"], "status": "STALE"}})
                else:
                    items.append({**entry, "quote": None, "priceSeries": [],
                                  "fundSeries": [],
                                  "fundFlowStatus": "NO_RELIABLE_SOURCE"})
                continue
            point = {"collectedAt": collected_at, "price": quote["price"]}
            series = [entry for entry in (self.store.series(day, symbol) if day else [])
                      if entry.get("collectedAt") != collected_at]
            if day is not None:
                series.append(point)
            series.sort(key=lambda entry: entry["collectedAt"])
            items.append({
                **entry,
                "quote": {key: value for key, value in quote.items()
                          if key not in {"symbol", "code", "name", "market", "closeConfirmed"}}
                | {"source": "SINA_ETF", "tradeDate": day,
                   "collectedAt": collected_at, "status": "FRESH"},
                "priceSeries": series, "fundSeries": [],
                "fundFlowStatus": "NO_RELIABLE_SOURCE",
            })
        if self.mode == "auto" and self.store.client.get(LOCK_KEY) != token:
            raise SourceControlError("业务任务锁已失效")
        self.store.publish({
            "schemaVersion": 1, "source": "AKShare.fund_etf_category_sina",
            "generatedAt": collected_at, "tradeDate": day,
            "items": items,
        })
        return "resource" if resource_failed else "cooldown" if cooling else "partial" if failed else "published"
