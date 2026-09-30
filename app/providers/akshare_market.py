"""AKShare 市场看板接口适配。"""

import signal
import time
import logging
from contextlib import contextmanager, nullcontext
from datetime import date
from typing import Any, Iterator, Protocol

import requests

LOGGER = logging.getLogger(__name__)
THS_SECTOR_TIMEOUT_SECONDS = 120
THS_INDIVIDUAL_TIMEOUT_SECONDS = 900
THS_REQUEST_INTERVAL_SECONDS = 1


def _root_exception_name(exc: BaseException) -> str:
    """只记录底层异常类型，避免把请求参数写进日志。"""
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        nested = exc.__cause__ or exc.__context__
        if nested is None:
            nested = next((arg for arg in exc.args if isinstance(arg, BaseException)), None)
        if nested is None:
            break
        exc = nested
    return type(exc).__name__


class MarketSource(Protocol):
    def latest_trading_date(self, today: date) -> date | None: ...

    def sector_fund_flow(self, sector_type: str) -> Any: ...

    def market_fund_flow(self) -> Any: ...


@contextmanager
def _deadline(seconds: int) -> Iterator[None]:
    """限制单次 AKShare 调用时长，避免采集无限占用锁。"""
    def timeout_handler(_signum: int, _frame: Any) -> None:
        raise TimeoutError("数据源调用超时")

    old_handler = signal.signal(signal.SIGALRM, timeout_handler)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, old_handler)


class AkShareMarketProvider:
    """只暴露 V1 需要的 AKShare 接口，便于测试替换。"""

    def __init__(self, timeout_seconds: int) -> None:
        import akshare

        self._akshare = akshare
        self._timeout_seconds = timeout_seconds
        self._last_ths_request_at: float | None = None

    @contextmanager
    def _paced_ths(self) -> Iterator[None]:
        """只在独立采集进程内约束同花顺分页请求间隔。"""
        original_get = requests.get

        def paced_get(url: str, *args: Any, **kwargs: Any) -> Any:
            if "data.10jqka.com.cn" in url:
                if self._last_ths_request_at is not None:
                    remaining = THS_REQUEST_INTERVAL_SECONDS - (
                        time.monotonic() - self._last_ths_request_at
                    )
                    if remaining > 0:
                        time.sleep(remaining)
                self._last_ths_request_at = time.monotonic()
                kwargs.setdefault("timeout", self._timeout_seconds)
            return original_get(url, *args, **kwargs)

        requests.get = paced_get
        try:
            yield
        finally:
            requests.get = original_get

    def _call(
        self, function: Any, *, timeout_seconds: int | None = None,
        pace_ths: bool = False, **kwargs: str
    ) -> Any:
        started = time.monotonic()
        name = getattr(function, "__name__", "unknown")
        try:
            pacing = self._paced_ths() if pace_ths else nullcontext()
            with _deadline(timeout_seconds or self._timeout_seconds), pacing:
                result = function(**kwargs)
            LOGGER.info("接口 %s 成功，耗时 %.2f 秒", name, time.monotonic() - started)
            return result
        except Exception as exc:
            LOGGER.warning(
                "接口 %s 失败，耗时 %.2f 秒，异常 %s，底层异常 %s",
                name, time.monotonic() - started, type(exc).__name__,
                _root_exception_name(exc),
            )
            raise

    def latest_trading_date(self, today: date) -> date | None:
        frame = self._call(self._akshare.tool_trade_date_hist_sina)
        dates = []
        for raw in frame["trade_date"]:
            trading_date = date.fromisoformat(str(raw)[:10])
            if trading_date <= today:
                dates.append(trading_date)
        return max(dates, default=None)

    def sector_fund_flow(self, sector_type: str) -> Any:
        function = (
            self._akshare.stock_fund_flow_industry
            if sector_type == "industry"
            else self._akshare.stock_fund_flow_concept
        )
        return self._call(
            function, symbol="即时", timeout_seconds=THS_SECTOR_TIMEOUT_SECONDS,
            pace_ths=True,
        )

    def market_fund_flow(self) -> Any:
        return self._call(
            self._akshare.stock_fund_flow_individual, symbol="即时",
            timeout_seconds=THS_INDIVIDUAL_TIMEOUT_SECONDS,
            pace_ths=True,
        )
