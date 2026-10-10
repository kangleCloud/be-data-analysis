"""供 Java 同步 ETF 字典、资料和资产配置的受保护接口。"""

import hmac
import logging
import re
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import redis
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.runtime.source_execution import SourceExecutor, SourceBusyError, SourceCoolingError, SourceThrottledError
from app.runtime.resources import SourceResourceError
from app.runtime.gates import collection_entry
from app.etf_monitor.dictionary import load_dictionary, save_dictionary, dictionary_response
from app.etf_monitor.normalize import asset_allocation, etf_symbol, AllocationNoData
from app.etf_monitor.profiles import ProfileBatchError, collect_profiles
from app.providers.akshare_etf import AkShareEtfProvider

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ProfilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: list[str]


class AllocationRequest(BaseModel):
    symbol: str
    reportPeriod: str


def create_etf_router(
    settings: Settings, *, provider_factory: Callable[[], Any] | None = None,
    redis_factory: Callable[[], Any] | None = None,
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
            settings.source_timeout_seconds,
            executor=SourceExecutor(settings.redis_url.get_secret_value(), settings.source_timeout_seconds),
        )

    def redis_client():
        return redis_factory() if redis_factory else redis.Redis.from_url(settings.redis_url.get_secret_value(),
            decode_responses=True,socket_timeout=1,socket_connect_timeout=1)

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
        client = None
        try:
            client = redis_client()
            now = datetime.now(SHANGHAI)
            cached = load_dictionary(client,now)
            if cached:
                return dictionary_response(cached)
            with collection_entry(client) as entered:
                if not entered:
                    raise HTTPException(status_code=409,detail='采集入口忙碌')
                rows = provider().quotes()
                return dictionary_response(save_dictionary(client,rows,datetime.now(SHANGHAI)))
        except HTTPException:
            raise
        except SourceBusyError as exc:
            raise HTTPException(status_code=409,detail='采集入口忙碌') from exc
        except SourceResourceError as exc:
            raise HTTPException(status_code=503,detail={'reason':'RESOURCE','message':'采集资源不足'}) from exc
        except Exception as exc:
            LOGGER.warning("ETF 字典同步失败，异常 %s",type(exc).__name__)
            raise HTTPException(status_code=502,detail='ETF 字典同步失败') from exc
        finally:
            if client is not None:
                client.close()

    @router.post("/profiles", summary="同步同花顺 ETF 基本资料")
    def profile_list(
        request: ProfilesRequest,
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        authorize(x_internal_token)
        symbols = [etf_symbol(value) for value in request.symbols]
        if (not symbols or len(symbols) > 10 or None in symbols
                or len(set(symbols)) != len(symbols)):
            LOGGER.warning(
                "ETF 资料代码列表校验失败，数量 %d，无效代码 %d，重复代码 %s",
                len(symbols), symbols.count(None), len(set(symbols)) != len(symbols),
            )
            raise HTTPException(status_code=422, detail="ETF 代码列表必须为 1 至 10 个不重复代码")
        client = None
        try:
            client = redis_factory() if redis_factory else redis.Redis.from_url(
                settings.redis_url.get_secret_value(), decode_responses=True,
                socket_timeout=5, socket_connect_timeout=5,
            )
            with collection_entry(client) as entered:
                if not entered:
                    raise HTTPException(status_code=409,detail='采集入口忙碌')
                return collect_profiles(provider(),client,symbols)
        except HTTPException:
            raise
        except SourceResourceError as exc:
            raise HTTPException(status_code=503,detail={'reason':'RESOURCE','message':'采集资源不足'}) from exc
        except ProfileBatchError as exc:
            raise HTTPException(status_code=exc.status_code, detail={
                "message": str(exc), "sourceStatus": exc.states,
            }) from exc
        except Exception as exc:
            LOGGER.warning("ETF 资料批次基础设施失败，异常 %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="ETF 资料批次基础设施不可用") from exc
        finally:
            if client is not None:
                client.close()

    @router.post("/asset-allocation", summary="同步雪球基金资产配置")
    def allocation(
        request: AllocationRequest,
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> dict[str, Any]:
        authorize(x_internal_token)
        if not settings.stock_monitor_xq_enabled or not settings.xueqiu_token.get_secret_value():
            raise HTTPException(status_code=503, detail={"reason":"DISABLED","message":"雪球生产采集未启用"})
        symbol = etf_symbol(request.symbol)
        if symbol is None or not valid_day(request.reportPeriod):
            raise HTTPException(status_code=422, detail="ETF 代码或请求报告期不正确")
        client = None
        try:
            client = redis_client()
            with collection_entry(client) as entered:
                if not entered:
                    raise HTTPException(status_code=409,detail='采集入口忙碌')
                rows = provider().asset_allocation(symbol[2:],request.reportPeriod)
                now = datetime.now(SHANGHAI).isoformat(timespec='seconds')
                return asset_allocation(rows,symbol,request.reportPeriod,now)
        except HTTPException:
            raise
        except SourceBusyError as exc:
            raise HTTPException(status_code=409,detail='采集入口忙碌') from exc
        except (SourceCoolingError,SourceThrottledError) as exc:
            raise HTTPException(status_code=429,detail='源保护或请求间隔未满足') from exc
        except SourceResourceError as exc:
            raise HTTPException(status_code=503,detail={'reason':'RESOURCE','message':'采集资源不足'}) from exc
        except AllocationNoData as exc:
            raise HTTPException(status_code=502,detail={'reason':'NO_DATA','message':'该报告期无有效资产配置'}) from exc
        except Exception as exc:
            LOGGER.warning('ETF资产配置源失败，异常 %s',type(exc).__name__)
            raise HTTPException(status_code=502,detail={'reason':'SOURCE','message':'资产配置源失败'}) from exc
        finally:
            if client is not None:
                client.close()

    return router
