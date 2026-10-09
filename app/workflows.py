"""CLI、自动调度和内部手动任务复用的业务入口。"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.collector import MarketCollector
from app.core.config import Settings
from app.source_execution import SourceExecutor
from app.providers.akshare_market import AkShareMarketProvider
from app.providers.akshare_etf import AkShareEtfProvider
from app.etf_monitor import EtfCollector, EtfStore
from app.providers.xueqiu import XueqiuProvider
from app.snapshot import RedisSnapshotStore
from app.stock_monitor import MonitorStore, StockMonitorSampler
from app.trading_calendar import AkShareCalendarSource, CalendarService

SHANGHAI = ZoneInfo("Asia/Shanghai")


def calendar_service(settings: Settings, client: Any) -> CalendarService:
    return CalendarService(client, AkShareCalendarSource(settings.source_timeout_seconds, executor=SourceExecutor(settings.redis_url.get_secret_value(), settings.source_timeout_seconds, client=client)))


def run_calendar(settings: Settings, client: Any, *, manual: bool = False,
                 at: datetime | None = None) -> str:
    return calendar_service(settings, client).refresh(
        at or datetime.now(SHANGHAI), manual=manual,
    )


def run_market(settings: Settings, client: Any, *, at: datetime | None = None) -> str:
    return MarketCollector(
        AkShareMarketProvider(settings.source_timeout_seconds, executor=SourceExecutor(settings.redis_url.get_secret_value(), settings.source_timeout_seconds, client=client)),
        RedisSnapshotStore(client, settings.redis_lock_seconds),
        calendar_service(settings, client),
    ).collect(at or datetime.now(SHANGHAI))


def run_monitor(settings: Settings, client: Any, *, at: datetime | None = None) -> str:
    if not settings.stock_monitor_xq_enabled:
        return "disabled"
    token = settings.xueqiu_token.get_secret_value()
    if not token:
        return "missing_token"
    return StockMonitorSampler(
        MonitorStore(client),
        XueqiuProvider(token, settings.source_timeout_seconds, sample_quotes=True, executor=SourceExecutor(settings.redis_url.get_secret_value(), settings.source_timeout_seconds, client=client)),
        calendar_service(settings, client),
        xq_enabled=True,
    ).sample(at or datetime.now(SHANGHAI))


def run_etf(settings: Settings, client: Any, *, at: datetime | None = None) -> str:
    return EtfCollector(
        AkShareEtfProvider(settings.source_timeout_seconds, market_quotes=True, executor=SourceExecutor(settings.redis_url.get_secret_value(), settings.source_timeout_seconds, client=client)),
        EtfStore(client), calendar_service(settings, client),
    ).collect(at or datetime.now(SHANGHAI))
