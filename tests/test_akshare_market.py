"""AKShare 接口选择和交易日历适配。"""

from datetime import date
from types import SimpleNamespace

import pandas as pd

from app.providers.akshare_market import AkShareMarketProvider


def test_adapter_uses_expected_akshare_functions():
    calls = []

    def capture(name):
        def invoke(**kwargs):
            calls.append((name, kwargs))
            return pd.DataFrame([{"trade_date": date(2026, 9, 23)}])
        return invoke

    provider = AkShareMarketProvider(2)
    provider._akshare = SimpleNamespace(
        tool_trade_date_hist_sina=capture("calendar"),
        stock_board_industry_name_em=capture("industry"),
        stock_board_concept_name_em=capture("concept"),
        stock_sector_fund_flow_rank=capture("flow"),
        stock_market_fund_flow=capture("market"),
    )
    assert provider.latest_trading_date(date(2026, 9, 23)) == date(2026, 9, 23)
    provider.sector_quotes("industry")
    provider.sector_quotes("concept")
    provider.sector_fund_flow("industry")
    provider.sector_fund_flow("concept")
    provider.market_fund_flow()
    assert calls == [
        ("calendar", {}),
        ("industry", {}),
        ("concept", {}),
        ("flow", {"indicator": "今日", "sector_type": "行业资金流"}),
        ("flow", {"indicator": "今日", "sector_type": "概念资金流"}),
        ("market", {}),
    ]
