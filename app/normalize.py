"""将 AKShare DataFrame 转换为跨服务 JSON 数据。"""

import math
from datetime import date
from typing import Any


class SourceDataError(ValueError):
    """源字段不完整或结果不可用。"""


def _rows(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        raise SourceDataError("数据源返回空结果")
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else frame
    if not isinstance(rows, list) or not rows:
        raise SourceDataError("数据源返回空结果")
    return rows


def _require(rows: list[dict[str, Any]], *columns: str) -> None:
    missing = set(columns) - set(rows[0])
    if missing:
        raise SourceDataError("数据源字段变化")


def _number(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _integer(raw: Any) -> int | None:
    value = _number(raw)
    return int(value) if value is not None else None


def _text(raw: Any) -> str:
    value = str(raw).strip() if raw is not None else ""
    return "" if value.lower() in {"nan", "nat", "<na>", "none"} else value


def normalize_sectors(frame: Any, sector_type: str) -> list[dict[str, Any]]:
    """板块面积采用源站总市值，不能从其他字段估算成交额。"""
    rows = _rows(frame)
    _require(rows, "板块代码", "板块名称", "涨跌幅", "总市值")
    sectors = []
    seen_codes = set()
    for row in rows:
        code, name = _text(row["板块代码"]), _text(row["板块名称"])
        cap = _number(row["总市值"])
        change = _number(row["涨跌幅"])
        if not code or not name or cap is None or cap <= 0 or change is None:
            continue
        if code in seen_codes:
            raise SourceDataError("板块代码重复")
        seen_codes.add(code)
        sectors.append({
            "sectorCode": code,
            "sectorName": name,
            "sectorType": sector_type,
            "marketCap": cap,
            "changePercent": change,
            "turnoverRate": _number(row.get("换手率")),
            "riseCount": _integer(row.get("上涨家数")),
            "fallCount": _integer(row.get("下跌家数")),
            "leadingStockName": _text(row.get("领涨股票")) or None,
        })
    if not sectors:
        raise SourceDataError("无有效板块数据")
    return sectors


def normalize_top5(frame: Any, sector_type: str) -> dict[str, Any]:
    """从同花顺即时板块资金流独立生成涨跌与净额榜。"""
    rows = _rows(frame)
    _require(rows, "行业", "行业-涨跌幅", "净额")
    name_counts: dict[str, int] = {}
    for row in rows:
        name = _text(row["行业"])
        name_counts[name] = name_counts.get(name, 0) + 1

    items = []
    for row in rows:
        name = _text(row["行业"])
        change = _number(row["行业-涨跌幅"])
        net_amount = _number(row["净额"])
        if not name or name_counts[name] != 1 or change is None or net_amount is None:
            continue
        items.append({
            "sectorName": name,
            "sectorType": sector_type,
            "changePercent": change,
            "netFlowAmount": net_amount * 100_000_000,
        })

    if not items:
        raise SourceDataError("无有效同花顺板块资金流数据")

    return {
        "source": "THS",
        "period": "INTRADAY",
        "topRise": sorted(
            (item for item in items if item["changePercent"] > 0),
            key=lambda item: (-item["changePercent"], item["sectorName"]),
        )[:5],
        "topFall": sorted(
            (item for item in items if item["changePercent"] < 0),
            key=lambda item: (item["changePercent"], item["sectorName"]),
        )[:5],
        "topInflow": sorted(
            (item for item in items if item["netFlowAmount"] > 0),
            key=lambda item: (-item["netFlowAmount"], item["sectorName"]),
        )[:5],
        "topOutflow": sorted(
            (item for item in items if item["netFlowAmount"] < 0),
            key=lambda item: (item["netFlowAmount"], item["sectorName"]),
        )[:5],
    }


def normalize_market_fund_flow(frame: Any) -> tuple[str, dict[str, Any]]:
    """只保留最新 20 个实际交易日，且交易日期取自源数据。"""
    rows = _rows(frame)
    _require(rows, "日期", "主力净流入-净额")
    by_date = {}
    for row in rows:
        try:
            trading_date = date.fromisoformat(_text(row["日期"])[:10])
        except ValueError:
            continue
        main = _number(row["主力净流入-净额"])
        if main is None:
            continue
        by_date[trading_date.isoformat()] = {
            "date": trading_date.isoformat(),
            "mainNetInflow": main,
            "mainNetInflowRatio": _number(row.get("主力净流入-净占比")),
            "superLargeNetInflow": _number(row.get("超大单净流入-净额")),
            "superLargeNetInflowRatio": _number(row.get("超大单净流入-净占比")),
            "largeNetInflow": _number(row.get("大单净流入-净额")),
            "largeNetInflowRatio": _number(row.get("大单净流入-净占比")),
            "mediumNetInflow": _number(row.get("中单净流入-净额")),
            "mediumNetInflowRatio": _number(row.get("中单净流入-净占比")),
            "smallNetInflow": _number(row.get("小单净流入-净额")),
            "smallNetInflowRatio": _number(row.get("小单净流入-净占比")),
            "shanghaiClose": _number(row.get("上证-收盘价")),
            "shanghaiChangePercent": _number(row.get("上证-涨跌幅")),
            "shenzhenClose": _number(row.get("深证-收盘价")),
            "shenzhenChangePercent": _number(row.get("深证-涨跌幅")),
        }
    if not by_date:
        raise SourceDataError("无有效大盘资金流数据")
    series = [by_date[key] for key in sorted(by_date)[-20:]]
    return series[-1]["date"], {"latest": series[-1], "series": series}
