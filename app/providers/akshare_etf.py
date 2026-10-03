"""固定 AKShare 版本的新浪、交易所与雪球基金接口隔离。"""

import multiprocessing as mp
import time
from io import BytesIO
from queue import Empty
from typing import Any
from urllib.parse import urlparse

ALLOWED_HOSTS = {
    "quotes": {"vip.stock.finance.sina.com.cn"},
    "sse_scale": {"query.sse.com.cn"},
    "szse_scale": {"fund.szse.cn"},
    "asset_allocation": {"danjuanfunds.com"},
}


def _worker(queue: Any, kind: str, arguments: dict[str, str], timeout: int) -> None:
    import akshare
    import pandas as pd
    import requests

    original_get = requests.get
    original_read_excel = pd.read_excel
    last_request: float | None = None

    def allowed_get(url: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal last_request
        if urlparse(url).hostname not in ALLOWED_HOSTS[kind]:
            raise RuntimeError("ETF 接口请求了未审计域名")
        if last_request is not None:
            time.sleep(max(0, 0.2 - (time.monotonic() - last_request)))
        last_request = time.monotonic()
        kwargs.setdefault("timeout", timeout)
        return original_get(url, *args, **kwargs)

    def read_excel_bytes(source: Any, *args: Any, **kwargs: Any) -> Any:
        # AKShare 1.18.97 直接把 bytes 交给 pandas.read_excel；当前 pandas 需要文件对象。
        return original_read_excel(BytesIO(source) if isinstance(source, bytes) else source,
                                   *args, **kwargs)

    requests.get = allowed_get
    if kind == "szse_scale":
        pd.read_excel = read_excel_bytes
    try:
        if kind == "quotes":
            frame = akshare.fund_etf_category_sina(symbol="ETF基金")
        elif kind == "sse_scale":
            frame = akshare.fund_etf_scale_sse(date=arguments["date"])
        elif kind == "szse_scale":
            frame = akshare.fund_etf_scale_szse()
        elif kind == "asset_allocation":
            frame = akshare.fund_individual_detail_hold_xq(
                symbol=arguments["symbol"], date=arguments["date"], timeout=timeout,
            )
        else:
            raise ValueError("未知 ETF 源接口")
        queue.put(("ok", frame.to_dict("records")))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))
    finally:
        requests.get = original_get
        pd.read_excel = original_read_excel


class AkShareEtfProvider:
    """每次源调用在限时子进程中执行，主服务线程不共享猴子补丁。"""

    def __init__(self, timeout_seconds: int = 15) -> None:
        self.timeout_seconds = timeout_seconds

    def _call(self, kind: str, **arguments: str) -> list[dict[str, Any]]:
        context = mp.get_context("spawn")
        queue = context.Queue()
        process = context.Process(
            target=_worker, args=(queue, kind, arguments, self.timeout_seconds),
        )
        process.start()
        try:
            status, value = queue.get(timeout=self.timeout_seconds + 15)
        except Empty as exc:
            raise TimeoutError("ETF 数据源超时") from exc
        finally:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join()
            queue.close()
        if status != "ok":
            raise RuntimeError(f"ETF 数据源失败：{value}")
        return value

    def quotes(self) -> list[dict[str, Any]]:
        return self._call("quotes")

    def sse_scale(self, date: str) -> list[dict[str, Any]]:
        return self._call("sse_scale", date=date)

    def szse_scale(self) -> list[dict[str, Any]]:
        return self._call("szse_scale")

    def asset_allocation(self, symbol: str, report_period: str) -> list[dict[str, Any]]:
        return self._call("asset_allocation", symbol=symbol, date=report_period)
