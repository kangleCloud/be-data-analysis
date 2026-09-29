"""三家交易所 A 股代码字典。"""

import pandas as pd
import pytest
from functools import lru_cache

from app.providers.exchange_stocks import ExchangeStockProvider


class FakeExchange:
    def __init__(self):
        self.calls = []

    def stock_info_sh_name_code(self, symbol):
        self.calls.append(symbol)
        code = "600000" if symbol == "主板A股" else "688001"
        return pd.DataFrame([{"证券代码": code, "证券简称": "上海股票"}])

    def stock_info_sz_name_code(self, symbol):
        self.calls.append(symbol)
        return pd.DataFrame([{"A股代码": "000001", "A股简称": "深圳股票"}])

    def stock_info_bj_name_code(self):
        self.calls.append("北交所")
        return pd.DataFrame([{"证券代码": "920262", "证券简称": "北京股票"}])


def test_exchange_dictionary_includes_main_star_sz_and_bj():
    source = FakeExchange()
    stocks = ExchangeStockProvider(source=source).all_a_stocks()
    assert [item["symbol"] for item in stocks] == [
        "BJ920262", "SH600000", "SH688001", "SZ000001"
    ]
    assert stocks[0] == {
        "symbol": "BJ920262", "code": "920262", "name": "北京股票", "market": "BJ"
    }
    assert source.calls == ["主板A股", "科创板", "A股列表", "北交所"]


def test_exchange_dictionary_rejects_partial_source_result():
    source = FakeExchange()
    source.stock_info_bj_name_code = lambda: pd.DataFrame(columns=["证券代码", "证券简称"])
    with pytest.raises(ValueError, match="为空"):
        ExchangeStockProvider(source=source).all_a_stocks()


def test_daily_sync_clears_akshare_process_cache():
    class CachedExchange(FakeExchange):
        name = "首日名称"

        @lru_cache
        def stock_info_sh_name_code(self, symbol):
            self.calls.append(symbol)
            code = "600000" if symbol == "主板A股" else "688001"
            return pd.DataFrame([{"证券代码": code, "证券简称": self.name}])

    source = CachedExchange()
    provider = ExchangeStockProvider(source=source)
    assert next(item for item in provider.all_a_stocks() if item["symbol"] == "SH600000")["name"] == "首日名称"
    source.name = "次日名称"
    assert next(item for item in provider.all_a_stocks() if item["symbol"] == "SH600000")["name"] == "次日名称"
    assert source.calls.count("主板A股") == 2
