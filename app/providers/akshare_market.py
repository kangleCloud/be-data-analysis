"""市场源调用在独立进程中执行；标准化及发布由采集父进程完成。"""

import logging
import time
from typing import Any, Protocol

from app.providers.http import error_metadata
from app.runtime.source_execution import SourceCall, SourceExecutor, SourceCoolingError

LOGGER = logging.getLogger(__name__)
THS_SECTOR_TIMEOUT_SECONDS = 120
THS_INDIVIDUAL_TIMEOUT_SECONDS = 900
SINA_INDEX_TIMEOUT_SECONDS = 120


class MarketSource(Protocol):
    def sector_fund_flow(self, sector_type: str) -> Any: ...
    def market_fund_flow(self) -> Any: ...
    def index_spot(self) -> Any: ...


class AkShareMarketProvider:
    def __init__(self, timeout_seconds: int, *, executor: Any = None, api: Any = None) -> None:
        self.executor = executor if executor is not None else (None if api is not None else SourceExecutor.configured(timeout_seconds))
        self._akshare = api
        self._timeout_seconds = timeout_seconds

    def _call(self, function: str, *, budget: float, group: str,
              module: str, **parameters: Any) -> Any:
        started = time.monotonic()
        try:
            if self._akshare is not None:
                result = getattr(self._akshare, function)(**parameters)
            else:
                source = "ths" if group == "ths" else "sina-index"
                result = self.executor.call(SourceCall(function, group, parameters, budget,
                    ("data.10jqka.com.cn",) if group == "ths" else ("vip.stock.finance.sina.com.cn",),
                    (f"stock:market:v1:cooldown:{source}", f"stock:market:v1:cooldown:module:{module}"), "market"))
            LOGGER.info("接口 %s 成功，耗时 %.2f 秒", function, time.monotonic()-started)
            return result
        except SourceCoolingError as exc:
            LOGGER.info("接口 %s 冷却跳过，剩余 TTL %d 秒", function, exc.ttl)
            raise
        except Exception as exc:
            metadata = error_metadata(exc)
            LOGGER.warning("接口 %s 失败，耗时 %.2f 秒，异常 %s，底层异常 %s，HTTP %s，分类 %s",
                           function, time.monotonic()-started, metadata["exception_type"],
                           metadata["root_type"], metadata["http_status"], metadata["category"])
            raise

    def sector_fund_flow(self, sector_type: str) -> Any:
        function = "stock_fund_flow_industry" if sector_type == "industry" else "stock_fund_flow_concept"
        return self._call(function, budget=THS_SECTOR_TIMEOUT_SECONDS, group="ths",
                          module="industrySectors" if sector_type == "industry" else "conceptSectors", symbol="即时")

    def market_fund_flow(self) -> Any:
        return self._call("stock_fund_flow_individual", budget=THS_INDIVIDUAL_TIMEOUT_SECONDS, group="ths", module="marketFundFlow", symbol="即时")

    def index_spot(self) -> Any:
        return self._call("stock_zh_index_spot_sina", budget=SINA_INDEX_TIMEOUT_SECONDS, group="sina", module="coreIndices")
