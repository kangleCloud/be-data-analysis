"""AKShare 字段转换与排行榜口径。"""

from datetime import date, timedelta

import pandas as pd
import pytest

from app.normalize import (
    SourceDataError,
    normalize_market_fund_flow,
    normalize_sectors,
    normalize_top5,
)


def test_sector_and_top5_units_and_signs(sector_rows, flow_rows):
    sectors = normalize_sectors(sector_rows, "industry")
    top5 = normalize_top5(flow_rows, sectors)
    assert sectors[0]["marketCap"] == 1_000_000_000
    assert sectors[0]["changePercent"] == 3.5
    assert top5["topRise"][0]["sectorCode"] == "BK1"
    assert top5["topFall"][0]["sectorCode"] == "BK2"
    assert top5["topInflow"][0]["mainNetInflow"] == 150_000_000
    assert top5["topOutflow"][0]["mainNetInflow"] == -200_000_000
    assert top5["unmatchedFundRows"] == 0


def test_invalid_values_are_null_or_excluded(sector_rows, flow_rows):
    sector_rows.loc[0, "换手率"] = float("nan")
    sector_rows.loc[1, "总市值"] = -1
    sectors = normalize_sectors(sector_rows, "industry")
    assert len(sectors) == 1
    assert sectors[0]["turnoverRate"] is None
    flow_rows["今日主力净流入-净占比"] = flow_rows["今日主力净流入-净占比"].astype(object)
    flow_rows.loc[0, "今日主力净流入-净占比"] = "-"
    top5 = normalize_top5(flow_rows, sectors)
    assert top5["topInflow"][0]["mainNetInflowRatio"] is None
    assert top5["unmatchedFundRows"] == 1


def test_ambiguous_or_unknown_name_is_not_ranked(sector_rows, flow_rows):
    duplicated = pd.concat([sector_rows, sector_rows.iloc[[0]].assign(板块代码="BK3")])
    sectors = normalize_sectors(duplicated, "concept")
    flow_rows.loc[2] = ["不存在", 100, 1]
    top5 = normalize_top5(flow_rows, sectors)
    assert top5["unmatchedFundRows"] == 2
    assert not top5["topInflow"]
    assert top5["topOutflow"][0]["sectorCode"] == "BK2"


def test_missing_columns_and_empty_are_errors(sector_rows, flow_rows):
    with pytest.raises(SourceDataError):
        normalize_sectors(sector_rows.drop(columns="总市值"), "industry")
    with pytest.raises(SourceDataError):
        normalize_top5(flow_rows.iloc[0:0], normalize_sectors(sector_rows, "industry"))
    with pytest.raises(SourceDataError):
        normalize_top5(flow_rows.assign(名称="未知板块"), normalize_sectors(sector_rows, "industry"))


def test_duplicate_fund_names_are_excluded(sector_rows, flow_rows):
    duplicated = pd.concat([flow_rows, flow_rows.iloc[[0]]], ignore_index=True)
    top5 = normalize_top5(duplicated, normalize_sectors(sector_rows, "industry"))
    assert top5["unmatchedFundRows"] == 2
    assert not top5["topInflow"]


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
