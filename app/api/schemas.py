"""API 请求与响应数据模型。"""

from datetime import date
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, Field


class PriceBarResponse(BaseModel):
    """单个交易日的标准化行情。"""

    trade_date: date = Field(description="交易日期")
    open: Decimal = Field(description="开盘价")
    high: Decimal = Field(description="最高价")
    low: Decimal = Field(description="最低价")
    close: Decimal = Field(description="收盘价")
    volume: int = Field(description="成交量")
    amount: Decimal = Field(description="成交额")


class MarketHistoryResponse(BaseModel):
    """股票或场内 ETF 的历史行情查询结果。"""

    asset_type: str = Field(description="资产类型：stock 或 fund")
    symbol: str = Field(description="六位证券代码")
    provider: str = Field(description="数据源名称")
    mock_data: bool = Field(description="是否为模拟数据")
    start_date: date = Field(description="查询开始日期")
    end_date: date = Field(description="查询结束日期")
    items: list[PriceBarResponse] = Field(description="按交易日期升序排列的行情")


class HealthResponse(BaseModel):
    """服务健康状态。"""

    status: str = Field(description="服务状态")


DataT = TypeVar("DataT")


class ApiResponse(BaseModel, Generic[DataT]):
    """统一 API 响应。"""

    code: int
    msg: str
    data: DataT
