"""API 请求与响应数据模型。"""

from datetime import date
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator


class PriceBarResponse(BaseModel):
    """单个交易日的标准化行情。"""

    trade_date: date = Field(description="交易日期")
    open: Decimal = Field(description="开盘价")
    high: Decimal = Field(description="最高价")
    low: Decimal = Field(description="最低价")
    close: Decimal = Field(description="收盘价")
    volume: int = Field(description="成交量")
    amount: Decimal = Field(description="成交额")
    price_change: Decimal | None = Field(default=None, description="涨跌额")
    change_percent: Decimal | None = Field(default=None, description="涨跌幅，单位百分比")
    turnover_rate: Decimal | None = Field(default=None, description="换手率，单位百分比")
    pre_close: Decimal | None = Field(default=None, description="前一交易日收盘价")
    ma5: Decimal | None = Field(default=None, description="五日均价")
    ma5_volume: int | None = Field(default=None, description="五日平均成交量")
    ma10: Decimal | None = Field(default=None, description="十日均价")
    ma10_volume: int | None = Field(default=None, description="十日平均成交量")
    ma20: Decimal | None = Field(default=None, description="二十日均价")
    ma20_volume: int | None = Field(default=None, description="二十日平均成交量")


class MarketHistoryResponse(BaseModel):
    """股票或场内 ETF 的历史行情查询结果。"""

    asset_type: str = Field(description="资产类型：stock 或 fund")
    symbol: str = Field(description="六位证券代码")
    name: str | None = Field(default=None, description="证券名称")
    provider: str = Field(description="数据源名称")
    mock_data: bool = Field(description="是否为模拟数据")
    start_date: date = Field(description="查询开始日期")
    end_date: date = Field(description="查询结束日期")
    items: list[PriceBarResponse] = Field(description="按交易日期升序排列的行情")


class MarketLatestResponse(BaseModel):
    """股票最近交易日的日线结果。"""

    asset_type: str = Field(description="资产类型：stock")
    symbol: str = Field(description="六位证券代码")
    name: str | None = Field(default=None, description="证券名称")
    provider: str = Field(description="数据源名称")
    mock_data: bool = Field(description="是否为模拟数据")
    item: PriceBarResponse = Field(description="最近交易日行情")


class StockLookupRequest(BaseModel):
    """按股票名称或代码查询的公共请求字段。"""

    symbol: str | None = Field(
        default=None,
        pattern=r"^\d{6}$",
        description="六位 A 股代码，与 name 二选一",
        examples=["603339"],
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=40,
        description="A 股完整名称，与 symbol 二选一",
        examples=["四方科技"],
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        """去除名称两侧空白并拒绝纯空白名称。"""
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("股票名称不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_lookup_fields(self):
        """股票名称和代码必须且只能提供一个。"""
        if (self.symbol is None) == (self.name is None):
            raise ValueError("股票名称和股票代码必须且只能提供一个")
        return self


class StockHistoryRequest(StockLookupRequest):
    """股票历史日 K 查询请求。"""

    start_date: date = Field(description="查询开始日期，格式 YYYY-MM-DD")
    end_date: date = Field(description="查询结束日期，格式 YYYY-MM-DD")


class StockLatestRequest(StockLookupRequest):
    """股票最新日线查询请求。"""


class HealthResponse(BaseModel):
    """服务健康状态。"""

    status: str = Field(description="服务状态")


DataT = TypeVar("DataT")


class ApiResponse(BaseModel, Generic[DataT]):
    """统一 API 响应。"""

    code: int
    msg: str
    data: DataT
