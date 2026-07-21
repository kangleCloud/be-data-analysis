"""离线开发使用的确定性股票名称解析器。"""

from app.models.domain import StockInstrument
from app.providers.base import SymbolNotFoundError

MOCK_INSTRUMENTS = {
    "四方科技": StockInstrument(symbol="603339", name="四方科技", exchange="沪A"),
    "浦发银行": StockInstrument(symbol="600000", name="浦发银行", exchange="沪A"),
}


class MockStockSymbolResolver:
    """为测试和离线联调返回固定名称映射。"""

    @property
    def name(self) -> str:
        return "mock"

    def resolve(self, name: str) -> StockInstrument:
        try:
            return MOCK_INSTRUMENTS[name.strip()]
        except KeyError as exc:
            raise SymbolNotFoundError(name) from exc
