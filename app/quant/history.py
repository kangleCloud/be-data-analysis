"""研究行情输入校验及源数据标准化。"""

from datetime import datetime
from math import isfinite
import re
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def validate_request(request: dict[str, Any]) -> None:
    kind, symbol = request.get("assetType"), request.get("symbol")
    if kind not in {"STOCK", "ETF", "INDEX"} or not isinstance(symbol, str):
        raise ValueError("资产类型或证券代码无效")
    if kind == "STOCK" and (len(symbol) != 6 or not symbol.isdigit() or not symbol.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))):
        raise ValueError("股票不属于沪深主板")
    if kind == "ETF" and (len(symbol) != 6 or not symbol.isdigit() or not symbol.startswith(("5", "1"))):
        raise ValueError("ETF 代码无效")
    if kind == "INDEX" and (not re.fullmatch(r"(?:sh|sz|csi)\d{6}", symbol) or request.get("adjust") != "raw"):
        raise ValueError("指数代码或复权方式无效")
    if request.get("frequency") != "daily" or request.get("adjust") not in {"raw", "qfq", "hfq"}:
        raise ValueError("行情频率或复权方式无效")
    fmt = "%Y-%m-%d"
    try:
        start = datetime.strptime(request["start"], fmt)
        end = datetime.strptime(request["end"], fmt)
        if start.strftime(fmt) != request["start"] or end.strftime(fmt) != request["end"]:
            raise ValueError("日期格式无效")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("日期格式无效") from exc
    if start > end or end > datetime.now(SHANGHAI).replace(tzinfo=None):
        raise ValueError("日期范围无效")
    now = datetime.now(SHANGHAI)
    if end.date() == now.date() and now.hour < 15:
        raise ValueError("仅支持当日收盘后的完整日线")


def normalize(request: dict[str, Any], function: str, records: Any) -> dict[str, Any]:
    if not isinstance(records, list) or not records:
        raise ValueError("数据源返回空行情")
    rows, seen = [], set()
    for row in records:
        try:
            stamp = str(row["date" if request["assetType"] == "INDEX" else "日期"])[:10]
            datetime.strptime(stamp, "%Y-%m-%d")
            if stamp < request["start"] or stamp > request["end"]:
                raise ValueError("行情超出请求范围")
            if stamp in seen:
                raise ValueError("行情时间重复")
            seen.add(stamp)
            columns = (("open", "开盘"), ("high", "最高"), ("low", "最低"), ("close", "收盘"), ("volume", "成交量"), ("amount", "成交额"))
            values = {key: float(row[key if request["assetType"] == "INDEX" else source]) for key, source in columns}
            if not all(isfinite(value) for value in values.values()) or any(values[k] <= 0 for k in ("open", "high", "low", "close")) or values["volume"] < 0 or values["amount"] < 0:
                raise ValueError("行情字段无效")
            if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"]):
                raise ValueError("行情高低价无效")
            rows.append({"time": stamp, **values})
        except (KeyError, TypeError) as exc:
            raise ValueError("数据源行情字段缺失") from exc
    rows.sort(key=lambda row: row["time"])
    reasons = ["历史证券资格、价格连续性及修订可见性尚未核验"]
    return {**{key: request[key] for key in ("assetType", "symbol", "frequency", "adjust")},
            "source": "akshare:eastmoney", "sourceFunction": function,
            "requestedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "rows": rows, "quality": {"status": "LIMITED", "reasons": reasons}}


def normalize_universe(asset_type: str, function: str, records: Any) -> dict[str, Any]:
    if not isinstance(records, list) or not records:
        raise ValueError("数据源返回空证券池")
    rows = []
    for row in records:
        symbol, name = str(row.get("代码", "")), str(row.get("名称", ""))
        if len(symbol) != 6 or not symbol.isdigit() or not name:
            raise ValueError("证券池字段无效")
        if asset_type == "STOCK" and not symbol.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
            continue
        if asset_type == "ETF" and not symbol.startswith(("5", "1")):
            continue
        rows.append({"symbol": symbol, "name": name, "assetType": asset_type,
                     "group": None, "benchmark": None})
    if not rows or len({r["symbol"] for r in rows}) != len(rows):
        raise ValueError("证券池为空或代码重复")
    return {"assetType": asset_type, "source": "akshare:eastmoney", "sourceFunction": function,
            "requestedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"), "rows": rows,
            "quality": {"status": "LIMITED", "reasons": ["仅当前列表，不可作为历史证券池", "上市退市、历史 ST 和停复牌未核验", "ETF 分类和基准未核验"]}}
