"""供内部服务读取研究行情并执行日频量化计算。"""

import hmac
import logging
from typing import Any, Callable

from fastapi import APIRouter, Header, HTTPException

from app.core.config import Settings
from app.providers.akshare_quant import AkShareQuantProvider
from app.quant import factors, history

LOGGER = logging.getLogger(__name__)


def create_quant_router(settings: Settings, *, provider_factory: Callable[[], Any] | None = None) -> APIRouter:
    router = APIRouter(prefix="/internal/quant/v1", tags=["量化研究内部接口"])

    def authorize(token: str | None) -> None:
        expected = settings.stock_monitor_internal_token.get_secret_value()
        if not expected:
            raise HTTPException(status_code=503, detail="内部接口令牌未配置")
        if token is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="内部接口认证失败")

    def provider() -> Any:
        return provider_factory() if provider_factory else AkShareQuantProvider(settings.source_timeout_seconds)

    @router.post("/history", summary="获取单标的历史日线")
    def fetch_history(request: dict[str, Any], token: str | None = Header(default=None, alias="X-Internal-Token")) -> dict[str, Any]:
        authorize(token)
        try:
            history.validate_request(request)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            function, records = provider().history(request["assetType"], request["symbol"], request["frequency"], request["adjust"], request["start"], request["end"])
            return history.normalize(request, function, records)
        except Exception as exc:
            LOGGER.warning("量化历史行情获取失败，异常 %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="行情源失败或数据无效") from exc

    @router.post("/universe", summary="获取当前候选证券列表")
    def fetch_universe(request: dict[str, Any], token: str | None = Header(default=None, alias="X-Internal-Token")) -> dict[str, Any]:
        authorize(token)
        kind = request.get("assetType")
        if kind not in {"STOCK", "ETF"}:
            raise HTTPException(status_code=422, detail="资产类型无效")
        try:
            function, records = provider().universe(kind)
            return history.normalize_universe(kind, function, records)
        except Exception as exc:
            LOGGER.warning("量化证券列表获取失败，异常 %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="证券列表源失败或数据无效") from exc

    @router.post("/factors", summary="计算收盘后日线因子和策略分数")
    def calculate(request: dict[str, Any], token: str | None = Header(default=None, alias="X-Internal-Token")) -> dict[str, Any]:
        authorize(token)
        try:
            return factors.calculate(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail="因子输入格式或数据无效") from exc

    @router.post("/factors/raw", summary="分批计算原始因子")
    def calculate_raw(request: dict[str, Any], token: str | None = Header(default=None, alias="X-Internal-Token")) -> dict[str, Any]:
        authorize(token)
        try:
            return factors.calculate_raw(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail="因子输入格式或数据无效") from exc

    @router.post("/factors/rank", summary="核验全量证券池并统一排名")
    def rank(request: dict[str, Any], token: str | None = Header(default=None, alias="X-Internal-Token")) -> dict[str, Any]:
        authorize(token)
        try:
            return factors.rank(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail="因子批次或证券清单无效") from exc

    @router.post("/backtest", summary="执行日频策略回测")
    def backtest(request: dict[str, Any], token: str | None = Header(default=None, alias="X-Internal-Token")) -> dict[str, Any]:
        authorize(token)
        from app.quant.backtest import run_backtest
        return run_backtest(request)

    return router
