"""个股监控 V1 的纯转换、Redis 存储与盘中采样。"""

import json
import logging
import math
import re
import time
from datetime import date, datetime, time as day_time
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.providers.xueqiu import XueqiuSourceError


LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
ENABLED_KEY = "stock:monitor:v1:enabled"
QUOTE_PREFIX = "stock:monitor:v1:quote:"
SERIES_PREFIX = "stock:monitor:v1:series:"
LAST_TRADE_DATE_KEY = "stock:monitor:v1:lastTradeDate"
SAMPLE_LOCK_KEY = "stock:monitor:v1:sample:lock"
LAST_REQUEST_PREFIX = "stock:monitor:v1:sample:lastRequest:"
XQ_COOLDOWN_KEY = "stock:monitor:v1:xq:cooldown"
LOCK_SECONDS = 240
COOLDOWN_SECONDS = 2 * 60 * 60
PER_SYMBOL_INTERVAL_SECONDS = 120
CLOSE_RETRY_MINUTES = frozenset({2, 4, 6, 8, 10})
SYMBOL_RE = re.compile(r"^(SH|SZ|BJ)[0-9]{6}$")
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class QuoteValidationError(ValueError):
    """可安全记录原因码和字段名的报价校验异常。"""

    def __init__(self, reason: str, field: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field


def valid_symbol(symbol: Any) -> bool:
    return isinstance(symbol, str) and SYMBOL_RE.fullmatch(symbol) is not None


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _source_time(raw: Any) -> datetime | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            value = datetime.fromtimestamp(raw / 1000, SHANGHAI)
        else:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            value = value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)
        return value.replace(microsecond=0)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def normalize_quote(symbol: str, raw: dict[str, Any], collected_at: datetime) -> dict[str, Any]:
    if not valid_symbol(symbol) or not isinstance(raw, dict):
        raise QuoteValidationError("invalid_quote_shape", "symbol_or_quote")
    source_time = _source_time(raw.get("time") or raw.get("timestamp"))
    if source_time is None:
        raise QuoteValidationError("invalid_source_time", "time")
    price = _number(raw.get("current"))
    change = _number(raw.get("percent"))
    amount = _number(raw.get("amount"))
    if price is None and change is None and amount is None:
        raise QuoteValidationError("missing_numeric_values", "current_percent_amount")
    return {
        "schemaVersion": 1,
        "symbol": symbol,
        "source": "XQ",
        "sourceTime": source_time.isoformat(timespec="seconds"),
        "collectedAt": collected_at.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        "tradeDate": source_time.date().isoformat(),
        "price": price,
        "changePercent": change,
        "amount": amount,
        "low": _number(raw.get("low")),
        "high": _number(raw.get("high")),
        "open": _number(raw.get("open")),
        "limitUp": _number(raw.get("limit_up")),
        "limitDown": _number(raw.get("limit_down")),
        "averagePrice": _number(raw.get("avg_price")),
        "volume": _number(raw.get("volume")),
        "previousClose": _number(raw.get("previous_close")),
        "status": "FRESH",
    }


def normalize_profile(symbol: str, raw: dict[str, Any], quote: dict[str, Any] | None,
                      updated_at: datetime) -> dict[str, Any]:
    if not valid_symbol(symbol) or not isinstance(raw, dict):
        raise ValueError("雪球个股资料格式不正确")
    industry = raw.get("industry") or raw.get("industry_name") or raw.get("所属行业")
    industry = str(industry).strip() if industry is not None and not (
        isinstance(industry, float) and math.isnan(industry)
    ) else None
    listing = raw.get("list_date") or raw.get("listing_date") or raw.get("上市日期")
    listing_date: str | None = None
    if isinstance(listing, str):
        try:
            text = listing.strip()[:10]
            listing_date = date.fromisoformat(text).isoformat() if "-" in text else datetime.strptime(
                text, "%Y%m%d"
            ).date().isoformat()
        except ValueError:
            pass
    elif isinstance(listing, (int, float)):
        text = str(int(listing)) if math.isfinite(listing) else ""
        try:
            listing_date = datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            parsed = _source_time(listing)
            listing_date = parsed.date().isoformat() if parsed else None
    capital = _number(raw.get("market_capital"))
    if capital is None and quote is not None:
        capital = _number(quote.get("market_capital"))
    return {
        "symbol": symbol,
        "industry": industry or None,
        "listingDate": listing_date,
        "marketCap": capital,
        "updatedAt": updated_at.astimezone(SHANGHAI).isoformat(timespec="seconds"),
    }


class MonitorStore:
    def __init__(self, client: Any) -> None:
        self.client = client

    def enabled(self) -> list[dict[str, str]]:
        raw = self.client.get(ENABLED_KEY)
        if raw is None:
            return []
        stocks = json.loads(raw)
        if not isinstance(stocks, list) or len(stocks) > 10:
            raise ValueError("已监控股票清单格式不正确")
        seen: set[str] = set()
        for stock in stocks:
            if not isinstance(stock, dict) or not valid_symbol(stock.get("symbol")):
                raise ValueError("已监控股票代码不正确")
            symbol = stock["symbol"]
            if symbol in seen or stock.get("code") != symbol[2:] or stock.get("market") != symbol[:2]:
                raise ValueError("已监控股票清单重复或字段不一致")
            seen.add(symbol)
        return stocks

    def acquire(self, lock_seconds: int = LOCK_SECONDS) -> str | None:
        token = uuid4().hex
        return token if self.client.set(SAMPLE_LOCK_KEY, token, nx=True, ex=lock_seconds) else None

    def release(self, token: str) -> None:
        self.client.eval(RELEASE_SCRIPT, 1, SAMPLE_LOCK_KEY, token)

    def cooldown_active(self) -> bool:
        return bool(self.client.exists(XQ_COOLDOWN_KEY))

    def start_cooldown(self) -> None:
        self.client.set(XQ_COOLDOWN_KEY, "1", ex=COOLDOWN_SECONDS)

    def quote(self, symbol: str) -> dict[str, Any] | None:
        raw = self.client.get(f"{QUOTE_PREFIX}{symbol}")
        return json.loads(raw) if raw else None

    def reserve_request(self, symbol: str) -> bool:
        """Redis 原子 TTL 按实际请求时间限制同股源调用。"""
        key = f"{LAST_REQUEST_PREFIX}{symbol}"
        return bool(self.client.set(key, "1", nx=True, ex=PER_SYMBOL_INTERVAL_SECONDS))

    def prepare_trade_date(self, trade_date: str) -> None:
        old_date = self.client.get(LAST_TRADE_DATE_KEY)
        if old_date == trade_date:
            return
        for key in self.client.scan_iter(match=f"{SERIES_PREFIX}*"):
            if not key.startswith(f"{SERIES_PREFIX}{trade_date}:"):
                self.client.delete(key)
        self.client.set(LAST_TRADE_DATE_KEY, trade_date)

    def write_quote(self, quote: dict[str, Any], *, append_point: bool) -> None:
        symbol, trade_date = quote["symbol"], quote["tradeDate"]
        pipe = self.client.pipeline(transaction=True)
        pipe.set(f"{QUOTE_PREFIX}{symbol}", json.dumps(quote, ensure_ascii=False, allow_nan=False))
        if append_point and quote["price"] is not None:
            key = f"{SERIES_PREFIX}{trade_date}:{symbol}"
            raw = self.client.get(key)
            points = json.loads(raw) if raw else []
            points = [point for point in points if point.get("time") != quote["sourceTime"]]
            points.append({"time": quote["sourceTime"], "price": quote["price"]})
            points.sort(key=lambda point: point["time"])
            pipe.set(key, json.dumps(points, ensure_ascii=False, allow_nan=False))
        pipe.execute()


class QuoteSource(Protocol):
    def quote(self, symbol: str) -> dict[str, Any]: ...


class TradingCalendar(Protocol):
    def day_status(self, today: date, at: datetime) -> bool | None: ...


class StockMonitorSampler:
    def __init__(self, store: MonitorStore, source: QuoteSource, calendar: TradingCalendar,
                 *, xq_enabled: bool = False, request_interval_seconds: float = 1.0) -> None:
        self.store, self.source, self.calendar = store, source, calendar
        self.xq_enabled = xq_enabled
        self.request_interval_seconds = request_interval_seconds

    def sample(self, at: datetime) -> str:
        if not self.xq_enabled:
            return "disabled"
        local = at.astimezone(SHANGHAI)
        close_retry = local.hour == 15 and local.minute in CLOSE_RETRY_MINUTES
        if local.weekday() >= 5 or not (
            day_time(9, 30) <= local.time() <= day_time(11, 30)
            or day_time(13) <= local.time() <= day_time(15)
            or close_retry
        ):
            return "skipped"
        token = self.store.acquire()
        if token is None:
            return "locked"
        try:
            if self.calendar.day_status(local.date(), local) is not True:
                return "skipped"
            stocks = self.store.enabled()
            if not stocks:
                return "skipped"
            if self.store.cooldown_active():
                if close_retry:
                    for stock in stocks:
                        if not self._close_confirmed(stock["symbol"], local.date()):
                            self._mark_failed(stock["symbol"], local)
                return "cooldown"
            failures = 0
            last_request: float | None = None
            for index, stock in enumerate(stocks):
                symbol = stock["symbol"]
                if close_retry and self._close_confirmed(symbol, local.date()):
                    continue
                if last_request is not None:
                    remaining = self.request_interval_seconds - (time.monotonic() - last_request)
                    if remaining > 0:
                        time.sleep(remaining)
                if not self.store.reserve_request(symbol):
                    if close_retry:
                        self._mark_failed(symbol, local)
                    continue
                last_request = time.monotonic()
                try:
                    quote = normalize_quote(symbol, self.source.quote(symbol), local)
                    source_time = datetime.fromisoformat(quote["sourceTime"])
                    previous = self.store.quote(symbol)
                    previous_time = _source_time(previous.get("sourceTime")) if previous else None
                    if source_time.date() > local.date():
                        raise QuoteValidationError("source_date_in_future", "time")
                    if source_time.date() < local.date():
                        self._preserve_stale(
                            symbol, quote, previous, local, "source_previous_date"
                        )
                        continue
                    if previous_time and previous_time > source_time:
                        self._preserve_stale(
                            symbol, quote, previous, local, "source_older_than_cache"
                        )
                        continue
                    if close_retry and source_time.time() <= day_time(15):
                        self._preserve_stale(
                            symbol, quote, previous, local, "close_not_confirmed"
                        )
                        continue
                    if previous_time == source_time:
                        self._preserve_stale(
                            symbol, quote, previous, local, "source_unchanged"
                        )
                        continue
                    self.store.prepare_trade_date(local.date().isoformat())
                    self.store.write_quote(quote, append_point=True)
                except Exception as exc:
                    failures += 1
                    reason = exc.reason if isinstance(exc, QuoteValidationError) else (
                        "source_error" if isinstance(exc, XueqiuSourceError) else "unexpected_error"
                    )
                    field = exc.field if isinstance(exc, QuoteValidationError) else "none"
                    LOGGER.warning(
                        "雪球报价 %s 失败，原因 %s，字段 %s，异常 %s，采集日期 %s",
                        symbol, reason, field, type(exc).__name__, local.date().isoformat(),
                    )
                    self._mark_failed(symbol, local)
                    if isinstance(exc, XueqiuSourceError) and exc.cooldown:
                        self.store.start_cooldown()
                        for pending in stocks[index + 1:]:
                            self._mark_failed(pending["symbol"], local)
                        break
            return "partial" if failures else "published"
        finally:
            self.store.release(token)

    def _preserve_stale(
        self, symbol: str, incoming: dict[str, Any], previous: dict[str, Any] | None,
        at: datetime, reason: str,
    ) -> None:
        """延迟报价只更新最近有效报价状态，不写入当日曲线。"""
        incoming_time = datetime.fromisoformat(incoming["sourceTime"])
        previous_time = _source_time(previous.get("sourceTime")) if previous else None
        use_previous = (
            previous is not None and previous_time is not None
            and _number(previous.get("price")) is not None
            and previous_time >= incoming_time
        )
        selected = previous if use_previous else incoming
        assert selected is not None
        self.store.write_quote({**selected, "status": "STALE"}, append_point=False)
        LOGGER.info(
            "雪球报价 %s 已标记 STALE，原因 %s，源时间 %s，缓存源时间 %s，采集日期 %s",
            symbol, reason, incoming_time.isoformat(timespec="seconds"),
            previous_time.isoformat(timespec="seconds") if previous_time else "none",
            at.date().isoformat(),
        )

    def _mark_failed(self, symbol: str, at: datetime) -> None:
        previous = self.store.quote(symbol)
        if previous and previous.get("sourceTime") and previous.get("price") is not None:
            previous["status"] = "STALE"
            self.store.write_quote(previous, append_point=False)
        else:
            self.store.write_quote({
                "schemaVersion": 1, "symbol": symbol, "source": "XQ",
                "sourceTime": None,
                "collectedAt": at.isoformat(timespec="seconds"),
                "tradeDate": None, "price": None,
                "changePercent": None, "amount": None,
                "low": None, "high": None, "open": None,
                "limitUp": None, "limitDown": None, "averagePrice": None,
                "volume": None, "status": "ERROR",
                "previousClose": None,
            }, append_point=False)

    def _close_confirmed(self, symbol: str, trade_date: date) -> bool:
        previous = self.store.quote(symbol)
        if not previous or previous.get("tradeDate") != trade_date.isoformat():
            return False
        source = _source_time(previous.get("sourceTime"))
        return source is not None and source.time() > day_time(15)
