"""固定 AKShare 版本的新浪、同花顺与雪球基金接口隔离。"""

import multiprocessing as mp
import time
from queue import Empty
from typing import Any
from urllib.parse import urlparse

ALLOWED_HOSTS = {
    "quotes": {"vip.stock.finance.sina.com.cn"},
    "profile": {"fund.10jqka.com.cn"},
    "asset_allocation": {"danjuanfunds.com"},
}


def _worker(queue: Any, kind: str, arguments: dict[str, str], timeout: int) -> None:
    import akshare
    import requests

    original_get = requests.get
    last_request: float | None = None

    def allowed_get(url: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal last_request
        if urlparse(url).hostname not in ALLOWED_HOSTS[kind]:
            raise RuntimeError("ETF 接口请求了未审计域名")
        if last_request is not None:
            interval = 2 if kind == "profile" else 0.2
            time.sleep(max(0, interval - (time.monotonic() - last_request)))
        last_request = time.monotonic()
        kwargs["timeout"] = min(kwargs.get("timeout", timeout), timeout)
        if kind == "profile":
            kwargs["allow_redirects"] = False
        return original_get(url, *args, **kwargs)

    requests.get = allowed_get
    try:
        if kind == "quotes":
            frame = akshare.fund_etf_category_sina(symbol="ETF基金")
        elif kind == "profile":
            frame = akshare.fund_info_ths(symbol=arguments["symbol"])
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


class AkShareEtfProvider:
    """每次源调用在限时子进程中执行，主服务线程不共享猴子补丁。"""

    def __init__(self, timeout_seconds: int = 15) -> None:
        self.timeout_seconds = timeout_seconds

    def _call(self, kind: str, *, budget_seconds: float | None = None,
              **arguments: str) -> list[dict[str, Any]]:
        wait_seconds = self.timeout_seconds + 17
        if budget_seconds is not None:
            wait_seconds = min(wait_seconds, budget_seconds)
        if wait_seconds <= 2:
            raise TimeoutError("ETF 资料批次预算不足")
        deadline = time.monotonic() + wait_seconds
        context = mp.get_context("spawn")
        queue = context.Queue()
        process = context.Process(
            target=_worker, args=(queue, kind, arguments, self.timeout_seconds),
        )
        process.start()
        try:
            status, value = queue.get(timeout=max(0, deadline - time.monotonic() - 2))
        except Empty as exc:
            raise TimeoutError("ETF 数据源超时") from exc
        finally:
            process.join(timeout=max(0, min(2, deadline - time.monotonic())))
            if process.is_alive():
                process.terminate()
                process.join(timeout=max(0, deadline - time.monotonic()))
            queue.close()
        if status != "ok":
            raise RuntimeError(f"ETF 数据源失败：{value}")
        return value

    def quotes(self) -> list[dict[str, Any]]:
        return self._call("quotes")

    def profile(self, symbol: str, *, budget_seconds: float) -> list[dict[str, Any]]:
        return self._call("profile", symbol=symbol, budget_seconds=budget_seconds)

    def asset_allocation(self, symbol: str, report_period: str) -> list[dict[str, Any]]:
        return self._call("asset_allocation", symbol=symbol, date=report_period)
