"""雪球个股资料与报价的受控请求。"""

from typing import Any

import requests


class XueqiuSourceError(RuntimeError):
    def __init__(self, message: str, *, cooldown: bool = False) -> None:
        super().__init__(message)
        self.cooldown = cooldown


class XueqiuProvider:
    BASE_URL = "https://stock.xueqiu.com"

    def __init__(self, token: str, timeout_seconds: int = 15, session: Any = None) -> None:
        if not token:
            raise ValueError("雪球令牌未配置")
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout_seconds
        self._headers = {
            "Cookie": f"xq_a_token={token};",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
                "Mobile/15E148 Safari/604.1"
            ),
        }
        self._initialized = False

    def _request(self, path: str, *, symbol: str | None = None) -> dict[str, Any]:
        try:
            if not self._initialized:
                home = self._session.get(
                    "https://xueqiu.com", headers=self._headers, timeout=self._timeout
                )
                if home.status_code in (403, 429):
                    raise XueqiuSourceError("雪球登录页拒绝访问", cooldown=True)
                home.raise_for_status()
                self._initialized = True
            response = self._session.get(
                f"{self.BASE_URL}{path}",
                params={"symbol": symbol} if symbol else None,
                headers=self._headers,
                timeout=self._timeout,
            )
            if response.status_code in (401, 403, 429):
                raise XueqiuSourceError("雪球接口拒绝访问或触发限流", cooldown=True)
            response.raise_for_status()
            payload = response.json()
        except XueqiuSourceError:
            raise
        except requests.RequestException as exc:
            raise XueqiuSourceError("雪球请求失败") from exc
        except ValueError as exc:
            raise XueqiuSourceError("雪球响应不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise XueqiuSourceError("雪球响应结构不正确")
        if payload.get("error_code") not in (None, 0):
            raise XueqiuSourceError("雪球令牌失效或源接口异常", cooldown=True)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise XueqiuSourceError("雪球响应缺少数据", cooldown=True)
        return data

    def quote(self, symbol: str) -> dict[str, Any]:
        data = self._request("/v5/stock/quote.json", symbol=symbol)
        quote = data.get("quote")
        if not isinstance(quote, dict):
            raise XueqiuSourceError("雪球报价结构不正确")
        return quote

    def profile(self, symbol: str) -> dict[str, Any]:
        data = self._request("/v5/stock/f10/cn/company.json", symbol=symbol)
        company = data.get("company", data)
        if not isinstance(company, dict):
            raise XueqiuSourceError("雪球公司资料结构不正确")
        return company
