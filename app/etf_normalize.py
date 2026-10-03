"""新浪 ETF 行情、交易所份额和雪球资产配置的纯转换。"""

import math
import re
from datetime import date
from typing import Any

ETF_SYMBOL = re.compile(r"^(sh|sz)[0-9]{6}$", re.IGNORECASE)


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
    return value if value and value.lower() not in {"nan", "nat", "none", "<na>"} else None


def _date(raw: Any) -> str | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(str(raw)[:10]).isoformat()
    except ValueError:
        return None


def etf_symbol(raw: Any) -> str | None:
    value = _text(raw)
    if not value or not ETF_SYMBOL.fullmatch(value):
        return None
    return value.upper()


def catalog(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """新浪 ETF 列表提供代码、名称和价格，不推断细分类别及跟踪指数。"""
    if not rows:
        raise ValueError("新浪 ETF 列表为空")
    etfs: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = etf_symbol(row.get("代码"))
        name = _text(row.get("名称"))
        if symbol is None or name is None:
            continue
        etfs[symbol] = {
            "symbol": symbol, "code": symbol[2:], "name": name,
            "market": symbol[:2], "exchange": "SSE" if symbol[:2] == "SH" else "SZSE",
            "etfType": "ETF", "listingStatus": None, "listingDate": None,
            "trackingIndexCode": None, "trackingIndexName": None,
            "source": "AKShare.fund_etf_category_sina",
        }
    if not etfs:
        raise ValueError("新浪 ETF 列表无有效代码")
    return [etfs[symbol] for symbol in sorted(etfs)]


def quote_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """只整理确实可解析的交易价格；基金净值不会进入报价。"""
    if not rows:
        raise ValueError("新浪 ETF 行情为空")
    quotes: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = etf_symbol(row.get("代码"))
        if symbol is None:
            continue
        price = _number(row.get("最新价"))
        if price is None or price <= 0:
            continue
        item = {
            "symbol": symbol, "code": symbol[2:], "name": _text(row.get("名称")),
            "market": symbol[:2], "price": price,
            "change": _number(row.get("涨跌额")),
            "changePercent": _number(row.get("涨跌幅")),
            "previousClose": _number(row.get("昨收")), "open": _number(row.get("今开")),
            "high": _number(row.get("最高")), "low": _number(row.get("最低")),
            "volume": _number(row.get("成交量")), "amount": _number(row.get("成交额")),
            "sourceTime": None, "closeConfirmed": False,
        }
        if symbol in quotes and quotes[symbol] != item:
            raise ValueError("新浪 ETF 行情重复代码冲突")
        quotes[symbol] = item
    if not quotes:
        raise ValueError("新浪 ETF 行情无有效交易价格")
    return quotes


def profiles(sse_rows: list[dict[str, Any]], szse_rows: list[dict[str, Any]],
             symbols: list[str]) -> list[dict[str, Any]]:
    requested = set(symbols)
    result: dict[str, dict[str, Any]] = {}
    for exchange, rows in (("SSE", sse_rows), ("SZSE", szse_rows)):
        for row in rows:
            code = _text(row.get("基金代码"))
            if not code or not re.fullmatch(r"\d{6}", code):
                continue
            symbol = ("SH" if exchange == "SSE" else "SZ") + code
            if symbol not in requested:
                continue
            result[symbol] = {
                "symbol": symbol, "code": code, "name": _text(row.get("基金简称")),
                "exchange": exchange,
                "etfType": _text(row.get("ETF类型") or row.get("基金类别")),
                "shareCount": _number(row.get("基金份额")),
                "shareDate": _date(row.get("统计日期")),
                "manager": _text(row.get("基金管理人")),
                "custodian": _text(row.get("基金托管人")),
                "listingDate": _date(row.get("上市日期")),
                "trackingIndexCode": None, "trackingIndexName": None,
                "source": "AKShare.fund_etf_scale_sse" if exchange == "SSE"
                          else "AKShare.fund_etf_scale_szse",
            }
    return [result[symbol] for symbol in symbols if symbol in result]


def asset_allocation(rows: list[dict[str, Any]], symbol: str,
                     report_period: str, collected_at: str) -> dict[str, Any]:
    categories = []
    for row in rows:
        category = _text(row.get("资产类型"))
        percent = _number(row.get("仓位占比"))
        if category is not None and percent is not None and 0 <= percent <= 100:
            categories.append({"category": category, "percent": percent})
    if not categories:
        raise ValueError("雪球资产配置无有效类别")
    return {
        "schemaVersion": 1, "symbol": symbol,
        "requestedReportPeriod": date.fromisoformat(
            f"{report_period[:4]}-{report_period[4:6]}-{report_period[6:]}"
        ).isoformat(),
        "source": "XQ_DANJUAN",
        "collectedAt": collected_at, "categories": categories,
    }
