"""同花顺即时板块与个股汇总字段、单位和异常边界。"""

import json

import pandas as pd
import pytest

from app.normalize import SourceDataError, normalize_individual_aggregate, normalize_sectors

COLLECTED_AT = "2026-09-23T10:00:00+08:00"


def test_sector_complete_items_units_and_net_flow_rate(flow_rows):
    data = normalize_sectors(flow_rows, "industry")
    assert set(data) == {"source", "period", "items"}
    assert (data["source"], data["period"]) == ("THS", "INTRADAY")
    first = data["items"][0]
    assert first == {
        "code": None, "name": "半导体", "type": "industry",
        "indexValue": 1234.5, "changePct": 3.5,
        "inflow": 200_000_000, "outflow": 50_000_000,
        "netAmount": 150_000_000, "netFlowRate": 60.0,
        "companyCount": 55, "leader": "芯片甲", "leaderChangePct": 9.9,
        "leaderPrice": 23.5,
    }
    assert data["items"][1]["netFlowRate"] == -50.0
    assert type(json.loads(json.dumps(data))["items"][0]["companyCount"]) is int


@pytest.mark.parametrize("raw,expected", [
    (105, 105), (105.0, 105), ("105", 105), ("105.0", 105),
    (0, 0), (-1, None), (1.5, None), ("-", None), (None, None),
    (True, None),
])
def test_company_count_only_emits_nonnegative_json_integer(flow_rows, raw, expected):
    rows = flow_rows.iloc[[0]].copy()
    rows["公司家数"] = rows["公司家数"].astype(object)
    rows.loc[rows.index[0], "公司家数"] = raw
    item = json.loads(json.dumps(normalize_sectors(rows, "industry")))["items"][0]
    assert item["companyCount"] == expected
    if expected is not None:
        assert type(item["companyCount"]) is int


def test_sector_missing_optional_values_are_null_and_code_only_if_real(flow_rows):
    rows = flow_rows.iloc[[0]].copy()
    rows["板块代码"] = "BK1234"
    rows["流入资金"] = "-"
    rows["流出资金"] = "-"
    rows["公司家数"] = None
    item = normalize_sectors(rows, "concept")["items"][0]
    assert item["code"] == "BK1234"
    assert item["inflow"] is None and item["netFlowRate"] is None
    assert item["companyCount"] is None


def test_sector_invalid_and_duplicate_names_are_excluded(flow_rows):
    rows = pd.concat([flow_rows, flow_rows.iloc[[0]]], ignore_index=True)
    assert [item["name"] for item in normalize_sectors(rows, "concept")["items"]] == ["银行"]


def test_sector_missing_required_columns_or_all_invalid_are_errors(flow_rows):
    with pytest.raises(SourceDataError):
        normalize_sectors(flow_rows.drop(columns="净额"), "industry")
    with pytest.raises(SourceDataError):
        normalize_sectors(flow_rows.iloc[0:0], "industry")
    with pytest.raises(SourceDataError):
        normalize_sectors(flow_rows.assign(行业指数=None, **{
            "行业-涨跌幅": None, "流入资金": None, "流出资金": None, "净额": None,
        }), "industry")


def test_individual_aggregate_sums_source_net_and_counts_stocks(market_rows):
    data = normalize_individual_aggregate(market_rows, COLLECTED_AT)
    assert data["source"] == "THS_INDIVIDUAL_AGGREGATE"
    assert data["latest"] == {
        "collectedAt": COLLECTED_AT,
        "inflow": 130_000_000, "outflow": 90_000_000,
        "netAmount": 40_000_000,
        "riseCount": 1, "fallCount": 1, "flatCount": 0, "stockCount": 2,
    }
    assert data["series"] == [{
        "collectedAt": COLLECTED_AT,
        "inflow": 130_000_000, "outflow": 90_000_000, "netAmount": 40_000_000,
    }]


def test_individual_excludes_nonstock_and_exact_duplicate_rows(market_rows):
    rows = pd.concat([
        market_rows,
        market_rows.iloc[[0]],
        pd.DataFrame([{"股票代码": "汇总", "股票简称": "合计", "涨跌幅": 0,
                       "流入资金": "1亿", "流出资金": "1亿", "净额": "0亿"}]),
    ], ignore_index=True)
    data = normalize_individual_aggregate(rows, COLLECTED_AT)
    assert data["latest"]["stockCount"] == 2
    assert data["latest"]["netAmount"] == 40_000_000


def test_individual_numeric_code_keeps_leading_zero_stock(market_rows):
    rows = market_rows.copy()
    rows["股票代码"] = rows["股票代码"].astype(object)
    rows.loc[1, "股票代码"] = 1
    assert normalize_individual_aggregate(rows, COLLECTED_AT)["latest"]["stockCount"] == 2


def test_individual_partial_amount_or_conflicting_duplicate_is_error(market_rows):
    bad = market_rows.copy()
    bad.loc[1, "净额"] = "-"
    with pytest.raises(SourceDataError, match="部分汇总"):
        normalize_individual_aggregate(bad, COLLECTED_AT)
    conflicting = pd.concat([market_rows, market_rows.iloc[[0]].assign(净额="2亿")])
    with pytest.raises(SourceDataError, match="冲突"):
        normalize_individual_aggregate(conflicting, COLLECTED_AT)
