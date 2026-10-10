"""从三家交易所清单构造统一 A 股字典。"""

from contextlib import closing
from typing import Any
from app.runtime.source_execution import SourceCall, SourceExecutor, completed, source_batch


class ExchangeStockProvider:
    def __init__(self, timeout_seconds: int = 15, source: Any = None, *, executor: Any = None) -> None:
        self._source = source
        self.executor = executor if executor is not None else (None if source is not None else SourceExecutor.configured(timeout_seconds))
        self._timeout_seconds = timeout_seconds

    def _frame(self, function: str, group: str, parameters: dict) -> Any:
        if self._source is not None:
            method = getattr(self._source, function)
            clear_cache = getattr(method, "cache_clear", None)
            if clear_cache:
                clear_cache()
            return method(**parameters)
        host = {"sse": "query.sse.com.cn", "szse": "www.szse.cn", "bse": "www.bse.cn"}[group]
        return self.executor.call(SourceCall(function, group, parameters, 60, (host,)))

    def all_a_stocks(self) -> list[dict[str, str]]:
        combinations = (
            ("main", "sse", "stock_info_sh_name_code", {"symbol": "主板A股"}, "SH", "证券代码", "证券简称"),
            ("star", "sse", "stock_info_sh_name_code", {"symbol": "科创板"}, "SH", "证券代码", "证券简称"),
            ("sz", "szse", "stock_info_sz_name_code", {"symbol": "A股列表"}, "SZ", "A股代码", "A股简称"),
            ("bj", "bse", "stock_info_bj_name_code", {}, "BJ", "证券代码", "证券简称"),
        )
        actions = [(key, group, lambda f=function,g=group,p=parameters: self._frame(f,g,p))
                   for key,group,function,parameters,*_ in combinations]
        results = {}
        with source_batch(self), closing(completed(actions, source=self)) as completed_results:
            for key, frame, error, finished_at in completed_results:
                if error:
                    raise error
                results[key] = frame
        frames = [(market, results[key], code, name)
                  for key,_,_,_,market,code,name in combinations]
        stocks: dict[str, dict[str, str]] = {}
        for market, frame, code_field, name_field in frames:
            rows = frame.to_dict("records") if hasattr(frame,"to_dict") else frame
            if not rows:
                raise ValueError(f"{market} 交易所股票清单为空")
            for row in rows:
                code = str(row.get(code_field, "")).strip()
                name = str(row.get(name_field, "")).strip()
                if not code.isdigit() or len(code) != 6 or not name or name.lower() == "nan":
                    continue
                symbol = f"{market}{code}"
                stock = {"symbol": symbol, "code": code, "name": name, "market": market}
                if symbol in stocks and stocks[symbol] != stock:
                    raise ValueError(f"交易所股票清单代码重复: {symbol}")
                stocks[symbol] = stock
        if not all(any(item["market"] == market for item in stocks.values()) for market in ("SH", "SZ", "BJ")):
            raise ValueError("交易所股票清单缺少市场")
        return [stocks[key] for key in sorted(stocks)]
