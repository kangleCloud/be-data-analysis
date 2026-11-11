"""AKShare 研究行情源，复用全局配额和独立源进程。"""

from typing import Any

from app.runtime.source_execution import SourceCall, SourceExecutor


class AkShareQuantProvider:
    def __init__(self, timeout_seconds: int, *, executor: Any = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.executor = executor or SourceExecutor.configured(timeout_seconds, lane="reference")

    def history(self, asset_type: str, symbol: str, frequency: str, adjust: str,
                start: str, end: str) -> tuple[str, Any]:
        function = {"ETF": "fund_etf_hist_em", "STOCK": "stock_zh_a_hist", "INDEX": "stock_zh_index_daily_em"}[asset_type]
        params = {"symbol": symbol, "start_date": start.replace("-", ""), "end_date": end.replace("-", "")}
        if asset_type != "INDEX":
            params.update({"period": "daily", "adjust": {"raw": "", "qfq": "qfq", "hfq": "hfq"}[adjust]})
        frame = self.executor.call(SourceCall(function, "eastmoney", params, self.timeout_seconds + 17,
            ("push2his.eastmoney.com",), ("stock:quant:v1:eastmoney:cooldown",), "none"))
        return function, frame.to_dict("records") if hasattr(frame, "to_dict") else frame

    def universe(self, asset_type: str) -> tuple[str, Any]:
        function = "stock_zh_a_spot_em" if asset_type == "STOCK" else "fund_etf_spot_em"
        host = "82.push2.eastmoney.com" if asset_type == "STOCK" else "push2delay.eastmoney.com"
        frame = self.executor.call(SourceCall(function, "eastmoney", {}, max(180, self.timeout_seconds + 17),
            (host,), ("stock:quant:v1:eastmoney:cooldown",), "none"))
        return function, frame.to_dict("records") if hasattr(frame, "to_dict") else frame
