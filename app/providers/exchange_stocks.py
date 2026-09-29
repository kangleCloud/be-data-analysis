"""从三家交易所清单构造统一 A 股字典。"""

import threading
from contextlib import contextmanager
from typing import Any, Iterator

import requests


_REQUEST_PATCH_LOCK = threading.Lock()


class ExchangeStockProvider:
    def __init__(self, timeout_seconds: int = 15, source: Any = None) -> None:
        if source is None:
            import akshare

            source = akshare
        self._source = source
        self._timeout_seconds = timeout_seconds

    @contextmanager
    def _bounded_requests(self) -> Iterator[None]:
        """AKShare 部分交易所函数未传超时，仅在互斥调用期间补齐。"""
        with _REQUEST_PATCH_LOCK:
            original_get, original_post = requests.get, requests.post

            def bounded_get(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("timeout", self._timeout_seconds)
                response = original_get(*args, **kwargs)
                response.raise_for_status()
                return response

            def bounded_post(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("timeout", self._timeout_seconds)
                response = original_post(*args, **kwargs)
                response.raise_for_status()
                return response

            requests.get, requests.post = bounded_get, bounded_post
            try:
                yield
            finally:
                requests.get, requests.post = original_get, original_post

    def all_a_stocks(self) -> list[dict[str, str]]:
        with self._bounded_requests():
            # AKShare 交易所清单函数有进程内 lru_cache；每日同步必须重新读取源站。
            for function in (
                self._source.stock_info_sh_name_code,
                self._source.stock_info_sz_name_code,
                self._source.stock_info_bj_name_code,
            ):
                clear_cache = getattr(function, "cache_clear", None)
                if clear_cache is not None:
                    clear_cache()
            frames = (
                ("SH", self._source.stock_info_sh_name_code(symbol="主板A股"), "证券代码", "证券简称"),
                ("SH", self._source.stock_info_sh_name_code(symbol="科创板"), "证券代码", "证券简称"),
                ("SZ", self._source.stock_info_sz_name_code(symbol="A股列表"), "A股代码", "A股简称"),
                ("BJ", self._source.stock_info_bj_name_code(), "证券代码", "证券简称"),
            )
        stocks: dict[str, dict[str, str]] = {}
        for market, frame, code_field, name_field in frames:
            rows = frame.to_dict("records")
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
