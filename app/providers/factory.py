"""行情数据源工厂。"""

from app.providers.base import MarketDataProvider
from app.providers.mock import MockMarketDataProvider


def create_provider(name: str) -> MarketDataProvider:
    """按配置创建数据源，未知名称直接阻止应用启动。"""
    if name == "mock":
        return MockMarketDataProvider()
    raise ValueError(f"不支持的数据源: {name}")
