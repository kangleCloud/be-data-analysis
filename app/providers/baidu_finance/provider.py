"""百度财经 Provider 实现。"""

from datetime import date

from app.models.domain import AssetType, PriceBar
from app.providers.baidu_finance.client import BaiduFinanceClient
from app.providers.baidu_finance.processing import parse_daily_kline


class BaiduFinanceProvider:
    """从百度财经公开接口获取标准化日 K。"""

    def __init__(self, client: BaiduFinanceClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "baidu_finance"

    @property
    def is_mock(self) -> bool:
        return False

    def fetch_history(
        self,
        asset_type: AssetType,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> tuple[PriceBar, ...]:
        payload = self._client.fetch_daily_kline(asset_type, symbol)
        items = parse_daily_kline(payload)
        return tuple(item for item in items if start_date <= item.trade_date <= end_date)
