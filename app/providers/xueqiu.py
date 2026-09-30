"""AKShare 雪球 DataFrame 到个股监控领域字段的适配。"""

import os
import time
from typing import Any

import akshare
import pandas as pd
from akshare.exceptions import APIError, NetworkError, RateLimitError


class XueqiuSourceError(RuntimeError):
    def __init__(self, message: str, *, cooldown: bool = False) -> None:
        super().__init__(message)
        self.cooldown = cooldown


def _items(frame: pd.DataFrame) -> dict[str, Any]:
    if not isinstance(frame, pd.DataFrame) or not {"item", "value"}.issubset(frame.columns):
        raise XueqiuSourceError("雪球数据结构不正确")
    if frame.empty:
        raise XueqiuSourceError("雪球数据为空")
    return dict(zip(frame["item"], frame["value"]))


def _call(function: Any, *, symbol: str, token: str, timeout: int) -> dict[str, Any]:
    try:
        return _items(function(symbol=symbol, token=token, timeout=timeout))
    except XueqiuSourceError:
        raise
    except APIError as exc:
        message = str(exc).lower()
        raise XueqiuSourceError(
            "雪球接口拒绝访问或数据异常",
            cooldown=exc.status_code in (401, 403, 429)
            or "token" in message or "login" in message,
        ) from exc
    except RateLimitError as exc:
        raise XueqiuSourceError("雪球接口限流", cooldown=True) from exc
    except NetworkError as exc:
        raise XueqiuSourceError("雪球网络请求失败") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise XueqiuSourceError("雪球数据转换失败") from exc


class XueqiuProvider:
    def __init__(self, token: str, timeout_seconds: int = 15, *, api: Any = None) -> None:
        if not token:
            raise ValueError("雪球令牌未配置")
        # AKShare 1.18.97 用 datetime.fromtimestamp 生成无时区的“时间”字符串。
        os.environ["TZ"] = "Asia/Shanghai"
        time.tzset()
        self._token = token
        self._timeout = timeout_seconds
        self._api = api if api is not None else akshare

    def quote(self, symbol: str) -> dict[str, Any]:
        rows = _call(
            self._api.stock_individual_spot_xq,
            symbol=symbol, token=self._token, timeout=self._timeout,
        )
        return {
            field: rows.get(item)
            for field, item in {
                "current": "现价", "percent": "涨幅", "amount": "成交额",
                "time": "时间", "low": "最低", "high": "最高",
                "open": "今开", "limit_up": "涨停", "limit_down": "跌停",
                "avg_price": "均价", "volume": "成交量",
                "previous_close": "昨收",
                "market_capital": "资产净值/总市值",
            }.items()
        }

    def profile(self, symbol: str) -> dict[str, Any]:
        rows = _call(
            self._api.stock_individual_basic_info_xq,
            symbol=symbol, token=self._token, timeout=self._timeout,
        )
        affiliate = rows.get("affiliate_industry")
        industry = rows.get("affiliate_industry.ind_name")
        if industry is None and isinstance(affiliate, dict):
            industry = affiliate.get("ind_name")
        return {"industry": industry, "list_date": rows.get("listed_date")}
