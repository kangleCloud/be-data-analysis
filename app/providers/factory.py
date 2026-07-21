"""行情数据源和证券名称解析器工厂。"""

from app.core.config import Settings
from app.providers.baidu_finance import BaiduFinanceClient, BaiduFinanceProvider
from app.providers.base import MarketDataProvider, StockSymbolResolver
from app.providers.eastmoney_symbol import EastmoneyStockSymbolResolver
from app.providers.mock import MockMarketDataProvider
from app.providers.mock_symbol import MockStockSymbolResolver


def create_provider(settings: Settings) -> MarketDataProvider:
    """按配置创建数据源，未知名称直接阻止应用启动。"""
    if settings.data_provider == "mock":
        return MockMarketDataProvider()
    if settings.data_provider == "baidu_finance":
        client = BaiduFinanceClient(
            base_url=settings.baidu_finance_base_url,
            timeout_seconds=settings.external_http_timeout_seconds,
            retry_count=settings.external_http_retry_count,
            ab_sr=settings.baidu_ab_sr,
        )
        return BaiduFinanceProvider(client)
    raise ValueError(f"不支持的数据源: {settings.data_provider}")


def create_symbol_resolver(settings: Settings) -> StockSymbolResolver:
    """按配置创建股票名称解析器。"""
    if settings.symbol_resolver == "mock":
        return MockStockSymbolResolver()
    if settings.symbol_resolver == "eastmoney":
        return EastmoneyStockSymbolResolver(
            base_url=settings.eastmoney_search_base_url,
            timeout_seconds=settings.external_http_timeout_seconds,
            retry_count=settings.external_http_retry_count,
            cache_ttl_seconds=settings.symbol_cache_ttl_seconds,
        )
    raise ValueError(f"不支持的名称解析器: {settings.symbol_resolver}")
