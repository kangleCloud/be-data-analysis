"""与具体数据源无关的行情领域模型。"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any


class AssetType(str, Enum):
    """首版支持的证券类型。"""

    STOCK = "stock"
    FUND = "fund"


@dataclass(frozen=True)
class PriceBar:
    """单个交易日的标准化 OHLCV 行情。"""

    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
        }


@dataclass(frozen=True)
class MarketHistory:
    """完成标准化后的历史行情查询结果。"""

    asset_type: AssetType
    symbol: str
    provider: str
    mock_data: bool
    start_date: date
    end_date: date
    items: tuple[PriceBar, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_type": self.asset_type.value,
            "symbol": self.symbol,
            "provider": self.provider,
            "mock_data": self.mock_data,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "items": [item.to_dict() for item in self.items],
        }
