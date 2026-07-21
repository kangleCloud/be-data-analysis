"""行情数据源协议。"""

from datetime import date
from typing import Protocol, Sequence

from app.models.domain import AssetType, PriceBar, StockInstrument


class ProviderError(Exception):
    """数据源已知故障，服务层会将其转换为 502。"""


class SymbolNotFoundError(Exception):
    """名称解析器未找到精确匹配证券。"""


class AmbiguousSymbolError(Exception):
    """名称解析器返回多个精确匹配证券。"""


class MarketDataProvider(Protocol):
    """所有真实或模拟行情数据源都必须实现的接口。"""

    @property
    def name(self) -> str:
        """返回稳定的数据源标识。"""
        ...

    @property
    def is_mock(self) -> bool:
        """标识当前数据是否仅用于开发测试。"""
        ...

    def fetch_history(
        self,
        asset_type: AssetType,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> Sequence[PriceBar]:
        """获取指定资产在日期区间内的日线行情。"""
        ...


class StockSymbolResolver(Protocol):
    """股票名称到六位代码的解析协议。"""

    @property
    def name(self) -> str:
        """返回稳定的解析器标识。"""
        ...

    def resolve(self, name: str) -> StockInstrument:
        """按股票全名执行精确解析。"""
        ...
