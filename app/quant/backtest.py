"""使用显式历史快照的日频研究回测，不连接行情源或数据库。"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PERIODS = {"short": (1, 5, 0.05), "medium": (6, 60, 0.08), "long": (61, 250, 0.12)}


class _Invalid(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _Invalid(message)


def _number(value: Any, field: str, minimum: float | None = None) -> float:
    _require(not isinstance(value, bool) and isinstance(value, (int, float)), f"{field}必须为数字")
    number = float(value)
    _require(math.isfinite(number) and (minimum is None or number >= minimum), f"{field}无效")
    return number


def _timestamp(value: Any, name: str) -> datetime:
    _require(isinstance(value, str), f"缺少{name}")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _Invalid(f"{name}时间格式无效") from exc
    _require(result.tzinfo is not None, f"{name}必须包含时区")
    return result.astimezone(_SHANGHAI)


def _validate(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, float], tuple[int, int, float]]:
    _require(payload.get("frequency") == "daily" and payload.get("signalTime") == "close", "仅支持日线收盘信号回测")
    strategy = payload.get("strategy")
    _require(strategy in _PERIODS, "策略必须为short、medium或long")
    _require(payload.get("assetType") in ("STOCK", "ETF") and isinstance(payload.get("group"), str) and bool(payload["group"]), "缺少单一资产类型和可比组")
    _require(isinstance(payload.get("dataVersion"), str) and bool(payload["dataVersion"]), "缺少数据版本")
    _require(isinstance(payload.get("factorVersion"), str) and bool(payload["factorVersion"]), "缺少因子版本")
    coverage = payload.get("coverage")
    _require(isinstance(coverage, dict), "缺少历史数据覆盖说明")
    for key in ("calendar", "universe", "statuses", "limits", "openLiquidity", "corporateActions", "benchmark", "factors"):
        _require(coverage.get(key) is True, f"历史{key}覆盖未核验")
    _require(isinstance(coverage.get("evidenceVersion"), str) and bool(coverage["evidenceVersion"]), "缺少覆盖证据版本")
    costs = payload.get("costs")
    _require(isinstance(costs, dict), "必须明确交易成本")
    parsed_costs = {key: _number(costs.get(key), key, 0) for key in ("buyCommission", "sellCommission", "minCommission", "sellTax", "slippage")}
    _require(all(parsed_costs[key] < 1 for key in ("buyCommission", "sellCommission", "sellTax", "slippage")), "费率必须小于1")
    days = payload.get("days")
    _require(isinstance(days, list) and len(days) >= 2, "至少需要两个完整交易日")
    previous = None
    all_symbols = set()
    for day in days:
        _require(isinstance(day, dict), "交易日记录格式错误")
        try:
            day_date = date.fromisoformat(day["date"])
        except (ValueError, TypeError, KeyError) as exc:
            raise _Invalid("交易日格式无效") from exc
        _require(previous is None or day_date > previous, "交易日必须严格递增")
        previous = day_date
        _number(day.get("benchmarkClose"), "基准收盘价", 0.00000001)
        cutoff = datetime.combine(day_date, time(23, 59, 59), _SHANGHAI)
        end = datetime.combine(day_date, time(15), _SHANGHAI)
        benchmark_at = _timestamp(day.get("benchmarkAvailableAt"), "基准可见时间")
        _require(end <= benchmark_at <= cutoff, "基准可见时间不在当日收盘后")
        items = day.get("items")
        _require(isinstance(items, list), "缺少当日证券池")
        seen = set()
        for item in items:
            _require(isinstance(item, dict), "证券记录格式错误")
            symbol = item.get("symbol")
            _require(isinstance(symbol, str) and len(symbol) == 6 and symbol.isdigit() and symbol not in seen, "证券代码无效或重复")
            seen.add(symbol)
            if payload["assetType"] == "STOCK":
                _require(symbol.startswith(("600", "601", "603", "605", "000", "001", "002", "003")), "证券不属于沪深主板")
            else:
                _require(symbol.startswith(("1", "5")), "ETF代码无效")
            _require(item.get("assetType") == payload["assetType"] and item.get("group") == payload["group"], "跨资产或跨可比组排名")
            for field in ("listed", "st", "suspended"):
                _require(isinstance(item.get(field), bool), f"{symbol}缺少{field}状态")
            _require(_timestamp(item.get("statusAvailableAt"), "证券状态可见时间") <= datetime.combine(day_date, time(9, 30), _SHANGHAI), "证券状态在开盘时尚不可见")
            actions = item.get("corporateActions")
            _require(isinstance(actions, list), "必须提供权益事件列表")
            # 权益事件需要登记日、支付日及税务等精确信息，当前版本不作近似入账。
            _require(not actions, "区间存在权益事件，当前回测不支持权益入账")
            if not item["listed"]:
                continue
            close = _number(item.get("close"), "收盘价", 0.00000001)
            _require(_timestamp(item.get("closeAvailableAt"), "收盘可见时间") >= end, "收盘行情可见时间过早")
            _require(_timestamp(item["closeAvailableAt"], "收盘可见时间") <= cutoff, "收盘行情不在当日可见")
            if not item["suspended"]:
                opened = _number(item.get("open"), "开盘价", 0.00000001)
                high = _number(item.get("high"), "最高价", 0.00000001)
                low = _number(item.get("low"), "最低价", 0.00000001)
                _require(low <= min(opened, close) <= max(opened, close) <= high, "OHLC关系无效")
                _number(item.get("volume"), "全天成交量(股)", 0)
                _number(item.get("openBuyVolume"), "开盘可买数量(股)", 0)
                _number(item.get("openSellVolume"), "开盘可卖数量(股)", 0)
                quote_time = _timestamp(item.get("openAvailableAt"), "开盘盘口可见时间")
                _require(quote_time == datetime.combine(day_date, time(9, 30), _SHANGHAI), "开盘盘口必须在09:30可见")
                lower = _number(item.get("lowerLimit"), "跌停价", 0.00000001)
                upper = _number(item.get("upperLimit"), "涨停价", 0.00000001)
                _require(lower < upper and lower <= opened <= upper, "涨跌停或开盘价无效")
            signal = item.get("signal")
            _require(isinstance(signal, dict) and isinstance(signal.get("eligible"), bool), "缺少完整因子信号")
            _require(_timestamp(signal.get("availableAt"), "信号可见时间") >= end and _timestamp(signal["availableAt"], "信号可见时间") <= cutoff, "信号未在当日收盘后可见")
            _require(isinstance(signal.get("trendBelow"), bool), "缺少均线退出信号")
            if signal["eligible"]:
                _number(signal.get("score"), "因子分数", 0)
                _require(signal["score"] <= 1, "因子分数超出范围")
                _require(item["st"] is False, "风险警示证券不能入选")
        all_symbols.update(seen)
    # 每日必须显式描述全区间证券，含上市前和退市后的状态，不得以后来的名单代替历史可见状态。
    for day in days:
        _require({item["symbol"] for item in day["items"]} == all_symbols, "历史证券状态覆盖不完整")
    return days, parsed_costs, _PERIODS[strategy]


def _fee(amount: float, rate: float, minimum: float) -> float:
    return max(amount * rate, minimum)


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    days, costs, (min_days, max_days, stop) = _validate(payload)
    cash = 10000.0
    holdings: dict[str, dict[str, Any]] = {}
    pending_buy: list[str] = []
    pending_sell: dict[str, str] = {}
    orders: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []
    peak = cash
    paused = False
    for index, day in enumerate(days):
        rows = {item["symbol"]: item for item in day["items"]}
        day_name = day["date"]
        just_sold: set[str] = set()
        for symbol, position in list(holdings.items()):
            if index - position["buyIndex"] >= max_days:
                pending_sell.setdefault(symbol, "达到最长持有期")
        for symbol, reason in list(pending_sell.items()):
            position, row = holdings[symbol], rows[symbol]
            if index <= position["buyIndex"] or row["suspended"] or not row["listed"] or row["openSellVolume"] < position["shares"] or row["open"] <= row["lowerLimit"]:
                orders.append({"date": day_name, "symbol": symbol, "side": "SELL", "status": "UNFILLED", "reason": reason})
                continue
            price = row["open"] * (1 - costs["slippage"])
            if price < row["lowerLimit"]:
                orders.append({"date": day_name, "symbol": symbol, "side": "SELL", "status": "UNFILLED", "reason": "滑点后价格超出有效行情"})
                continue
            amount = price * position["shares"]
            fee = _fee(amount, costs["sellCommission"], costs["minCommission"]) + amount * costs["sellTax"]
            cash += amount - fee
            pnl = amount - fee - position["cost"]
            trades.append({"symbol": symbol, "buyDate": position["buyDate"], "sellDate": day_name, "shares": position["shares"], "buyPrice": position["price"], "sellPrice": price, "pnl": pnl, "returnRate": pnl / position["cost"], "reason": reason})
            orders.append({"date": day_name, "symbol": symbol, "side": "SELL", "status": "FILLED", "shares": position["shares"], "price": price, "fee": fee, "reason": reason})
            del holdings[symbol]
            del pending_sell[symbol]
            just_sold.add(symbol)
        for symbol in pending_buy:
            if paused or len(holdings) >= 2 or symbol in holdings or symbol in just_sold:
                continue
            row = rows[symbol]
            if not row["listed"] or row["st"] or row["suspended"] or row["openBuyVolume"] <= 0 or row["open"] >= row["upperLimit"]:
                orders.append({"date": day_name, "symbol": symbol, "side": "BUY", "status": "UNFILLED", "reason": "不可交易或涨停"})
                continue
            price = row["open"] * (1 + costs["slippage"])
            if price > row["upperLimit"]:
                orders.append({"date": day_name, "symbol": symbol, "side": "BUY", "status": "UNFILLED", "reason": "滑点后价格超出有效行情"})
                continue
            # 按上日已知净值限仓，避免使用当前收盘价造成前视偏差。
            budget = min(cash, (equity[-1]["value"] if equity else 10000.0) * .45)
            shares = int(budget / price // 100) * 100
            while shares > 0 and shares * price + _fee(shares * price, costs["buyCommission"], costs["minCommission"]) > budget:
                shares -= 100
            if shares <= 0 or shares > row["openBuyVolume"]:
                orders.append({"date": day_name, "symbol": symbol, "side": "BUY", "status": "UNFILLED", "reason": "资金或成交量不足"})
                continue
            amount = shares * price
            fee = _fee(amount, costs["buyCommission"], costs["minCommission"])
            cash -= amount + fee
            holdings[symbol] = {"shares": shares, "price": price, "cost": amount + fee, "buyIndex": index, "buyDate": day_name}
            orders.append({"date": day_name, "symbol": symbol, "side": "BUY", "status": "FILLED", "shares": shares, "price": price, "fee": fee})
        _require(all(rows[symbol]["listed"] for symbol in holdings), "持仓证券退市，缺少退市处置数据")
        value = cash + sum(position["shares"] * rows[symbol]["close"] for symbol, position in holdings.items())
        peak = max(peak, value)
        drawdown = value / peak - 1
        if drawdown <= -.10:
            paused = True
        equity.append({"date": day_name, "value": value, "cash": cash, "drawdown": drawdown, "benchmarkClose": day["benchmarkClose"], "paused": paused})
        for symbol, position in holdings.items():
            row = rows[symbol]
            age = index - position["buyIndex"]
            if row["close"] <= position["price"] * (1 - stop):
                pending_sell.setdefault(symbol, "收盘止损")
            elif age >= min_days and row["signal"]["trendBelow"]:
                pending_sell.setdefault(symbol, "跌破均线")
        eligible = [row for row in day["items"] if row["listed"] and not row["st"] and not row["suspended"] and row["signal"]["eligible"] and row["symbol"] not in holdings]
        eligible.sort(key=lambda item: (-item["signal"]["score"], item["symbol"]))
        pending_buy = [] if paused else [item["symbol"] for item in eligible[:max(0, 2 - len(holdings))]]
    end_value = equity[-1]["value"]
    daily_returns = [equity[i]["value"] / equity[i - 1]["value"] - 1 for i in range(1, len(equity))]
    annual_return = (end_value / 10000) ** (252 / len(daily_returns)) - 1 if daily_returns and end_value > 0 else None
    volatility = statistics.stdev(daily_returns) * math.sqrt(252) if len(daily_returns) >= 2 else None
    sharpe = (statistics.fmean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(252)
              if len(daily_returns) >= 2 and statistics.stdev(daily_returns) > 0 else None)
    turnover = sum(order["shares"] * order["price"] for order in orders if order["status"] == "FILLED") / statistics.fmean(point["value"] for point in equity)
    recovery_days = None
    if equity:
        valley_index = min(range(len(equity)), key=lambda i: equity[i]["drawdown"])
        if equity[valley_index]["drawdown"] < 0:
            peak_index, peak_value = -1, 10000.0
            for i in range(valley_index + 1):
                if equity[i]["value"] >= peak_value:
                    peak_index, peak_value = i, equity[i]["value"]
            for i in range(valley_index + 1, len(equity)):
                if equity[i]["value"] >= peak_value:
                    recovery_days = i - peak_index
                    break
    start_benchmark = days[0]["benchmarkClose"]
    benchmark_return = days[-1]["benchmarkClose"] / start_benchmark - 1
    worst_drawdown = min(point["drawdown"] for point in equity)
    winning_returns = [trade["returnRate"] for trade in trades if trade["returnRate"] > 0]
    losing_returns = [trade["returnRate"] for trade in trades if trade["returnRate"] < 0]
    profit_loss_ratio = (statistics.fmean(winning_returns) / abs(statistics.fmean(losing_returns))
                         if winning_returns and losing_returns else None)
    profit_loss_ratio_reason = None if profit_loss_ratio is not None else "缺少盈利或亏损的已平仓交易，无法计算盈亏比"
    return {"status": "LIMITED", "reasons": ["使用日线开盘价近似成交，不能验证开盘盘口和排队成交", "覆盖完整性与数据可见时间由输入证据提供方负责核验", "存在权益事件的区间尚不支持"],
            "assumptions": {"initialCapital": 10000, "maxPositions": 2, "maxWeight": .45, "frequency": "daily", "signalTime": "close", "strategy": payload["strategy"], "buyT1": True, "riskFreeRate": 0, "turnoverDefinition": "买卖成交额合计/平均净值", "drawdownPause": .10, "resume": "不自动恢复", "stopLoss": stop, "costs": costs, "dataVersion": payload["dataVersion"], "factorVersion": payload["factorVersion"], "evidenceVersion": payload["coverage"]["evidenceVersion"]},
            "orders": orders, "trades": trades, "equity": equity, "positions": [{"symbol": symbol, **position} for symbol, position in holdings.items()],
            "metrics": {"totalReturn": end_value / 10000 - 1, "annualizedReturn": annual_return, "annualizedVolatility": volatility, "sharpe": sharpe, "calmar": annual_return / abs(worst_drawdown) if annual_return is not None and worst_drawdown < 0 else None, "turnover": turnover, "recoveryDays": recovery_days, "benchmarkReturn": benchmark_return, "excessReturn": end_value / 10000 - 1 - benchmark_return, "maxDrawdown": worst_drawdown, "trades": len(trades), "fees": sum(order.get("fee", 0) for order in orders), "unfilledOrders": sum(order["status"] == "UNFILLED" for order in orders), "winRate": sum(trade["pnl"] > 0 for trade in trades) / len(trades) if trades else None, "profitLossRatio": profit_loss_ratio, "profitLossRatioReason": profit_loss_ratio_reason}}


def run_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    """执行收盘信号次日开盘的研究回测；数据不满足要求时返回诊断。"""
    if not isinstance(payload, dict):
        return {"status": "INVALID", "reasons": ["请求必须为对象"]}
    try:
        return _run(payload)
    except _Invalid as exc:
        return {"status": "INVALID", "reasons": [str(exc)]}
