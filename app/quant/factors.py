"""无副作用的因子计算；输入必须由调用方确认行情连续和历史可见性。"""

from __future__ import annotations

import math
import statistics
import hashlib
from bisect import bisect_left, bisect_right
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
FACTOR_VERSION = "1.0.0"


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if len(value) == 10:
        parsed = datetime.combine(parsed.date(), time(15))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _series(rows: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    """仅使用截止时刻前可见记录；每日只能有一个已完成收盘值。"""
    result: dict[date, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get("time", row.get("date", "")))
        if len(value) != 10:
            raise ValueError("仅支持完整日线，不支持盘中行情")
        stamp = _time(value)
        if not isinstance(row.get("availableAt"), str) or len(row["availableAt"]) <= 10:
            raise ValueError("缺少行情可见时间")
        available = _time(str(row["availableAt"]))
        if stamp > cutoff or available > cutoff:
            continue
        if available < stamp:
            raise ValueError("行情可见时间早于行情时间")
        close = _number(row.get("close"))
        amount = _number(row.get("amount"))
        if close is None or close <= 0 or (amount is not None and amount < 0):
            raise ValueError("行情价格或成交额无效")
        previous = result.get(stamp.date())
        if previous is not None and previous["time"] == stamp:
            raise ValueError("行情时间重复")
        if previous is None or stamp > previous["time"]:
            result[stamp.date()] = {"time": stamp, "close": close, "amount": amount}
    return [result[day] for day in sorted(result)]


def _factor(value: float | None) -> dict[str, Any]:
    return {"value": value, "status": "AVAILABLE" if value is not None else "UNAVAILABLE"}


def _continuous(rows: list[dict[str, Any]], calendar: list[date]) -> list[dict[str, Any]]:
    allowed = set(calendar)
    if any(row["time"].date() >= calendar[0] and row["time"].date() not in allowed for row in rows):
        raise ValueError("行情日期不在交易日历中")
    by_date = {row["time"].date(): row for row in rows}
    tail = []
    for day in reversed(calendar):
        if day not in by_date:
            break
        tail.append(by_date[day])
    return list(reversed(tail))


def _compute(rows: list[dict[str, Any]], benchmark: list[dict[str, Any]], cutoff: datetime) -> dict[str, Any]:
    if not rows or rows[-1]["time"].date() != cutoff.date():
        return {}
    prices = [row["close"] for row in rows]
    by_day = {row["time"].date(): row["close"] for row in benchmark}
    factors: dict[str, Any] = {}
    for n in (5, 20, 60, 120):
        momentum = prices[-1] / prices[-n - 1] - 1 if len(prices) > n else None
        factors[f"return_{n}d"] = _factor(momentum)
        ma = statistics.fmean(prices[-n:]) if len(prices) >= n else None
        factors[f"ma_{n}d"] = _factor(ma)
        factors[f"trend_{n}d"] = _factor(prices[-1] / ma - 1 if ma else None)
        relative = None
        if momentum is not None:
            start, end = rows[-n - 1]["time"].date(), rows[-1]["time"].date()
            if start in by_day and end in by_day:
                relative = momentum - (by_day[end] / by_day[start] - 1)
        factors[f"relative_{n}d"] = _factor(relative)
    for n in (20, 60):
        daily_returns = [prices[i] / prices[i - 1] - 1 for i in range(max(1, len(prices) - n), len(prices))]
        factors[f"volatility_{n}d"] = _factor(statistics.stdev(daily_returns) if len(daily_returns) == n else None)
        factors[f"drawdown_{n}d"] = _factor(prices[-1] / max(prices[-n:]) - 1 if len(prices) >= n else None)
    amounts = [row["amount"] for row in rows[-20:]]
    factors["amount_20d"] = _factor(statistics.fmean(amounts) if len(amounts) == 20 and all(v is not None for v in amounts) else None)
    factors["zero_amount_days_20d"] = _factor(float(sum(v == 0 for v in amounts)) if len(amounts) == 20 and all(v is not None for v in amounts) else None)
    return factors


STRATEGIES = {
    "short": (("return_5d", .30, True), ("relative_20d", .25, True), ("amount_20d", .25, True), ("volatility_20d", .20, False)),
    "medium": (("relative_20d", .25, True), ("relative_60d", .25, True), ("trend_60d", .20, True), ("volatility_60d", .15, False), ("amount_20d", .15, True)),
    "long": (("relative_120d", .30, True), ("trend_120d", .25, True), ("volatility_60d", .25, False), ("amount_20d", .20, True)),
}


def definitions() -> list[dict[str, Any]]:
    """返回计算公式和版本，供结果归档。"""
    result = []
    def add(name: str, formula: str, window: int) -> None:
        result.append({"name": name, "formula": formula, "window": window,
                       "assetTypes": ["STOCK", "ETF"], "version": FACTOR_VERSION})
    for n in (5, 20, 60, 120):
        add(f"return_{n}d", "close[t]/close[t-n]-1", n)
        add(f"ma_{n}d", "mean(close[t-n+1:t])", n)
        add(f"trend_{n}d", "close[t]/ma_n[t]-1", n)
        add(f"relative_{n}d", "return_n[t]-benchmark_return_n[t]", n)
    for n in (20, 60):
        add(f"volatility_{n}d", "sample_std(daily_return[t-n+1:t])", n)
        add(f"drawdown_{n}d", "close[t]/max(close[t-n+1:t])-1", n)
    add("amount_20d", "mean(amount[t-19:t])", 20)
    add("zero_amount_days_20d", "count(amount[t-19:t]==0)", 20)
    return result


def _value(factors: dict[str, Any], name: str) -> float | None:
    return factors.get(name, {}).get("value")


def _eligible(name: str, factors: dict[str, Any]) -> bool:
    if any(_value(factors, item[0]) is None for item in STRATEGIES[name]):
        return False
    trend = _value(factors, f"trend_{ {'short': 20, 'medium': 60, 'long': 120}[name] }d")
    if trend is None or trend <= 0:
        return False
    if name == "short":
        return 0 < _value(factors, "return_5d") <= .15
    if name == "medium":
        ma20, ma60 = _value(factors, "ma_20d"), _value(factors, "ma_60d")
        return ma20 is not None and ma60 is not None and ma20 > ma60
    return _value(factors, "return_120d") is not None and _value(factors, "return_120d") > 0


def calculate(request: dict[str, Any], *, ranked: bool = True) -> dict[str, Any]:
    """截止时间和已验证连续价输入转为因子与同类百分位排名。"""
    if len(request["asOf"]) != 10:
        raise ValueError("仅支持收盘后日线选股")
    trading_close = _time(request["asOf"])
    signal = request.get("signalAt")
    if not isinstance(signal, str) or len(signal) <= 10:
        raise ValueError("缺少收盘后信号时间")
    cutoff = _time(signal)
    if cutoff.date() != trading_close.date() or cutoff <= trading_close:
        raise ValueError("信号时间必须是交易日收盘之后")
    if cutoff > datetime.now(SHANGHAI):
        raise ValueError("收盘数据尚不可用")
    if not isinstance(request.get("dataVersion"), str) or not request["dataVersion"].strip():
        raise ValueError("缺少数据版本")
    dates = request.get("tradeDates")
    if request.get("calendarVerified") is not True or not isinstance(dates, list) or not dates:
        raise ValueError("缺少已核验交易日历")
    if any(not isinstance(day, str) or len(day) != 10 for day in dates):
        raise ValueError("交易日历格式无效")
    calendar = [date.fromisoformat(day) for day in dates]
    if calendar != sorted(set(calendar)) or dates[-1] != request["asOf"]:
        raise ValueError("交易日历未按交易日递增或末日不匹配")
    calendar_hash = hashlib.sha256("\n".join(dates).encode()).hexdigest()
    benchmark_rows = request.get("benchmarks", [])
    if not isinstance(benchmark_rows, list) or not isinstance(request.get("items"), list) or not request["items"]:
        raise ValueError("证券池或基准格式无效")
    benchmarks = {entry["symbol"]: _continuous(_series(entry["bars"], cutoff), calendar) if entry.get("qualityVerified") is True else [] for entry in benchmark_rows}
    if len(benchmarks) != len(benchmark_rows):
        raise ValueError("基准重复")
    output = []
    seen = set()
    for item in request["items"]:
        key = (item["assetType"], item["symbol"])
        if key in seen:
            raise ValueError("证券重复")
        seen.add(key)
        kind, symbol = key
        if kind not in {"STOCK", "ETF"} or not isinstance(item.get("group"), str) or not item["group"].strip() or not isinstance(symbol, str):
            raise ValueError("资产类型、证券或可比组无效")
        if kind == "STOCK" and not (len(symbol) == 6 and symbol.isdigit() and symbol.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))):
            raise ValueError("股票不属于受支持的沪深主板")
        if kind == "ETF" and not (len(symbol) == 6 and symbol.isdigit() and symbol.startswith(("5", "1"))):
            raise ValueError("ETF 代码无效")
        verified = item.get("qualityVerified") is True and item.get("universeVerified") is True and item.get("securityEligible") is True
        bars = _continuous(_series(item.get("bars", []), cutoff), calendar)
        factors = _compute(bars, benchmarks.get(item.get("benchmark"), []), cutoff) if verified else {}
        output.append({"symbol": symbol, "assetType": kind, "group": item["group"], "factors": factors,
                       "scores": {}, "quality": "CALLER_ASSERTED" if verified else "UNVERIFIED"})
    if ranked:
        _score(output)
    return {"asOf": request["asOf"], "signalAt": signal, "dataVersion": request["dataVersion"], "factorVersion": FACTOR_VERSION, "calendarHash": calendar_hash,
            "computedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "quality": {"status": "LIMITED", "reasons": ["输入连续性、证券资格及历史可见性由调用方声明，未经本服务独立核验"]},
            "factorDefinitions": definitions(), "items": output}


def _score(output: list[dict[str, Any]]) -> None:
    pools: dict[tuple[str, str, str, str], list[float]] = {}
    eligible = {}
    for index, item in enumerate(output):
        for name, specs in STRATEGIES.items():
            eligible[index, name] = _eligible(name, item["factors"])
            if eligible[index, name]:
                for factor_name, _, _ in specs:
                    key = (item["assetType"], item["group"], name, factor_name)
                    pools.setdefault(key, []).append(_value(item["factors"], factor_name))
    for pool in pools.values():
        pool.sort()
    for index, item in enumerate(output):
        for name, specs in STRATEGIES.items():
            is_eligible = eligible[index, name]
            score = {"eligible": is_eligible, "score": None, "rank": None, "components": [], "reason": None if is_eligible else "条件不满足或必需因子不可用"}
            if is_eligible:
                for factor_name, weight, higher in specs:
                    value = _value(item["factors"], factor_name)
                    pool = pools[item["assetType"], item["group"], name, factor_name]
                    below = bisect_left(pool, value)
                    equal = bisect_right(pool, value) - below
                    percentile = (below + (equal + 1) / 2) / len(pool)
                    if not higher:
                        percentile = 1 + 1 / len(pool) - percentile
                    score["components"].append({"factor": factor_name, "value": value, "percentile": percentile, "weight": weight})
                score["score"] = sum(c["percentile"] * c["weight"] for c in score["components"])
            item["scores"][name] = score
    ranked_scores: dict[tuple[str, str, str], list[float]] = {}
    for item in output:
        for name, score in item["scores"].items():
            if score["score"] is not None:
                ranked_scores.setdefault((item["assetType"], item["group"], name), []).append(-score["score"])
    for values in ranked_scores.values():
        values.sort()
    for item in output:
        for name, score in item["scores"].items():
            if score["score"] is not None:
                score["rank"] = bisect_left(ranked_scores[(item["assetType"], item["group"], name)], -score["score"]) + 1


def calculate_raw(request: dict[str, Any]) -> dict[str, Any]:
    """分批计算因子，不进行分批排名。"""
    batch_id = request.get("batchId")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("缺少批次标识")
    result = calculate(request, ranked=False)
    result.pop("factorDefinitions")
    result["batchId"] = batch_id
    for item in result["items"]:
        item.pop("scores")
        item["factors"] = {name: value["value"] for name, value in item["factors"].items()}
    return result


def rank(request: dict[str, Any]) -> dict[str, Any]:
    """核验完整清单及批次血缘后对完整可比池进行排名。"""
    batches = request.get("batches")
    expected_batches = request.get("expectedBatchIds")
    universe = request.get("expectedUniverse")
    offset, limit = request.get("offset", 0), request.get("limit", 200)
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 500:
        raise ValueError("分页参数无效")
    if not isinstance(batches, list) or not batches or not isinstance(expected_batches, list) or not isinstance(universe, list) or not universe:
        raise ValueError("缺少批次或完整证券清单")
    if any(not isinstance(i, str) or not i for i in expected_batches) or len(set(expected_batches)) != len(expected_batches):
        raise ValueError("预期批次标识无效")
    batch_ids = [batch["batchId"] for batch in batches]
    if len(set(batch_ids)) != len(batch_ids) or set(batch_ids) != set(expected_batches):
        raise ValueError("批次与预期清单不一致")
    first = batches[0]
    metadata = (first["asOf"], first["signalAt"], first["dataVersion"], first["factorVersion"], first["calendarHash"])
    if first["factorVersion"] != FACTOR_VERSION or any(tuple(batch[key] for key in ("asOf", "signalAt", "dataVersion", "factorVersion", "calendarHash")) != metadata for batch in batches):
        raise ValueError("批次日期、时间或版本不一致")
    if not isinstance(metadata[4], str) or len(metadata[4]) != 64 or any(c not in "0123456789abcdef" for c in metadata[4]):
        raise ValueError("交易日历版本无效")
    close, signal = _time(metadata[0]), _time(metadata[1])
    if len(metadata[0]) != 10 or len(metadata[1]) <= 10 or signal.date() != close.date() or signal <= close or signal > datetime.now(SHANGHAI):
        raise ValueError("因子信号时间无效")
    if not isinstance(metadata[2], str) or not metadata[2].strip():
        raise ValueError("数据版本无效")
    expected = [(row["assetType"], row["symbol"], row["group"]) for row in universe]
    if len(set(expected)) != len(expected) or len({(row[0], row[1]) for row in expected}) != len(expected):
        raise ValueError("证券清单重复")
    allowed_names = {item["name"] for item in definitions()}
    items = []
    for batch in batches:
        for original in batch["items"]:
            kind, symbol, group = original["assetType"], original["symbol"], original["group"]
            if kind not in {"STOCK", "ETF"} or not isinstance(symbol, str) or not isinstance(group, str) or not group:
                raise ValueError("证券信息无效")
            if original.get("quality") not in {"CALLER_ASSERTED", "UNVERIFIED"} or not isinstance(original.get("factors"), dict):
                raise ValueError("因子质量状态无效")
            values = original["factors"]
            if set(values) - allowed_names or (original["quality"] == "UNVERIFIED" and values):
                raise ValueError("因子名称或质量状态无效")
            converted = {}
            for key, value in values.items():
                number = _number(value)
                if value is not None and number is None:
                    raise ValueError("因子数值无效")
                converted[key] = _factor(number)
            items.append({"symbol": symbol, "assetType": kind, "group": group, "quality": original["quality"], "factors": converted, "scores": {}})
    actual = [(row["assetType"], row["symbol"], row["group"]) for row in items]
    if len(set(actual)) != len(actual) or len({(row[0], row[1]) for row in actual}) != len(actual) or set(actual) != set(expected):
        raise ValueError("证券池与预期清单不一致")
    _score(items)
    return {"asOf": metadata[0], "signalAt": metadata[1], "dataVersion": metadata[2], "factorVersion": metadata[3], "calendarHash": metadata[4],
            "sourceBatchIds": batch_ids, "computedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "quality": {"status": "LIMITED", "reasons": ["输入连续性、证券资格及历史可见性由调用方声明，未经本服务独立核验"]},
            "factorDefinitions": definitions(), "total": len(items), "offset": offset, "limit": limit,
            "hasMore": offset + limit < len(items), "items": items[offset:offset + limit]}
