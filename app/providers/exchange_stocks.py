"""从三家交易所的四个清单组合构造统一 A 股字典。"""

import logging
import re
import time
from contextlib import closing
from typing import Any

from app.providers.http import error_metadata
from app.core.logging import redis_failure_kind
from app.runtime.resources import SourceResourceError
from app.runtime.source_execution import (
    SourceCall, SourceExecutor, SourceBusyError, SourceControlError, SourceCoolingError,
    SourceNotStartedError, SourceThrottledError, completed, source_batch,
)

LOGGER = logging.getLogger(__name__)
COMBINATIONS = (
    ("main", "sse", "stock_info_sh_name_code", {"symbol": "主板A股"}, "SH", "证券代码", "证券简称"),
    ("star", "sse", "stock_info_sh_name_code", {"symbol": "科创板"}, "SH", "证券代码", "证券简称"),
    ("sz", "szse", "stock_info_sz_name_code", {"symbol": "A股列表"}, "SZ", "A股代码", "A股简称"),
    ("bj", "bse", "stock_info_bj_name_code", {}, "BJ", "证券代码", "证券简称"),
)
DATA_REASONS = {"EMPTY_BATCH", "INVALID_SHAPE", "MISSING_FIELDS", "NO_VALID_ROWS", "DUPLICATE_CONFLICT"}


class ExchangeDictionaryDataError(ValueError):
    """字典校验仅携带预定义原因，不携带源行、名称或响应文本。"""
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("交易所股票清单校验失败")


def log_dictionary_failure(logger: logging.Logger, exc: Exception, *, phase: str,
                           elapsed: float, combination: str = "-", group: str = "-",
                           function: str = "-") -> None:
    """只记录类型标识、已知分类和整数状态，禁止输出异常原文。"""
    metadata = error_metadata(exc)
    def identifier(value: Any) -> str:
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", value) else "UNKNOWN"
    category = metadata["category"]
    if not isinstance(category, str) or category not in {"RESOURCE", "HTTP_REJECTED", "HTTP_ERROR", "AUTH_REJECTED", "NETWORK", "TIMEOUT", "FORMAT", "UNEXPECTED"}:
        category = "UNEXPECTED"
    reason = getattr(exc, "reason", None)
    reason = reason if isinstance(reason, str) and reason in DATA_REASONS else "-"
    if isinstance(exc, SourceBusyError):
        category, reason = "CONTROL", "ENTRY_BUSY"
    elif isinstance(exc, SourceCoolingError):
        category, reason = "CONTROL", "SOURCE_COOLDOWN"
    elif isinstance(exc, SourceThrottledError):
        category, reason = "CONTROL", "SOURCE_THROTTLED"
    elif isinstance(exc, SourceNotStartedError):
        category, reason = "TIMEOUT", "NOT_STARTED"
    elif isinstance(exc, SourceControlError):
        category, reason = "CONTROL", "CONTROL_FAILED"
    elif isinstance(exc, SourceResourceError):
        category, reason = "RESOURCE", "PROCESS_EXIT"
    redis_kind = redis_failure_kind(exc)
    if redis_kind:
        category, reason = "CONTROL", "REDIS_"+redis_kind
    status = metadata["http_status"]
    status = status if type(status) is int and 100 <= status <= 599 else None
    exitcode = getattr(exc, "exitcode", None)
    exitcode = exitcode if type(exitcode) is int else None
    log = logger.info if isinstance(exc, (SourceBusyError, SourceCoolingError, SourceThrottledError, SourceNotStartedError)) else logger.warning
    log("交易所字典失败 phase=%s combination=%s group=%s function=%s type=%s root=%s http=%s category=%s reason=%s exitcode=%s elapsed=%.2f",
        phase, combination, group, function, identifier(metadata["exception_type"]),
        identifier(metadata["root_type"]), status, category, reason, exitcode, elapsed)


class ExchangeStockProvider:
    def __init__(self, timeout_seconds: int = 15, source: Any = None, *, executor: Any = None, mode: str = "auto") -> None:
        self._source = source
        self.mode = mode
        self.executor = executor if executor is not None else (None if source is not None else SourceExecutor.configured(timeout_seconds, lane="reference", mode=mode))
        self._timeout_seconds = timeout_seconds

    def _frame(self, function: str, group: str, parameters: dict, *, combination: str) -> Any:
        started = time.monotonic()
        try:
            if self._source is not None:
                method = getattr(self._source, function)
                clear_cache = getattr(method, "cache_clear", None)
                if clear_cache:
                    clear_cache()
                frame = method(**parameters)
            else:
                host = {"sse": "query.sse.com.cn", "szse": "www.szse.cn", "bse": "www.bse.cn"}[group]
                frame = self.executor.call(SourceCall(function, group, parameters, 60, (host,),
                                                       entry_lane="reference", mode=self.mode))
        except Exception as exc:
            log_dictionary_failure(LOGGER, exc, phase="source", combination=combination,
                                   group=group, function=function, elapsed=time.monotonic()-started)
            raise
        LOGGER.info("交易所字典完成 phase=source combination=%s group=%s function=%s elapsed=%.2f",
                    combination, group, function, time.monotonic()-started)
        return frame

    def all_a_stocks(self) -> list[dict[str, str]]:
        actions = [(key, group, lambda k=key,f=function,g=group,p=parameters: self._frame(f,g,p,combination=k))
                   for key,group,function,parameters,*_ in COMBINATIONS]
        results = {}
        with source_batch(self), closing(completed(actions, source=self)) as completed_results:
            for key, frame, error, finished_at in completed_results:
                if error:
                    raise error
                results[key] = frame
        stocks: dict[str, dict[str, str]] = {}
        for key, group, function, _, market, code_field, name_field in COMBINATIONS:
            started = time.monotonic()
            try:
                frame = results.pop(key)
                rows = frame.to_dict("records") if hasattr(frame,"to_dict") else frame
                if not isinstance(rows, list):
                    raise ExchangeDictionaryDataError("INVALID_SHAPE")
                if not rows:
                    raise ExchangeDictionaryDataError("EMPTY_BATCH")
                valid_rows = 0
                for row in rows:
                    if not isinstance(row, dict):
                        raise ExchangeDictionaryDataError("INVALID_SHAPE")
                    if code_field not in row or name_field not in row:
                        raise ExchangeDictionaryDataError("MISSING_FIELDS")
                    code = str(row[code_field]).strip()
                    name = str(row[name_field]).strip()
                    if not code.isascii() or not code.isdigit() or len(code) != 6 or not name or name.lower() in {"nan", "none", "<na>", "nat"}:
                        continue
                    symbol = f"{market}{code}"
                    stock = {"symbol": symbol, "code": code, "name": name, "market": market}
                    if symbol in stocks and stocks[symbol] != stock:
                        raise ExchangeDictionaryDataError("DUPLICATE_CONFLICT")
                    stocks[symbol] = stock
                    valid_rows += 1
                # 上海主板和科创板各自必须有效，不能由另一SH组合掩盖缺失。
                if not valid_rows:
                    raise ExchangeDictionaryDataError("NO_VALID_ROWS")
            except Exception as exc:
                log_dictionary_failure(LOGGER, exc, phase="validate", combination=key,
                                       group=group, function=function, elapsed=time.monotonic()-started)
                raise
            LOGGER.info("交易所字典完成 phase=validate combination=%s group=%s function=%s rows=%d elapsed=%.2f",
                        key, group, function, valid_rows, time.monotonic()-started)
        return [stocks[key] for key in sorted(stocks)]
