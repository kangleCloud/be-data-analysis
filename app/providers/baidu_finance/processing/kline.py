"""百度财经日 K 数据解析。"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.domain import PriceBar
from app.providers.base import ProviderError

EMPTY_VALUES = {"", "-", "--", "null", "None"}


def _decimal(value: Any, *, required: bool = False) -> Decimal | None:
    text = str(value).strip() if value is not None else ""
    if text in EMPTY_VALUES:
        if required:
            raise ValueError("缺少必填数值")
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("数值格式非法") from exc


def _integer(value: Any, *, required: bool = False) -> int | None:
    number = _decimal(value, required=required)
    if number is None:
        return None
    if number != number.to_integral_value():
        raise ValueError("整数格式非法")
    return int(number)


def _trade_date(value: Any) -> date:
    text = str(value).strip()
    for separator in ("-", "/"):
        parts = text.split(separator)
        if len(parts) == 3:
            return date(*(int(part) for part in parts))
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    raise ValueError("交易日期格式非法")


def _to_price_bar(record: dict[str, Any]) -> PriceBar:
    return PriceBar(
        trade_date=_trade_date(record.get("time")),
        open=_decimal(record.get("open"), required=True),  # type: ignore[arg-type]
        high=_decimal(record.get("high"), required=True),  # type: ignore[arg-type]
        low=_decimal(record.get("low"), required=True),  # type: ignore[arg-type]
        close=_decimal(record.get("close"), required=True),  # type: ignore[arg-type]
        volume=_integer(record.get("volume"), required=True),  # type: ignore[arg-type]
        amount=_decimal(record.get("amount"), required=True),  # type: ignore[arg-type]
        price_change=_decimal(record.get("range")),
        change_percent=_decimal(record.get("ratio")),
        turnover_rate=_decimal(record.get("turnoverratio")),
        pre_close=_decimal(record.get("preClose")),
        ma5=_decimal(record.get("ma5avgprice")),
        ma5_volume=_integer(record.get("ma5volume")),
        ma10=_decimal(record.get("ma10avgprice")),
        ma10_volume=_integer(record.get("ma10volume")),
        ma20=_decimal(record.get("ma20avgprice")),
        ma20_volume=_integer(record.get("ma20volume")),
    )


def parse_daily_kline(payload: dict[str, Any]) -> tuple[PriceBar, ...]:
    """将百度动态 keys/marketData 结构转换为领域模型。"""
    result = payload.get("Result")
    market_data = result.get("newMarketData") if isinstance(result, dict) else None
    if not isinstance(market_data, dict):
        raise ProviderError("百度财经响应缺少行情结构")

    keys = market_data.get("keys")
    raw_rows = market_data.get("marketData")
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ProviderError("百度财经行情字段非法")
    if raw_rows in (None, ""):
        return ()
    if not isinstance(raw_rows, str):
        raise ProviderError("百度财经行情记录非法")

    parsed: dict[date, PriceBar] = {}
    non_empty_rows = 0
    for raw_row in raw_rows.split(";"):
        if not raw_row.strip():
            continue
        non_empty_rows += 1
        values = raw_row.split(",")
        if len(values) < len(keys):
            continue
        try:
            item = _to_price_bar(dict(zip(keys, values, strict=False)))
        except (TypeError, ValueError):
            continue
        parsed[item.trade_date] = item

    if non_empty_rows and not parsed:
        raise ProviderError("百度财经行情数据无法解析")
    return tuple(parsed[trade_date] for trade_date in sorted(parsed))
