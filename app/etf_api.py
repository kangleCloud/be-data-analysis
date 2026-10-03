"""供 Java 同步 ETF 字典、资料和资产配置的受保护接口。"""

import hmac
import logging
import re
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.core.config import Settings
from app.etf_normalize import asset_allocation, catalog, etf_symbol, profiles
from app.providers.akshare_etf import AkShareEtfProvider

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ProfilesRequest(BaseModel):
    symbols: list[str]
    asOfDate: str


class AllocationRequest(BaseModel):
    symbol: str
    reportPeriod: str


def create_etf_router(
    settings: Settings, *, provider_factory: Callable[[], Any] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/internal/etf-monitor/v1", tags=["ETF 内部同步"])

    def authorize(token: str | None) -> None:
        expected = settings.stock_monitor_internal_token.get_secret_value()
        if not expected:
            raise HTTPException(status_code=503, detail="内部接口令牌未配置")
        if token is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="内部接口认证失败")

    def provider() -> Any:
        return provider_factory() if provider_factory else AkShareEtfProvider(
            settings.source_timeout_seconds
        )

    def valid_day(value: str) -> bool:
        if not re.fullmatch(r"\d{8}", value):
            return False
        try:
            datetime.strptime(value, "%Y%m%d")
        except ValueError:
            return False
        return True

    @router.post("/dictionary", summary="同步非东财 ETF 字典")
    def dictionary(
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        authorize(x_internal_token)
        try:
            return {
                "schemaVersion": 1, "source": "SINA",
                "collectedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                "etfs": catalog(provider().quotes()),
            }
        except Exception as exc:
            LOGGER.warning("ETF 字典同步失败，异常 %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="ETF 字典同步失败") from exc

    @router.post("/profiles", summary="同步交易所 ETF 资料")
    def profile_list(
        request: ProfilesRequest,
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        authorize(x_internal_token)
        symbols = [etf_symbol(value) for value in request.symbols]
        if (not symbols or len(symbols) > 10 or None in symbols
                or len(set(symbols)) != len(symbols)
                or not valid_day(request.asOfDate)):
            raise HTTPException(status_code=422, detail="ETF 代码或统计日期不正确")
        source = provider()
        states = {}
        rows: dict[str, list[dict[str, Any]]] = {}
        for exchange, method in (("sse", lambda: source.sse_scale(request.asOfDate)),
                                 ("szse", source.szse_scale)):
            try:
                rows[exchange] = method()
                states[exchange] = "OK"
            except Exception as exc:
                LOGGER.warning("ETF %s 资料源失败，异常 %s", exchange, type(exc).__name__)
                rows[exchange] = []
                states[exchange] = "ERROR"
        if all(state == "ERROR" for state in states.values()):
            raise HTTPException(status_code=502, detail="ETF 资料源均不可用")
        return {
            "schemaVersion": 1, "sourceStatus": states,
            "collectedAt": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "profiles": profiles(rows["sse"], rows["szse"], symbols),
        }

    @router.post("/asset-allocation", summary="同步雪球基金资产配置")
    def allocation(
        request: AllocationRequest,
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        authorize(x_internal_token)
        if not settings.stock_monitor_xq_enabled or not settings.xueqiu_token.get_secret_value():
            raise HTTPException(status_code=503, detail="雪球生产采集未启用")
        symbol = etf_symbol(request.symbol)
        if symbol is None or not valid_day(request.reportPeriod):
            raise HTTPException(status_code=422, detail="ETF 代码或请求报告期不正确")
        try:
            now = datetime.now(SHANGHAI).isoformat(timespec="seconds")
            return asset_allocation(
                provider().asset_allocation(symbol[2:], request.reportPeriod),
                symbol, request.reportPeriod, now,
            )
        except Exception as exc:
            LOGGER.warning("ETF 资产配置源失败，异常 %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="ETF 资产配置源失败") from exc

    return router
