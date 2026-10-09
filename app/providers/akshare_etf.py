"""ETF 单次源调用复用共享执行器，不嵌套额外源子进程。"""

from typing import Any
from app.providers.http import error_metadata
from app.source_execution import SourceCall, SourceCallError, SourceExecutor, SourceNotStartedError


class EtfSourceError(SourceCallError):
    """ETF 跨进程的脱敏异常信息。"""


ALLOWED_HOSTS = {
    "quotes": {"vip.stock.finance.sina.com.cn"},
    "profile": {"fund.10jqka.com.cn"},
    "asset_allocation": {"danjuanfunds.com"},
}


class AkShareEtfProvider:
    def __init__(self, timeout_seconds: int = 15, *, executor: Any = None, market_quotes: bool = False) -> None:
        self.timeout_seconds = timeout_seconds
        self.market_quotes = market_quotes
        self.executor = executor if executor is not None else SourceExecutor.configured(timeout_seconds)

    def _call(self, kind: str, *, budget_seconds: float | None = None,
              **arguments: str) -> list[dict[str, Any]]:
        budget = min(self.timeout_seconds+17, budget_seconds) if budget_seconds is not None else self.timeout_seconds+17
        if budget <= 2:
            raise SourceNotStartedError("ETF 资料批次预算不足，未发起请求")
        if kind == "quotes":
            function, group, parameters = "fund_etf_category_sina", "sina", {"symbol": "ETF基金"}
            cooldown, policy = (("stock:etf-monitor:v1:sina:cooldown",), "etf") if self.market_quotes else ((), "none")
        elif kind == "profile":
            function, group, parameters = "fund_info_ths", "ths", {"symbol": arguments["symbol"]}
            cooldown, policy = (), "none"
        else:
            function, group = "fund_individual_detail_hold_xq", "xq"
            parameters = {"symbol": arguments["symbol"], "date": arguments["date"], "timeout": self.timeout_seconds}
            cooldown, policy = (), "none"
        try:
            frame = self.executor.call(SourceCall(function, group, parameters, budget,
                tuple(ALLOWED_HOSTS[kind]), cooldown, policy, kind == "profile"))
        except SourceCallError as exc:
            raise EtfSourceError(error_metadata(exc)) from exc
        return frame.to_dict("records") if hasattr(frame,"to_dict") else frame

    def quotes(self) -> list[dict[str, Any]]:
        return self._call("quotes")

    def profile(self, symbol: str, *, budget_seconds: float) -> list[dict[str, Any]]:
        return self._call("profile", symbol=symbol, budget_seconds=budget_seconds)

    def asset_allocation(self, symbol: str, report_period: str) -> list[dict[str, Any]]:
        return self._call("asset_allocation", symbol=symbol, date=report_period)
