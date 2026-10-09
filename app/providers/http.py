"""源请求的有限超时与可脱敏异常分类。"""

from contextlib import contextmanager
from typing import Any, Iterator

import requests


def bounded_timeout(original: Any, read_seconds: float) -> tuple[float, float]:
    """为 connect/read 分别设置上限，保留调用方更短的限制。"""
    connect, read = original if isinstance(original, tuple) else (original, original)
    return (min(5, connect) if connect is not None else 5,
            min(read_seconds, read) if read is not None else read_seconds)


def error_metadata(exc: BaseException) -> dict[str, Any]:
    if getattr(exc,'category',None) == 'RESOURCE':
        return {'exception_type':type(exc).__name__,'root_type':type(exc).__name__,
                'http_status':None,'category':'RESOURCE'}
    if all(hasattr(exc, field) for field in ("exception_type", "root_type", "http_status", "category")):
        result = {field: getattr(exc, field) for field in ("exception_type", "root_type", "http_status", "category")}
        if getattr(exc,'reason',None):
            result.update(reason=exc.reason,fields=exc.fields,code=exc.code,bad_rows=exc.bad_rows)
        return result
    chain = []
    seen = set()
    current = exc
    while id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        nested = current.__cause__ or current.__context__
        if nested is None:
            nested = next((arg for arg in current.args if isinstance(arg, BaseException)), None)
        if nested is None:
            break
        current = nested
    status = next((getattr(getattr(item, "response", None), "status_code", None)
                   for item in chain if getattr(item, "response", None) is not None), None)
    if not isinstance(status, int):
        status = None
    if status in {403, 429}:
        category = "HTTP_REJECTED"
    elif status is not None:
        category = "HTTP_ERROR"
    elif any(isinstance(item, (requests.Timeout, TimeoutError)) for item in chain):
        category = "TIMEOUT"
    elif any(isinstance(item, (requests.ConnectionError, ConnectionError, OSError))
             for item in chain):
        category = "NETWORK"
    elif isinstance(exc, (ValueError, TypeError, KeyError, AttributeError, IndexError)):
        category = "FORMAT"
    else:
        category = "UNEXPECTED"
    result = {"exception_type": type(exc).__name__, "root_type": type(chain[-1]).__name__,
            "http_status": status, "category": category}
    if hasattr(exc,'diagnostic') and getattr(exc,'reason',None):
        result.update(reason=exc.reason,fields=exc.fields,code=exc.code,bad_rows=exc.bad_rows)
    return result


@contextmanager
def quiet_progress() -> Iterator[None]:
    """独立采集进程内关闭 AKShare 的 tqdm，避免分页进度污染生产日志。"""
    from tqdm.std import tqdm

    original = tqdm.__init__
    descriptor = tqdm.__dict__.get("__init__")
    def quiet(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["disable"] = True
        original(self, *args, **kwargs)
    tqdm.__init__ = quiet
    try:
        yield
    finally:
        if descriptor is None:
            del tqdm.__init__
        else:
            tqdm.__init__ = descriptor
