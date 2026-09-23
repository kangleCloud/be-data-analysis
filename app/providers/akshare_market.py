"""AKShare 市场看板接口适配。"""

import signal
import time
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator, Protocol

import requests


class MarketSource(Protocol):
    def latest_trading_date(self, today: date) -> date | None: ...

    def sector_quotes(self, sector_type: str) -> Any: ...

    def sector_fund_flow(self, sector_type: str) -> Any: ...

    def market_fund_flow(self) -> Any: ...


@contextmanager
def _deadline(seconds: int) -> Iterator[None]:
    """限制 AKShare 内部网络调用时长，避免定时任务无限占用锁。"""
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

    def _call(self, function: Any, **kwargs: str) -> Any:
        for attempt in range(2):
            try:
                with _deadline(self._timeout_seconds):
                    return function(**kwargs)
            except (requests.RequestException, TimeoutError):
                if attempt:
                    raise
                time.sleep(0.5)
        raise AssertionError("数据源重试循环未返回")

    def latest_trading_date(self, today: date) -> date | None:
        frame = self._call(self._akshare.tool_trade_date_hist_sina)
        dates = []
        for raw in frame["trade_date"]:
            trading_date = date.fromisoformat(str(raw)[:10])
            if trading_date <= today:
                dates.append(trading_date)
        return max(dates, default=None)

    def sector_quotes(self, sector_type: str) -> Any:
        function = (
            self._akshare.stock_board_industry_name_em
            if sector_type == "industry"
            else self._akshare.stock_board_concept_name_em
        )
        return self._call(function)

    def sector_fund_flow(self, sector_type: str) -> Any:
        source_type = "行业资金流" if sector_type == "industry" else "概念资金流"
        return self._call(
            self._akshare.stock_sector_fund_flow_rank,
            indicator="今日",
            sector_type=source_type,
        )

    def market_fund_flow(self) -> Any:
        return self._call(self._akshare.stock_market_fund_flow)
