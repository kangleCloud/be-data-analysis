"""两条固定通道的隔离、原子准入及快照乐观合并回归。"""

import json
import multiprocessing as mp
import threading
from concurrent.futures import ThreadPoolExecutor

import fakeredis
import pytest
import redis

from app.runtime.gates import (ACQUIRE,RELEASE,RENEW,QUOTES_ENTRY_KEY,FUNDS_ENTRY_KEY,
                               collection_entry,check_entry)
from app.runtime.source_execution import SourceCall,SourceExecutor,SourceControlError
from app.runtime.resources import MEMORY_LIMIT_BYTES,SourceResourceError
from app.market.collector import MarketCollector
from app.market.snapshot import RedisSnapshotStore,SNAPSHOT_KEY,UPDATES_CHANNEL,FUND_SERIES_PREFIX
from app.stock_monitor.service import ENABLED_KEY,MONITOR_UPDATES_CHANNEL
from tests.test_collector import FakeProvider,FakeCalendar,TRADING_AT
from tests.test_snapshot import FakeRedis
from tests.test_source_execution import ControlRedis,ignore_term_worker


def test_complete_market_lua_acquires_both_or_none_and_preserves_other_owner():
    client=fakeredis.FakeRedis(decode_responses=True)
    client.set(FUNDS_ENTRY_KEY,'other',ex=30)
    with collection_entry(client,lane='market') as acquired:
        assert not acquired
    assert client.get(QUOTES_ENTRY_KEY) is None and client.get(FUNDS_ENTRY_KEY)=='other'
    client.delete(FUNDS_ENTRY_KEY)
    with collection_entry(client,lane='market') as acquired:
        assert acquired
        token=client.get(QUOTES_ENTRY_KEY)
        assert client.get(FUNDS_ENTRY_KEY)==token
        assert all(0 < client.ttl(key) <= 30 for key in (QUOTES_ENTRY_KEY,FUNDS_ENTRY_KEY))
        assert not client.eval(RENEW,2,QUOTES_ENTRY_KEY,FUNDS_ENTRY_KEY,'wrong',30)
        client.eval(RELEASE,2,QUOTES_ENTRY_KEY,FUNDS_ENTRY_KEY,'wrong')
        assert client.get(FUNDS_ENTRY_KEY)==token
    assert client.get(QUOTES_ENTRY_KEY) is None and client.get(FUNDS_ENTRY_KEY) is None


def test_two_channel_contexts_are_independent_and_same_lane_is_nonqueuing():
    client=fakeredis.FakeRedis(decode_responses=True)
    ready,release=threading.Event(),threading.Event()
    def hold():
        with collection_entry(client,lane='funds') as acquired:
            assert acquired
            ready.set()
            assert release.wait(2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(hold)
        assert ready.wait(1)
        with collection_entry(client,lane='funds') as acquired:
            assert not acquired
        with collection_entry(client,lane='quotes') as acquired:
            assert acquired
            check_entry()
        release.set()
        pending.result(timeout=2)


def test_entry_renewal_failure_stops_current_context(monkeypatch):
    import app.runtime.gates as gates
    client=fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(gates,'RENEW_SECONDS',.01)
    with collection_entry(client,lane='market'):
        client.set(FUNDS_ENTRY_KEY,'replacement',ex=30)
        with pytest.raises(SourceControlError):
            check_entry()
    assert client.get(FUNDS_ENTRY_KEY)=='replacement' and client.get(QUOTES_ENTRY_KEY) is None


def test_slow_funds_do_not_block_quotes_or_overwrite_newer_modules(flow_rows,market_rows):
    backend=FakeRedis()
    backend.set(ENABLED_KEY,json.dumps([{'symbol':'SH600000','code':'600000','name':'浦发银行','market':'SH'}]))
    ready,release=threading.Event(),threading.Event()
    funds=FakeProvider(flow_rows,market_rows)
    quotes=FakeProvider(flow_rows,market_rows)
    original=funds.market_fund_flow
    def slow():
        ready.set()
        assert release.wait(3)
        return original()
    funds.market_fund_flow=slow
    fund_store=RedisSnapshotStore(backend,240,lane='funds')
    quote_store=RedisSnapshotStore(backend,240,lane='quotes')
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(MarketCollector(funds,fund_store,FakeCalendar(funds),lane='funds').collect,TRADING_AT)
        assert ready.wait(1)
        try:
            assert MarketCollector(quotes,quote_store,FakeCalendar(quotes),lane='quotes').collect(TRADING_AT)=='published'
            assert not pending.done()
            assert set(quote_store.load()['modules'])=={'coreIndices','industrySectors','conceptSectors'}
        finally:
            release.set()
        assert pending.result(timeout=3)=='published'
    snapshot=quote_store.load()
    assert all(module['status']=='FRESH' for module in snapshot['modules'].values())
    assert quotes.calls==['calendar','index','flow:industry','flow:concept']
    assert funds.calls==['calendar','market']
    notices=[json.loads(value) for action,key,value in backend.events if action=='publish' and key==UPDATES_CHANNEL]
    assert len(notices)==4
    assert [notice['previousSnapshotId'] for notice in notices[1:]]==[notice['snapshotId'] for notice in notices[:-1]]
    assert notices[-1]['changedModules']==['marketFundFlow']
    assert len(json.loads(backend.get(FUND_SERIES_PREFIX+'2026-09-23:SH600000')))==1
    assert sum(action=='publish' and key==MONITOR_UPDATES_CHANNEL for action,key,_ in backend.events)==1


@pytest.mark.parametrize('conflicts',[1,3,4])
def test_watch_retries_latest_base_and_only_current_module(conflicts):
    backend=FakeRedis()
    store=RedisSnapshotStore(backend,240)
    store.save({'schemaVersion':1,'generatedAt':'2026-10-09T10:00:00+08:00','modules':{'coreIndices':{'status':'FRESH'}}})
    pipeline,attempts=backend.pipeline,[]
    def conflicting_pipe(*args,**kwargs):
        pipe=pipeline(*args,**kwargs)
        execute=pipe.execute
        def run():
            attempts.append(1)
            if len(attempts)<=conflicts:
                latest=json.loads(backend.get(SNAPSHOT_KEY))
                latest['snapshotId']='parallel-'+str(len(attempts))
                latest['modules']['conceptSectors']={'status':'FRESH','lastSuccessAt':str(len(attempts))}
                backend.set(SNAPSHOT_KEY,json.dumps(latest))
                raise redis.WatchError('冲突')
            return execute()
        pipe.execute=run
        return pipe
    backend.pipeline=conflicting_pipe
    patch={'schemaVersion':1,'generatedAt':'2026-10-09T10:02:00+08:00','modules':{'industrySectors':{'status':'FRESH'}}}
    if conflicts==4:
        with pytest.raises(RuntimeError,match='重试3次'):
            store.save(patch)
        assert 'industrySectors' not in store.load()['modules']
        assert len(attempts)==4
    else:
        store.save(patch)
        assert len(attempts)==conflicts+1
        assert set(store.load()['modules'])=={'coreIndices','conceptSectors','industrySectors'}
        last=json.loads([value for action,key,value in backend.events if action=='publish' and key==UPDATES_CHANNEL][-1])
        assert last['previousSnapshotId']=='parallel-'+str(conflicts)
        assert last['changedModules']==['industrySectors']


def test_actual_redis_watch_detects_lost_update_and_rebuilds(monkeypatch):
    client=fakeredis.FakeRedis(decode_responses=True)
    store=RedisSnapshotStore(client,240)
    original=store._save_transaction
    once=[]
    def race(pipe,snapshot,**kwargs):
        if not once:
            once.append(True)
            RedisSnapshotStore(client,240).save({'schemaVersion':1,'generatedAt':'2026-10-09T10:01:00+08:00','modules':{'coreIndices':{'status':'FRESH'}}})
        return original(pipe,snapshot,**kwargs)
    monkeypatch.setattr(store,'_save_transaction',race)
    store.save({'schemaVersion':1,'generatedAt':'2026-10-09T10:00:00+08:00','modules':{'conceptSectors':{'status':'FRESH'}}})
    assert set(store.load()['modules'])=={'coreIndices','conceptSectors'}
    assert store.load()['generatedAt']=='2026-10-09T10:01:00+08:00'


def test_800mib_pressure_reaps_two_independent_term_ignoring_sources(monkeypatch):
    backend=ControlRedis()
    high=threading.Event()
    monkeypatch.setattr('app.runtime.resources.memory_state',lambda:{'currentBytes':MEMORY_LIMIT_BYTES if high.is_set() else 0})
    context=mp.get_context('spawn')
    ready=[context.Event(),context.Event()]
    sources=[SourceExecutor('redis://offline',client=backend,worker=ignore_term_worker) for _ in range(2)]
    calls=[SourceCall('fake','ths',{'ready':ready[0]},900,cooldown_keys=('cool',)),
           SourceCall('stock_fund_flow_individual','ths',{'symbol':'即时','ready':ready[1]},900,cooldown_keys=('cool',))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=[pool.submit(source.call,call) for source,call in zip(sources,calls)]
        assert all(event.wait(5) for event in ready)
        assert backend.max_global==2 and backend.max_groups=={'ths':2}
        high.set()
        for result in pending:
            with pytest.raises(SourceResourceError):
                result.result(timeout=4)
    assert not any(child.is_alive() for child in mp.active_children())
    assert backend.metrics()[-1]==[] and backend.get('cool') is None


@pytest.mark.parametrize('cached',[False,True])
def test_funds_read_calendar_without_quotes_entry_or_monthly_retry(flow_rows,market_rows,cached):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.calendar.service import CalendarService,CACHE_KEY,AUTO_RETRY_KEY,normalize_dates
    backend=FakeRedis()
    calls=[]
    class NoCalendarSource:
        def dates(self):
            calls.append('calendar-http')
            raise AssertionError('资金通道不得请求日历')
    at=TRADING_AT
    if cached:
        # 日期覆盖完整，但刷新月份落后；资金仍读取缓存，不主动月刷新。
        payload=normalize_dates(['2026-01-05','2026-09-23','2026-12-31'],datetime(2026,8,1,tzinfo=ZoneInfo('Asia/Shanghai')))
        backend.set(CACHE_KEY,json.dumps(payload))
    ready,release=threading.Event(),threading.Event()
    def quotes_busy():
        with collection_entry(backend,lane='quotes'):
            ready.set()
            assert release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(quotes_busy)
        assert ready.wait(1)
        try:
            provider=FakeProvider(flow_rows,market_rows)
            store=RedisSnapshotStore(backend,240,lane='funds')
            result=MarketCollector(provider,store,CalendarService(backend,NoCalendarSource()),lane='funds').collect(at)
            assert result==('published' if cached else 'partial')
            assert provider.calls==(['market'] if cached else [])
            assert backend.get(AUTO_RETRY_KEY) is None and calls==[]
            assert set(store.load()['modules'])=={'marketFundFlow'}
        finally:
            release.set()
        pending.result(timeout=2)


def test_manual_market_workflow_uses_both_locks_and_quote_job_can_coexist_with_funds(monkeypatch):
    import app.runtime.workflows as workflows
    from app.core.config import load_settings
    backend=FakeRedis()
    settings=load_settings({})
    def market(settings,client,at,lane):
        assert lane=='market'
        assert client.get(QUOTES_ENTRY_KEY)==client.get(FUNDS_ENTRY_KEY)
        return 'published'
    monkeypatch.setattr(workflows,'_market',market)
    assert workflows.run_market(settings,backend)=='published'
    backend.set(FUNDS_ENTRY_KEY,'other',ex=30)
    assert workflows.run_market(settings,backend)=='locked'
    # 总闸关闭的行情任务无需资金入口，也不会创建XQ客户端。
    assert workflows.run_monitor(settings,backend)=='disabled'
    assert backend.get(QUOTES_ENTRY_KEY) is None and backend.get(FUNDS_ENTRY_KEY)=='other'
