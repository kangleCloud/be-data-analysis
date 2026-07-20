"""用于本地开发和测试的确定性模拟行情数据源。"""

from datetime import date
from decimal import Decimal

from app.models.domain import AssetType, PriceBar


def _bar(
    trade_date: date,
    open_price: str,
    high_price: str,
    low_price: str,
    close_price: str,
    volume: int,
    amount: str,
) -> PriceBar:
    return PriceBar(
        trade_date=trade_date,
        open=Decimal(open_price),
        high=Decimal(high_price),
        low=Decimal(low_price),
        close=Decimal(close_price),
        volume=volume,
        amount=Decimal(amount),
    )


STOCK_BARS = (
    _bar(date(2024, 1, 2), "10.00", "10.20", "9.90", "10.10", 100_000, "1005000.00"),
    _bar(date(2024, 1, 3), "10.10", "10.35", "10.05", "10.30", 120_000, "1224000.00"),
    _bar(date(2024, 1, 4), "10.30", "10.40", "10.15", "10.20", 90_000, "921000.00"),
)

FUND_BARS = (
    _bar(date(2024, 1, 2), "3.500", "3.530", "3.490", "3.520", 200_000, "702000.00"),
    _bar(date(2024, 1, 3), "3.520", "3.550", "3.510", "3.540", 220_000, "777700.00"),
    _bar(date(2024, 1, 4), "3.540", "3.560", "3.500", "3.510", 180_000, "635400.00"),
)


class MockMarketDataProvider:
    """返回固定样例数据，响应会明确标记为模拟数据。"""

    @property
    def name(self) -> str:
        return "mock"

    @property
    def is_mock(self) -> bool:
        return True

    def fetch_history(
        self,
        asset_type: AssetType,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> tuple[PriceBar, ...]:
        del symbol  # Mock 只演示数据流，不模拟证券之间的差异。
        source = STOCK_BARS if asset_type is AssetType.STOCK else FUND_BARS
        return tuple(bar for bar in source if start_date <= bar.trade_date <= end_date)
