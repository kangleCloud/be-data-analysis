"""行情服务标准化行为测试。"""

from datetime import date
from decimal import Decimal

from app.models.domain import AssetType, PriceBar
from app.service.market_data import MarketDataService


def _bar(day: int) -> PriceBar:
    return PriceBar(
        trade_date=date(2024, 1, day),
        open=Decimal("1.0"),
        high=Decimal("1.1"),
        low=Decimal("0.9"),
        close=Decimal("1.0"),
        volume=100,
        amount=Decimal("100.0"),
    )


class _UnorderedProvider:
    name = "unordered"
    is_mock = False

    def fetch_history(self, asset_type, symbol, start_date, end_date):
        return [_bar(4), _bar(1), _bar(3), _bar(2), _bar(3)]


def test_service_filters_and_sorts_provider_results():
    service = MarketDataService(_UnorderedProvider())

    result = service.get_history(
        AssetType.STOCK,
        "600000",
        date(2024, 1, 2),
        date(2024, 1, 3),
    )

    assert [item.trade_date for item in result.items] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert result.provider == "unordered"
    assert result.mock_data is False
