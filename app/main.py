"""健康接口与服务内采集调度。"""

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Any, Callable

from fastapi import FastAPI

from app.core import msg
from app.core.config import Settings, get_settings
from app.calendar_scheduler import run_calendar_scheduler
from app.etf_api import create_etf_router
from app.etf_scheduler import run_etf_scheduler
from app.jobs_api import create_jobs_router
from app.scheduler import run_scheduler
from app.stock_monitor_api import create_monitor_router
from app.stock_monitor_scheduler import run_monitor_scheduler


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
        task = asyncio.create_task(run_scheduler()) if scheduler_enabled else None
        calendar_task = (
            asyncio.create_task(run_calendar_scheduler(configured)) if scheduler_enabled else None
        )
        monitor_task = (
            asyncio.create_task(run_monitor_scheduler())
            if scheduler_enabled and configured.stock_monitor_xq_enabled else None
        )
        etf_task = asyncio.create_task(run_etf_scheduler()) if scheduler_enabled else None
        try:
            yield
        finally:
            for running in (task, calendar_task, monitor_task, etf_task):
                if running is not None:
                    running.cancel()
                    with suppress(asyncio.CancelledError):
                        await running

    application = FastAPI(title="be-data-analysis", version="1.0.0", lifespan=lifespan)
    application.include_router(create_monitor_router(
        configured, exchange_factory=exchange_factory,
        xq_factory=xq_factory, redis_factory=redis_factory,
    ))
    application.include_router(create_etf_router(configured, provider_factory=etf_factory))
    application.include_router(create_jobs_router(
        configured, redis_factory=redis_factory, runner=job_runner,
    ))

    @application.get("/health", tags=["系统接口"], summary="服务健康检查")
    def health():
        return msg.ok({"status": "ok"}, message="服务正常")

    return application


app = create_app()
