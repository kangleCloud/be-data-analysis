"""供 Spring 服务使用的个股监控内部接口。"""

import hmac
import logging
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import redis
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.core.config import Settings
from app.providers.exchange_stocks import ExchangeStockProvider
from app.providers.xueqiu import XueqiuProvider, XueqiuSourceError
from app.stock_monitor import MonitorStore, normalize_profile, valid_symbol


LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ProfileRequest(BaseModel):
    symbols: list[str]


def create_monitor_router(
    settings: Settings,
    *,
    exchange_factory: Callable[[], Any] | None = None,
    xq_factory: Callable[[], Any] | None = None,
    redis_factory: Callable[[], Any] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/internal/stock-monitor/v1", tags=["个股监控内部接口"])

    def authorize(token: str | None) -> None:
        expected = settings.stock_monitor_internal_token.get_secret_value()
        if not expected:
            raise HTTPException(status_code=503, detail="内部接口令牌未配置")
        if token is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="内部接口认证失败")

    def new_store() -> MonitorStore:
        client = redis_factory() if redis_factory else redis.Redis.from_url(
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        return MonitorStore(client)

    @router.post("/exchange-dictionary", summary="同步交易所 A 股字典")
    def exchange_dictionary(
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        authorize(x_internal_token)
        source = exchange_factory() if exchange_factory else ExchangeStockProvider(
            settings.source_timeout_seconds
        )
        try:
            return {"schemaVersion": 1, "stocks": source.all_a_stocks()}
        except Exception as exc:
            LOGGER.warning("交易所股票字典同步失败，异常 %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="交易所股票字典同步失败") from exc

    @router.post("/profiles", summary="刷新已监控股票雪球资料")
    def profiles(
        request: ProfileRequest,
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        authorize(x_internal_token)
        if not settings.stock_monitor_xq_enabled:
            raise HTTPException(status_code=503, detail="雪球生产采集已关闭")
        token = settings.xueqiu_token.get_secret_value()
        if not token:
            raise HTTPException(status_code=503, detail="雪球令牌未配置")
        symbols = request.symbols
        if not symbols or len(symbols) > 10 or len(set(symbols)) != len(symbols) or not all(
            valid_symbol(symbol) for symbol in symbols
        ):
            raise HTTPException(status_code=422, detail="股票代码列表必须为 1 至 10 个不重复的 A 股代码")
        store = new_store()
        try:
            if store.cooldown_active():
                raise HTTPException(status_code=429, detail="雪球源冷却中")
            lock = store.acquire(lock_seconds=10 * 60)
            if lock is None:
                raise HTTPException(status_code=409, detail="雪球采集任务正在运行")
            try:
                source = xq_factory() if xq_factory else XueqiuProvider(
                    token, settings.source_timeout_seconds
                )
                result = []
                last_call: float | None = None

                def pace() -> None:
                    nonlocal last_call
                    if last_call is not None:
                        remaining = 1 - (time.monotonic() - last_call)
                        if remaining > 0:
                            time.sleep(remaining)
                    last_call = time.monotonic()

                for symbol in symbols:
                    pace()
                    raw_profile = source.profile(symbol)
                    updated_at = datetime.now(SHANGHAI)
                    profile = normalize_profile(symbol, raw_profile, None, updated_at)
                    if profile["marketCap"] is None:
                        pace()
                        profile = normalize_profile(
                            symbol, raw_profile, source.quote(symbol), updated_at
                        )
                    result.append(profile)
                return {"schemaVersion": 1, "profiles": result}
            finally:
                store.release(lock)
        except HTTPException:
            raise
        except XueqiuSourceError as exc:
            if exc.cooldown:
                store.start_cooldown()
            LOGGER.warning("雪球个股资料同步失败，异常 %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="雪球个股资料同步失败") from exc
        except Exception as exc:
            LOGGER.warning("雪球个股资料同步失败，异常 %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="雪球个股资料同步失败") from exc
        finally:
            store.client.close()

    return router
