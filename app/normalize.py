"""将 AKShare 同花顺 DataFrame 转换为市场快照 JSON。"""

import math
import re
import logging
from collections import Counter
from typing import Any


class SourceDataError(ValueError):
    """源字段不完整或结果不可用。"""


LOGGER = logging.getLogger(__name__)


def _rows(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        raise SourceDataError("数据源返回空结果")
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else frame
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise SourceDataError("数据源返回空结果")
    return rows


def _require(rows: list[dict[str, Any]], *columns: str) -> None:
    if not all(set(columns).issubset(row) for row in rows):
        raise SourceDataError("数据源字段变化")


def _number(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(str(raw).replace(",", "").strip().removesuffix("%"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _text(raw: Any) -> str | None:
    value = str(raw).strip() if raw is not None else ""
    return None if value.lower() in {"", "nan", "nat", "<na>", "none", "-"} else value


def _money(raw: Any, *, numeric_factor: float) -> float | None:
    """字符串采用显式单位；纯数值采用对应接口表头单位。"""
    if isinstance(raw, str):
        value = raw.strip().replace(",", "")
        for suffix, factor in (("亿元", 1e8), ("亿", 1e8), ("万元", 1e4), ("万", 1e4), ("元", 1.0)):
            if value.endswith(suffix):
                number = _number(value.removesuffix(suffix))
                return number * factor if number is not None else None
        if value.endswith("%"):
            return None
    number = _number(raw)
    return number * numeric_factor if number is not None else None


def _count(raw: Any) -> int | None:
    number = _number(raw)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def normalize_sectors(frame: Any, sector_type: str) -> dict[str, Any]:
    """保留完整板块行；排行由读取方按有效数值过滤计算。"""
    if sector_type not in {"industry", "concept"}:
        raise ValueError("板块类型不正确")
    rows = _rows(frame)
    _require(rows, "行业", "行业指数", "行业-涨跌幅", "流入资金", "流出资金", "净额")
    names = [_text(row["行业"]) for row in rows]
    name_counts = Counter(names)
    items = []
    for row, name in zip(rows, names):
        if name is None or name_counts[name] != 1:
            continue
        inflow = _money(row["流入资金"], numeric_factor=1e8)
        outflow = _money(row["流出资金"], numeric_factor=1e8)
        net_amount = _money(row["净额"], numeric_factor=1e8)
        denominator = inflow + outflow if inflow is not None and outflow is not None else None
        index_value = _number(row["行业指数"])
        change = _number(row["行业-涨跌幅"])
        if all(value is None for value in (index_value, change, inflow, outflow, net_amount)):
            continue
        code = _text(row.get("板块代码") or row.get("代码"))
        items.append({
            "code": code if code and re.fullmatch(r"[A-Za-z0-9]+", code) else None,
            "name": name,
            "type": sector_type,
            "indexValue": index_value,
            "changePct": change,
            "inflow": inflow,
            "outflow": outflow,
            "netAmount": net_amount,
            "netFlowRate": (
                net_amount / denominator * 100
                if net_amount is not None and denominator is not None and denominator > 0
                else None
            ),
            "companyCount": _count(row.get("公司家数")),
            "leader": _text(row.get("领涨股")),
            "leaderChangePct": _number(row.get("领涨股-涨跌幅")),
            "leaderPrice": _number(row.get("当前价")),
        })
    if not items:
        raise SourceDataError("无有效同花顺板块资金流数据")
    return {"source": "THS", "period": "INTRADAY", "items": items}


def normalize_individual_batch(
    frame: Any, collected_at: str,
) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    """从同一批去重股票构造市场汇总与个股资金点。"""
    rows = _rows(frame)
    _require(rows, "股票代码", "股票简称", "涨跌幅", "流入资金", "流出资金", "净额")
    stocks: dict[str, tuple[str, tuple[float, float, float, float]]] = {}
    for row in rows:
        raw_code = row["股票代码"]
        if isinstance(raw_code, (int, float)) and not isinstance(raw_code, bool):
            code = (
                str(int(raw_code)).zfill(6)
                if math.isfinite(raw_code) and raw_code >= 0 and raw_code == int(raw_code)
                else None
            )
        else:
            code = _text(raw_code)
            if code is not None and code.isdigit() and len(code) < 6:
                code = code.zfill(6)
        name = _text(row["股票简称"])
        if code is None or not re.fullmatch(r"\d{6}", code) or name is None:
            continue
        change = _number(row["涨跌幅"])
        inflow = _money(row["流入资金"], numeric_factor=1.0)
        outflow = _money(row["流出资金"], numeric_factor=1.0)
        source_net = _money(row["净额"], numeric_factor=1.0)
        if any(value is None for value in (change, inflow, outflow, source_net)):
            raise SourceDataError("个股资金行缺少必要数值，拒绝部分汇总")
        values = (change, inflow, outflow, source_net)
        if code in stocks and stocks[code] != (name, values):
            raise SourceDataError("个股资金重复行互相冲突")
        stocks[code] = (name, values)
    if not stocks:
        raise SourceDataError("无有效同花顺个股资金流数据")
    values = [entry[1] for entry in stocks.values()]
    inflow_total = sum(row[1] for row in values)
    outflow_total = sum(row[2] for row in values)
    source_net_total = sum(row[3] for row in values)
    net_total = inflow_total - outflow_total
    if not math.isclose(source_net_total, net_total, rel_tol=0, abs_tol=max(1.0, len(values))):
        LOGGER.info(
            "同花顺个股源净额与流入减流出存在差异：样本数 %d，差额 %.2f 元",
            len(values), source_net_total - net_total,
        )
    latest = {
        "collectedAt": collected_at,
        "inflow": inflow_total,
        "outflow": outflow_total,
        "netAmount": net_total,
        "riseCount": sum(row[0] > 0 for row in values),
        "fallCount": sum(row[0] < 0 for row in values),
        "flatCount": sum(row[0] == 0 for row in values),
        "stockCount": len(values),
    }
    if latest["riseCount"] + latest["fallCount"] + latest["flatCount"] != latest["stockCount"]:
        raise SourceDataError("个股涨跌样本计数不一致")
    point = {field: latest[field] for field in ("collectedAt", "inflow", "outflow", "netAmount")}
    by_code = {
        code: {"collectedAt": collected_at, "inflow": value[1],
               "outflow": value[2], "netAmount": value[1] - value[2]}
        for code, (_name, value) in stocks.items()
    }
    return {
        "source": "THS_INDIVIDUAL_AGGREGATE", "latest": latest,
        "series": [point], "reconciledFromLegacy": False,
    }, by_code


def normalize_individual_aggregate(frame: Any, collected_at: str) -> dict[str, Any]:
    return normalize_individual_batch(frame, collected_at)[0]
