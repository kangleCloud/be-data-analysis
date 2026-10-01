"""共享交易日历缓存、刷新节流及未知日期判定。"""

import json
import logging
import multiprocessing as mp
import re
from queue import Empty
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
CACHE_KEY = "stock:calendar:v1:trading-days"
LOCK_KEY = "stock:calendar:v1:refresh:lock"
AUTO_RETRY_KEY = "stock:calendar:v1:refresh:daily"
MANUAL_INTERVAL_KEY = "stock:calendar:v1:refresh:manual"
LOCK_SECONDS = 120
MANUAL_INTERVAL_SECONDS = 10 * 60
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class CalendarSource(Protocol):
    def dates(self) -> list[Any]: ...


def _source_worker(queue: Any, timeout_seconds: int) -> None:
    """将无 timeout 参数的 AKShare 调用隔离在子进程内。"""
    import akshare
    import requests

    original_get = requests.get

    def bounded_get(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout_seconds)
        return original_get(*args, **kwargs)

    requests.get = bounded_get
    try:
        frame = akshare.tool_trade_date_hist_sina()
        queue.put(("ok", [str(value) for value in frame["trade_date"]]))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


class AkShareCalendarSource:
    def __init__(self, timeout_seconds: int = 15,
                 *, worker: Callable[[Any, int], None] = _source_worker) -> None:
        self.timeout_seconds = timeout_seconds
        self._worker = worker

    def dates(self) -> list[str]:
        context = mp.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=self._worker, args=(queue, self.timeout_seconds))
        process.start()
        try:
            # 大量日期可能填满管道；先读取，再 join 子进程，避免双向等待。
            outcome, value = queue.get(timeout=self.timeout_seconds + 10)
        except Empty as exc:
            raise TimeoutError("交易日历源调用超时或未返回数据") from exc
        finally:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join()
            queue.close()
        if outcome != "ok":
            raise RuntimeError(f"交易日历源调用失败: {value}")
        return value


def normalize_dates(values: list[Any], refreshed_at: datetime) -> dict[str, Any]:
    """先校验全部源日期，再只保留上海时间目标年份。"""
    if not isinstance(values, list) or not values:
        raise ValueError("交易日历为空")
    target_year = refreshed_at.astimezone(SHANGHAI).year
    dates: list[str] = []
    for raw in values:
        value = raw.isoformat() if isinstance(raw, date) else str(raw)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("交易日历包含无效日期")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("交易日历包含无效日期") from exc
        if date.fromisoformat(value).year == target_year:
            dates.append(value)
    unique = sorted(set(dates))
    if not unique:
        raise ValueError("交易日历无当年可用日期")
    return {
        "schemaVersion": 1,
        "source": "AKShare.tool_trade_date_hist_sina",
        "year": target_year,
        "refreshedAt": refreshed_at.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        "firstDate": unique[0], "lastDate": unique[-1], "dates": unique,
    }


def _valid_cache(payload: Any) -> bool:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        return False
    if payload.get("source") != "AKShare.tool_trade_date_hist_sina":
        return False
    year = payload.get("year")
    if not isinstance(year, int) or isinstance(year, bool):
        return False
    dates = payload.get("dates")
    if not isinstance(dates, list) or not dates or not all(isinstance(item, str) for item in dates):
        return False
    try:
        normalized = normalize_dates(dates, datetime(year, 1, 1, tzinfo=SHANGHAI))
        refreshed = datetime.fromisoformat(payload["refreshedAt"])
        if (refreshed.utcoffset() != timedelta(hours=8)
                or refreshed.astimezone(SHANGHAI).year != year):
            return False
    except (ValueError, KeyError, TypeError):
        return False
    return (
        dates == normalized["dates"] and payload.get("firstDate") == dates[0]
        and payload.get("lastDate") == dates[-1]
    )


def _seconds_until_tomorrow(at: datetime) -> int:
    local = at.astimezone(SHANGHAI)
    tomorrow = datetime.combine(local.date() + timedelta(days=1), time(), SHANGHAI)
    return max(1, int((tomorrow - local).total_seconds()))


class CalendarService:
    def __init__(self, client: Any, source: CalendarSource) -> None:
        self.client, self.source = client, source

    def load(self) -> dict[str, Any] | None:
        raw = self.client.get(CACHE_KEY)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return payload if _valid_cache(payload) else None

    @staticmethod
    def covered(cache: dict[str, Any] | None, day: date) -> bool:
        return (
            cache is not None and cache["year"] == day.year
            and cache["firstDate"] <= day.isoformat() <= cache["lastDate"]
        )

    def day_status(self, day: date, at: datetime) -> bool | None:
        cache = self.load()
        local = at.astimezone(SHANGHAI)
        monthly_due = (
            (local.day > 1 or local.time() >= time(0, 10))
            and (cache is None or cache["refreshedAt"][:7] != f"{local:%Y-%m}")
        )
        if not self.covered(cache, day) or monthly_due:
            self.refresh(at)
            cache = self.load()
        if not self.covered(cache, day):
            return None
        return day.isoformat() in cache["dates"]

    def refresh(self, at: datetime, *, manual: bool = False) -> str:
        local = at.astimezone(SHANGHAI)
        token = uuid4().hex
        if not self.client.set(LOCK_KEY, token, nx=True, ex=LOCK_SECONDS):
            return "locked"
        try:
            gate = MANUAL_INTERVAL_KEY if manual else AUTO_RETRY_KEY
            ttl = MANUAL_INTERVAL_SECONDS if manual else _seconds_until_tomorrow(local)
            if not self.client.set(gate, "1", nx=True, ex=ttl):
                return "throttled"
            old = self.load()
            try:
                payload = normalize_dates(self.source.dates(), local)
                if payload["lastDate"] < local.date().isoformat():
                    raise ValueError("交易日历未覆盖当前日期")
                if (old is not None and old["year"] == payload["year"]
                        and payload["lastDate"] < old["lastDate"]):
                    raise ValueError("交易日历源范围倒退")
                if not self.client.set(CACHE_KEY, json.dumps(payload, ensure_ascii=False)):
                    raise RuntimeError("交易日历缓存写入失败")
                return "refreshed"
            except Exception as exc:
                LOGGER.warning("交易日历刷新失败，异常 %s", type(exc).__name__)
                return "failed"
        finally:
            self.client.eval(RELEASE_SCRIPT, 1, LOCK_KEY, token)
