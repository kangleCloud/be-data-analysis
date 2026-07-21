"""行情获取、异常映射和标准化服务。"""

import logging
from datetime import date

from app.core.exceptions import (
    DataProviderUnavailableError,
    InvalidQueryError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.models.domain import (
    AssetType,
    MarketHistory,
    MarketLatest,
    StockInstrument,
)
from app.providers.base import (
    AmbiguousSymbolError,
    MarketDataProvider,
    ProviderError,
    StockSymbolResolver,
    SymbolNotFoundError,
)

LOGGER = logging.getLogger(__name__)


class MarketDataService:
    """协调 Provider，并向 API 层返回稳定的领域结果。"""

    def __init__(
        self,
        provider: MarketDataProvider,
        symbol_resolver: StockSymbolResolver | None = None,
    ) -> None:
        self._provider = provider
        self._symbol_resolver = symbol_resolver

    def get_history(
        self,
        asset_type: AssetType,
        symbol: str,
        start_date: date,
        end_date: date,
        name: str | None = None,
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
        by_trade_date = {
            item.trade_date: item
            for item in raw_items
            if start_date <= item.trade_date <= end_date
        }
        items = tuple(by_trade_date[trade_date] for trade_date in sorted(by_trade_date))
        return MarketHistory(
            asset_type=asset_type,
            symbol=symbol,
            name=name,
            provider=self._provider.name,
            mock_data=self._provider.is_mock,
            start_date=start_date,
            end_date=end_date,
            items=items,
        )

    def get_stock_history(
        self,
        symbol: str | None,
        name: str | None,
        start_date: date,
        end_date: date,
    ) -> MarketHistory:
        """按代码或股票名称查询历史日 K。"""
        instrument = self._resolve_stock(symbol, name)
        return self.get_history(
            AssetType.STOCK,
            instrument.symbol,
            start_date,
            end_date,
            name=instrument.name,
        )

    def get_latest(
        self,
        asset_type: AssetType,
        symbol: str,
        name: str | None = None,
    ) -> MarketLatest:
        """返回截至今日最近一个交易日的日线。"""
        history = self.get_history(
            asset_type,
            symbol,
            date.min,
            date.today(),
            name=name,
        )
        if not history.items:
            raise ResourceNotFoundError("未找到最新日线行情")
        return MarketLatest(
            asset_type=asset_type,
            symbol=symbol,
            name=name,
            provider=history.provider,
            mock_data=history.mock_data,
            item=history.items[-1],
        )

    def get_stock_latest(
        self,
        symbol: str | None,
        name: str | None,
    ) -> MarketLatest:
        """按代码或股票名称查询最近交易日日线。"""
        instrument = self._resolve_stock(symbol, name)
        return self.get_latest(
            AssetType.STOCK,
            instrument.symbol,
            name=instrument.name,
        )

    def _resolve_stock(
        self,
        symbol: str | None,
        name: str | None,
    ) -> StockInstrument:
        if symbol is not None:
            return StockInstrument(symbol=symbol)
        if name is None:
            raise InvalidQueryError("必须提供股票名称或股票代码")
        if self._symbol_resolver is None:
            raise DataProviderUnavailableError()

        try:
            return self._symbol_resolver.resolve(name)
        except SymbolNotFoundError as exc:
            raise ResourceNotFoundError("未找到匹配的 A 股") from exc
        except AmbiguousSymbolError as exc:
            raise ResourceConflictError("股票名称对应多个 A 股代码") from exc
        except ProviderError as exc:
            LOGGER.warning(
                "symbol resolver %s failed: %s",
                self._symbol_resolver.name,
                exc,
            )
            raise DataProviderUnavailableError() from exc
