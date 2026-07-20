"""FastAPI 应用入口与装配模块。"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from app.api.routes import router
from app.core import msg
from app.core.config import Settings, get_settings
from app.core.exceptions import AppError
from app.core.logging_config import configure_logging
from app.providers.factory import create_provider
from app.service.market_data import MarketDataService

LOGGER = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建并配置完整的 FastAPI 应用。"""
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.service_log_level)
    provider = create_provider(resolved_settings.data_provider)

    application = FastAPI(
        title="be-data-analysis",
        description="面向中国 A 股与场内 ETF 的行情数据获取和标准化服务。",
        version="0.1.0",
    )
    application.state.market_data_service = MarketDataService(provider)
    application.include_router(router)

    @application.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError):
        """将业务异常转换为统一失败响应。"""
        return msg.fail(exc.status_code, exc.message, exc.data)

    @application.exception_handler(RequestValidationError)
    async def handle_request_validation(_: Request, exc: RequestValidationError):
        """将 FastAPI 参数校验错误统一映射为 HTTP 400。"""
        LOGGER.debug("request validation failed: %s", exc)
        return msg.fail(400, "请求参数非法")

    @application.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception):
        """隐藏未知异常细节并记录完整堆栈。"""
        LOGGER.exception("unexpected error", exc_info=exc)
        return msg.fail(500, "服务内部错误")

    return application


app = create_app()
