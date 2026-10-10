"""CLI、自动调度和内部手动任务复用的业务入口。"""

from datetime import datetime
from functools import wraps
from app.runtime.gates import collection_entry, check_entry
from typing import Any
from zoneinfo import ZoneInfo

from app.market.collector import MarketCollector
from app.core.config import Settings
from app.runtime.source_execution import SourceExecutor
from app.providers.akshare_market import AkShareMarketProvider
from app.providers.akshare_etf import AkShareEtfProvider
from app.etf_monitor.collector import EtfCollector, EtfStore
from app.providers.xueqiu import XueqiuProvider
from app.market.snapshot import RedisSnapshotStore
from app.stock_monitor.service import MonitorStore, StockMonitorSampler
from app.calendar.service import AkShareCalendarSource, CalendarService

SHANGHAI = ZoneInfo("Asia/Shanghai")


def exclusive(function):
    @wraps(function)
    def run(settings,client,**kwargs):
        with collection_entry(client) as acquired:
            if not acquired:
                return 'locked'
            return function(settings,client,**kwargs)
    return run


def calendar_service(settings: Settings, client: Any) -> CalendarService:
    return CalendarService(client, AkShareCalendarSource(settings.source_timeout_seconds, executor=SourceExecutor(settings.redis_url.get_secret_value(), settings.source_timeout_seconds, client=client)))


@exclusive
def run_calendar(settings: Settings, client: Any, *, manual: bool = False,
                 at: datetime | None = None) -> str:
    return calendar_service(settings, client).refresh(
        at or datetime.now(SHANGHAI), manual=manual,
    )


def _market(settings,client,at,lane):
    return MarketCollector(
        AkShareMarketProvider(settings.source_timeout_seconds,executor=SourceExecutor(
            settings.redis_url.get_secret_value(),settings.source_timeout_seconds,client=client,lane=lane)),
        RedisSnapshotStore(client,settings.redis_lock_seconds,lane=lane),
        calendar_service(settings,client),lane=lane,
    ).collect(at or datetime.now(SHANGHAI))


def run_market(settings: Settings, client: Any, *, at: datetime | None = None) -> str:
    """同步完整市场刷新：一次原子获取两个入口，忙时不遗留半把锁。"""
    with collection_entry(client,lane='market') as acquired:
        return _market(settings,client,at,'market') if acquired else 'locked'


@exclusive
def run_monitor(settings: Settings, client: Any, *, at: datetime | None = None) -> str:
    if not settings.stock_monitor_xq_enabled:
        return "disabled"
    token = settings.xueqiu_token.get_secret_value()
    if not token:
        return "missing_token"
    return StockMonitorSampler(
        MonitorStore(client),
        XueqiuProvider(token, settings.source_timeout_seconds, executor=SourceExecutor(settings.redis_url.get_secret_value(), settings.source_timeout_seconds, client=client)),
        calendar_service(settings, client),
        xq_enabled=True,
    ).sample(at or datetime.now(SHANGHAI))


@exclusive
def run_etf(settings: Settings, client: Any, *, at: datetime | None = None) -> str:
    return EtfCollector(
        AkShareEtfProvider(settings.source_timeout_seconds, market_quotes=True, executor=SourceExecutor(settings.redis_url.get_secret_value(), settings.source_timeout_seconds, client=client)),
        EtfStore(client), calendar_service(settings, client),
    ).collect(at or datetime.now(SHANGHAI))


def run_quotes(settings: Settings,client: Any,*,cancel=None) -> dict[str,str]:
    """行情通道：股票报价→ETF→指数→行业→概念，完成模块立即发布。"""
    with collection_entry(client,cancel,lane='quotes') as acquired:
        if not acquired:
            return {'outcome':'locked'}
        outcomes = {}
        for key,function in [('monitor',run_monitor),('etf',run_etf)]:
            check_entry()
            outcomes[key] = function(settings,client,at=datetime.now(SHANGHAI))
            if outcomes[key] == 'resource':
                return outcomes
        check_entry()
        outcomes['market'] = _market(settings,client,datetime.now(SHANGHAI),'quotes')
        return outcomes


def run_funds(settings: Settings,client: Any,*,cancel=None) -> str:
    """资金通道仅调用即时全市场榜，同批生成汇总/宽度/启用股票资金点。"""
    with collection_entry(client,cancel,lane='funds') as acquired:
        return _market(settings,client,datetime.now(SHANGHAI),'funds') if acquired else 'locked'
