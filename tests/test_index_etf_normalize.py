"""新浪指数和 ETF 的字段变化及数据语义边界。"""

import pytest

from app.etf_normalize import asset_allocation, quote_rows
from app.normalize import CORE_INDICES, SourceDataError, normalize_core_indices

COLLECTED_AT = "2026-09-28T09:32:00+08:00"


def _index_rows():
    return [{"代码": code, "名称": name, "最新价": "3000.5", "涨跌额": "12",
             "涨跌幅": "0.4", "昨收": "2988.5", "今开": "2990", "最高": "3010",
             "最低": "2980", "成交量": "1234", "成交额": "5678"}
            for code, name in CORE_INDICES.items()]


def test_five_core_indices_require_complete_batch_and_source_time_is_unknown():
    rows = _index_rows()
    data = normalize_core_indices(rows, COLLECTED_AT)
    assert [item["code"] for item in data["items"]] == list(CORE_INDICES)
    assert all(item["sourceTime"] is None for item in data["items"])
    assert data["items"][0]["series"] == [{"collectedAt": COLLECTED_AT,
                                           "price": 3000.5}]
    with pytest.raises(SourceDataError, match="不完整"):
        normalize_core_indices(rows[:-1], COLLECTED_AT)
    with pytest.raises(SourceDataError, match="字段变化"):
        normalize_core_indices([{key: value for key, value in row.items()
                                 if key != "成交额"} for row in rows], COLLECTED_AT)
    with pytest.raises(SourceDataError, match="冲突"):
        normalize_core_indices(rows + [{**rows[0], "最新价": "3001"}], COLLECTED_AT)


def test_etf_quotes_require_trading_price_and_do_not_infer_time_or_fund_flow():
    rows = [{"代码": "sh510050", "名称": "50ETF", "最新价": "2.5",
             "单位净值": "3.2", "涨跌额": "0.01"}]
    quote = quote_rows(rows)["SH510050"]
    assert quote["price"] == 2.5
    assert quote["sourceTime"] is None
    assert quote["closeConfirmed"] is False
    assert "单位净值" not in quote
    with pytest.raises(ValueError, match="有效交易价格"):
        quote_rows([{**rows[0], "最新价": "-"}])


def test_asset_allocation_is_categories_for_requested_report_period():
    result = asset_allocation(
        [{"资产类型": "股票", "仓位占比": "95.2%"}], "SH510050", "20260630",
        COLLECTED_AT,
    )
    assert result["requestedReportPeriod"] == "2026-06-30"
    assert result["categories"] == [{"category": "股票", "percent": 95.2}]
    assert "constituents" not in result
