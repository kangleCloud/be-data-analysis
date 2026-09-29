"""AKShare 字段转换与排行榜口径。"""

from datetime import date, timedelta

import pandas as pd
import pytest

from app.normalize import (
    SourceDataError,
    normalize_market_fund_flow,
    normalize_top5,
)


def test_top5_units_and_signs(flow_rows):
    top5 = normalize_top5(flow_rows, "industry")
    assert set(top5) == {"source", "period", "topRise", "topFall", "topInflow", "topOutflow"}
    assert (top5["source"], top5["period"]) == ("THS", "INTRADAY")
    assert top5["topRise"][0] == {
        "sectorName": "半导体", "sectorType": "industry",
        "changePercent": 3.5, "netFlowAmount": 150_000_000,
    }
    assert top5["topFall"][0]["sectorName"] == "银行"
    assert top5["topInflow"][0]["netFlowAmount"] == 150_000_000
    assert top5["topOutflow"][0]["netFlowAmount"] == -200_000_000


def test_invalid_values_are_excluded(flow_rows):
    flow_rows["净额"] = flow_rows["净额"].astype(object)
    flow_rows.loc[0, "净额"] = "-"
    top5 = normalize_top5(flow_rows, "concept")
    assert top5["topInflow"] == []
    assert top5["topOutflow"][0]["sectorType"] == "concept"


def test_duplicate_and_empty_names_are_excluded(flow_rows):
    duplicated = pd.concat([
        flow_rows,
        flow_rows.iloc[[0]],
        pd.DataFrame([{"行业": "", "行业-涨跌幅": 9, "净额": 9}]),
    ], ignore_index=True)
    top5 = normalize_top5(duplicated, "concept")
    assert top5["topInflow"] == []
    assert [item["sectorName"] for item in top5["topOutflow"]] == ["银行"]


def test_missing_columns_and_empty_are_errors(flow_rows):
    with pytest.raises(SourceDataError):
        normalize_top5(flow_rows.iloc[0:0], "industry")
    with pytest.raises(SourceDataError):
        normalize_top5(flow_rows.drop(columns="净额"), "industry")
    with pytest.raises(SourceDataError):
        normalize_top5(flow_rows.assign(净额=float("nan")), "industry")


def test_top5_sorts_by_change_and_absolute_amount_with_name_ties():
    rows = pd.DataFrame([
        {"行业": name, "行业-涨跌幅": change, "净额": amount}
        for name, change, amount in [
            ("乙", 2, 3), ("甲", 2, 3), ("丙", -2, -4), ("丁", -2, -4),
            ("戊", 1, -5), ("己", -1, 5), ("庚", 0, 0),
        ]
    ])
    top5 = normalize_top5(rows, "industry")
    assert [item["sectorName"] for item in top5["topRise"]] == ["乙", "甲", "戊"]
    assert [item["sectorName"] for item in top5["topFall"]] == ["丁", "丙", "己"]
    assert [item["sectorName"] for item in top5["topInflow"]] == ["己", "乙", "甲"]
    assert [item["sectorName"] for item in top5["topOutflow"]] == ["戊", "丁", "丙"]


def test_top5_limits_each_ranking_to_five():
    rows = pd.DataFrame([
        {"行业": f"板块{index}", "行业-涨跌幅": index, "净额": index}
        for index in range(1, 8)
    ])
    top5 = normalize_top5(rows, "concept")
    assert len(top5["topRise"]) == len(top5["topInflow"]) == 5
    assert top5["topRise"][0]["sectorName"] == "板块7"


def test_market_flow_actual_date_and_last_20(market_rows):
    start = date(2026, 8, 1)
    older = pd.DataFrame([
        {"日期": start + timedelta(days=index), "主力净流入-净额": index}
        for index in range(40)
    ])
    date_value, data = normalize_market_fund_flow(pd.concat([older, market_rows]))
    assert date_value == "2026-09-23"
    assert len(data["series"]) == 20
    assert data["latest"]["mainNetInflow"] == 500_000_000
    assert data["latest"]["largeNetInflow"] == 200_000_000
