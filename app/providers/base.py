"""行情数据源协议。"""

from datetime import date
from typing import Protocol, Sequence

from app.models.domain import AssetType, PriceBar


class ProviderError(Exception):
    """数据源已知故障，服务层会将其转换为 502。"""


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
