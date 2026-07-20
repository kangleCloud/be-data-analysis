"""行情获取、异常映射和标准化服务。"""

import logging
from datetime import date

from app.core.exceptions import DataProviderUnavailableError, InvalidQueryError
from app.models.domain import AssetType, MarketHistory
from app.providers.base import MarketDataProvider, ProviderError

LOGGER = logging.getLogger(__name__)


class MarketDataService:
    """协调 Provider，并向 API 层返回稳定的领域结果。"""

    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider

    def get_history(
        self,
        asset_type: AssetType,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> MarketHistory:
        """查询并标准化日期区间内的历史行情。"""
        if start_date > end_date:
            raise InvalidQueryError("开始日期不能晚于结束日期")

        try:
            raw_items = self._provider.fetch_history(
                asset_type,
                symbol,
                start_date,
                end_date,
            )
        except ProviderError as exc:
            LOGGER.warning("provider %s failed: %s", self._provider.name, exc)
            raise DataProviderUnavailableError() from exc

        # 再次过滤并排序，避免不同 Provider 的边界和顺序差异泄漏到 API。
        items = tuple(
            sorted(
                (item for item in raw_items if start_date <= item.trade_date <= end_date),
                key=lambda item: item.trade_date,
            )
        )
        return MarketHistory(
            asset_type=asset_type,
            symbol=symbol,
            provider=self._provider.name,
            mock_data=self._provider.is_mock,
            start_date=start_date,
            end_date=end_date,
            items=items,
        )
