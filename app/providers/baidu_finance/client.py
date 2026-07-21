"""百度财经 HTTP 客户端。"""

from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from app.models.domain import AssetType
from app.providers.base import ProviderError
from app.providers.http import ExternalHttpError, create_http_client, request_json

BAIDU_FINANCE_HOST = "finance.pae.baidu.com"


class BaiduFinanceClient:
    """封装百度财经公开日 K 请求和安全 Cookie 注入。"""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        retry_count: int,
        ab_sr: SecretStr | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._retry_count = retry_count
        self._client = client or create_http_client(timeout_seconds)
        self._client.headers.update(
            {
                "Accept": "application/vnd.finance-web.v1+json",
                "Origin": "https://finance.baidu.com",
                "Referer": "https://finance.baidu.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        )

        # 自定义测试地址不得收到真实百度 Cookie。
        if ab_sr and urlparse(self._base_url).hostname == BAIDU_FINANCE_HOST:
            self._client.cookies.set(
                "ab_sr",
                ab_sr.get_secret_value(),
                domain=BAIDU_FINANCE_HOST,
                path="/",
            )

    def fetch_daily_kline(
        self,
        asset_type: AssetType,
        symbol: str,
    ) -> dict[str, Any]:
        """获取股票或场内 ETF 的完整公开日 K 响应。"""
        params = {
            "all": "1",
            "isIndex": "false",
            "isBk": "false",
            "isBlock": "false",
            "isFutures": "false",
            "isStock": "true",
            "newFormat": "1",
            "group": "quotation_kline_ab",
            "finClientType": "pc",
            "market_type": "ab",
            "code": symbol,
            "start_time": "",
            "ktype": "1",
        }
        del asset_type  # 股票与场内 ETF 在该接口中共用 A/B 股日 K 参数。

        try:
            payload = request_json(
                self._client,
                f"{self._base_url}/selfselect/getstockquotation",
                params=params,
                retry_count=self._retry_count,
            )
        except ExternalHttpError as exc:
            raise ProviderError("百度财经请求失败") from exc

        if not isinstance(payload, dict) or str(payload.get("ResultCode")) != "0":
            raise ProviderError("百度财经返回失败状态")
        return payload
