"""健康检查及股票、场内 ETF 历史行情路由。"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api.schemas import (
    ApiResponse,
    HealthResponse,
    MarketHistoryResponse,
    MarketLatestResponse,
    StockHistoryRequest,
    StockLatestRequest,
)
from app.core import msg
from app.models.domain import AssetType
from app.service.market_data import MarketDataService

router = APIRouter()

SymbolPath = Annotated[
    str,
    Path(pattern=r"^\d{6}$", description="六位中国证券代码", examples=["600000"]),
]
StartDate = Annotated[date, Query(description="查询开始日期，格式 YYYY-MM-DD")]
EndDate = Annotated[date, Query(description="查询结束日期，格式 YYYY-MM-DD")]


def get_market_data_service(request: Request) -> MarketDataService:
    """从应用状态中取得行情服务，便于测试替换。"""
    return request.app.state.market_data_service


@router.get(
    "/health",
    tags=["系统接口"],
    summary="服务健康检查",
    response_description="返回当前服务状态。",
    response_model=ApiResponse[HealthResponse],
)
def health():
    """返回不依赖外部数据源的进程健康状态。"""
    return msg.ok({"status": "ok"}, message="服务正常")


def _history_response(
    asset_type: AssetType,
    symbol: str,
    start_date: date,
    end_date: date,
    service: MarketDataService,
):
    result = service.get_history(asset_type, symbol, start_date, end_date)
    return msg.ok(result.to_dict(), message="行情查询成功")


@router.get(
    "/api/v1/stocks/{symbol}/history",
    tags=["股票行情"],
    summary="查询 A 股历史行情",
    description="按日期区间查询中国 A 股日线行情，并返回标准化 OHLCV 数据。",
    response_description="返回按交易日期升序排列的股票行情。",
    response_model=ApiResponse[MarketHistoryResponse],
)
def stock_history(
    symbol: SymbolPath,
    start_date: StartDate,
    end_date: EndDate,
    service: MarketDataService = Depends(get_market_data_service),
):
    """查询中国 A 股历史行情。"""
    return _history_response(AssetType.STOCK, symbol, start_date, end_date, service)


@router.get(
    "/api/v1/stocks/{symbol}/latest",
    tags=["股票行情"],
    summary="查询 A 股最新日线",
    description="按六位代码返回截至今日最近一个交易日的日线，不代表盘中实时行情。",
    response_description="返回最近交易日的标准化日线行情。",
    response_model=ApiResponse[MarketLatestResponse],
)
def stock_latest(
    symbol: SymbolPath,
    service: MarketDataService = Depends(get_market_data_service),
):
    """按代码查询中国 A 股最新日线。"""
    result = service.get_latest(AssetType.STOCK, symbol)
    return msg.ok(result.to_dict(), message="最新日线查询成功")


@router.post(
    "/api/v1/stocks/history",
    tags=["股票行情"],
    summary="按名称或代码查询 A 股历史行情",
    description="股票名称和六位代码必须且只能提供一个，名称采用精确匹配。",
    response_description="返回按交易日期升序排列的股票行情。",
    response_model=ApiResponse[MarketHistoryResponse],
)
def stock_history_by_lookup(
    query: StockHistoryRequest,
    service: MarketDataService = Depends(get_market_data_service),
):
    """按名称或代码查询中国 A 股历史行情。"""
    result = service.get_stock_history(
        query.symbol,
        query.name,
        query.start_date,
        query.end_date,
    )
    return msg.ok(result.to_dict(), message="行情查询成功")


@router.post(
    "/api/v1/stocks/latest",
    tags=["股票行情"],
    summary="按名称或代码查询 A 股最新日线",
    description="返回最近交易日日线，不提供盘中实时价格。",
    response_description="返回最近交易日的标准化日线行情。",
    response_model=ApiResponse[MarketLatestResponse],
)
def stock_latest_by_lookup(
    query: StockLatestRequest,
    service: MarketDataService = Depends(get_market_data_service),
):
    """按名称或代码查询中国 A 股最新日线。"""
    result = service.get_stock_latest(query.symbol, query.name)
    return msg.ok(result.to_dict(), message="最新日线查询成功")


@router.get(
    "/api/v1/funds/{symbol}/history",
    tags=["基金行情"],
    summary="查询场内 ETF 历史行情",
    description="按日期区间查询中国场内 ETF 日线行情，并返回标准化 OHLCV 数据。",
    response_description="返回按交易日期升序排列的场内 ETF 行情。",
    response_model=ApiResponse[MarketHistoryResponse],
)
def fund_history(
    symbol: SymbolPath,
    start_date: StartDate,
    end_date: EndDate,
    service: MarketDataService = Depends(get_market_data_service),
):
    """查询中国场内 ETF 历史行情。"""
    return _history_response(AssetType.FUND, symbol, start_date, end_date, service)
