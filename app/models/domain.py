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
    price_change: Decimal | None = None
    change_percent: Decimal | None = None
    turnover_rate: Decimal | None = None
    pre_close: Decimal | None = None
    ma5: Decimal | None = None
    ma5_volume: int | None = None
    ma10: Decimal | None = None
    ma10_volume: int | None = None
    ma20: Decimal | None = None
    ma20_volume: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "price_change": self.price_change,
            "change_percent": self.change_percent,
            "turnover_rate": self.turnover_rate,
            "pre_close": self.pre_close,
            "ma5": self.ma5,
            "ma5_volume": self.ma5_volume,
            "ma10": self.ma10,
            "ma10_volume": self.ma10_volume,
            "ma20": self.ma20,
            "ma20_volume": self.ma20_volume,
        }


@dataclass(frozen=True)
class StockInstrument:
    """名称解析后的 A 股标的。"""

    symbol: str
    name: str | None = None
    exchange: str | None = None


@dataclass(frozen=True)
class MarketHistory:
    """完成标准化后的历史行情查询结果。"""

    asset_type: AssetType
    symbol: str
    name: str | None
    provider: str
    mock_data: bool
    start_date: date
    end_date: date
    items: tuple[PriceBar, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_type": self.asset_type.value,
            "symbol": self.symbol,
            "name": self.name,
            "provider": self.provider,
            "mock_data": self.mock_data,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class MarketLatest:
    """某个证券最近交易日的日线结果。"""

    asset_type: AssetType
    symbol: str
    name: str | None
    provider: str
    mock_data: bool
    item: PriceBar

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_type": self.asset_type.value,
            "symbol": self.symbol,
            "name": self.name,
            "provider": self.provider,
            "mock_data": self.mock_data,
            "item": self.item.to_dict(),
        }
