"""健康接口与服务内采集调度。"""

import asyncio
import logging
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from contextlib import asynccontextmanager, suppress
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler

from app.core import msg
from app.core.config import Settings, get_settings
from app.core.logging import log_failure
from app.etf_api import create_etf_router
from app.jobs_api import create_jobs_router
from app.scheduler import run_scheduler
from app.calendar_scheduler import run_calendar_scheduler
from app.stock_monitor_api import create_monitor_router

LOGGER = logging.getLogger(__name__)

def create_app(
    *, scheduler_enabled: bool = True, settings: Settings | None = None,
    exchange_factory: Callable[[], Any] | None = None,
    xq_factory: Callable[[], Any] | None = None,
    etf_factory: Callable[[], Any] | None = None,
    redis_factory: Callable[[], Any] | None = None,
    job_runner: Callable[[str], str] | None = None,
) -> FastAPI:
    configured = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        task = asyncio.create_task(run_scheduler(configured)) if scheduler_enabled else None
        calendar_task = asyncio.create_task(run_calendar_scheduler(configured)) if scheduler_enabled else None
        try:
            yield
        finally:
            for running in (task,calendar_task):
                if running is not None:
                    running.cancel()
                    with suppress(asyncio.CancelledError):
                        await running

    application = FastAPI(title="be-data-analysis", version="1.0.0", lifespan=lifespan)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path == "/internal/etf-monitor/v1/profiles":
            reasons = []
            for error in exc.errors():
                location = ".".join(
                    str(part) if isinstance(part, int) or part in {"body", "symbols", "asOfDate"}
                    else "<其他字段>"
                    for part in error["loc"]
                )
                reasons.append(f"{location}: {error['type']}")
            LOGGER.warning("ETF 资料请求格式校验失败，%s", "; ".join(reasons))
        return await request_validation_exception_handler(request, exc)

    application.include_router(create_monitor_router(
        configured, exchange_factory=exchange_factory,
        xq_factory=xq_factory, redis_factory=redis_factory,
    ))
    application.include_router(create_etf_router(
        configured, provider_factory=etf_factory, redis_factory=redis_factory,
    ))
    application.include_router(create_jobs_router(
        configured, redis_factory=redis_factory, runner=job_runner,
    ))

    @application.get("/health", tags=["系统接口"], summary="服务健康检查")
    def health():
        client = None
        try:
            client = redis_factory() if redis_factory else redis.Redis.from_url(
                configured.redis_url.get_secret_value(), decode_responses=True,
                socket_timeout=1, socket_connect_timeout=1, retry_on_timeout=False,
                retry=Retry(NoBackoff(), 0),
            )
            if not client.ping():
                return msg.fail(503, "Redis 不可用", {"status": "unavailable"})
            return msg.ok({"status": "ok"}, message="服务正常")
        except Exception as exc:
            log_failure(LOGGER, "health", exc)
            return msg.fail(503, "Redis 不可用", {"status": "unavailable"})
        finally:
            if client is not None:
                client.close()

    return application


app = create_app()
