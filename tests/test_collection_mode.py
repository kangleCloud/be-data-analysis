"""手动采集准入隔离、共享保护及并发业务写入的离线回归。"""

import json
import multiprocessing as mp
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
import redis
import requests
from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.main import create_app
from app.runtime.cooldown import ordinary_key, risk_key, record
from app.runtime.gates import QUOTES_ENTRY_KEY, FUNDS_ENTRY_KEY
from app.runtime.resources import SourceResourceError
from app.runtime.source_execution import (
    PREFIX, SourceCall, SourceControl, SourceExecutor, SourceCoolingError,
    SourceControlError, SourceBusyError, controlled_http,
)
from tests.test_snapshot import FakeRedis
from tests.test_source_execution import ControlRedis, frame_worker, ignore_term_worker
from tests.test_etf_monitor import _setup as etf_setup, AT, Calendar as EtfCalendar
from tests.test_stock_monitor import RedisClient, QuoteSource, STOCKS, Calendar
from tests.test_collector import FakeProvider, FakeCalendar, TRADING_AT
from app.etf_monitor.collector import EtfCollector, EtfStore
from app.market.collector import MarketCollector
from app.market.snapshot import RedisSnapshotStore
from app.stock_monitor.service import MonitorStore, StockMonitorSampler, normalize_quote

TOKEN = {'X-Internal-Token': 'offline-token'}
MANUAL = {**TOKEN, 'X-Collection-Mode': 'manual'}
ROUTES = [
    (f'/internal/jobs/v1/{kind}/refresh', None) for kind in ('calendar','market','monitor','etf')
] + [
    ('/internal/stock-monitor/v1/exchange-dictionary', None),
    ('/internal/stock-monitor/v1/profiles', {'symbols':['SH600000']}),
    ('/internal/etf-monitor/v1/dictionary', None),
    ('/internal/etf-monitor/v1/profiles', {'symbols':['SH510050']}),
    ('/internal/etf-monitor/v1/asset-allocation', {'symbol':'SH510050','reportPeriod':'20260930'}),
]


def api(backend=None, **kwargs):
    return TestClient(create_app(scheduler_enabled=False,
        settings=load_settings({'STOCK_MONITOR_INTERNAL_TOKEN':'offline-token'}),
        redis_factory=lambda: backend or FakeRedis(), **kwargs))


@pytest.mark.parametrize('route,body', ROUTES)
def test_token_authentication_precedes_mode_validation(route, body):
    calls = []
    client = api(job_runner=lambda kind, **kwargs: calls.append(kind))
    assert client.post(route, json=body, headers={'X-Collection-Mode':'invalid'}).status_code == 401
    assert client.post(route, json=body, headers={**TOKEN,'X-Collection-Mode':'invalid'}).status_code == 400
    assert calls == []


@pytest.mark.parametrize('value', ['', 'MANUAL', ' auto', 'manual ', 'manual,auto'])
def test_collection_mode_is_exact_and_not_normalized(value):
    assert api().post(ROUTES[0][0], headers={**TOKEN,'X-Collection-Mode':value}).status_code == 400


@pytest.mark.parametrize('kind', ['calendar','market','monitor','etf'])
def test_manual_jobs_bypass_existing_admission_and_propagate_without_polling(kind):
    from app.api.jobs import LOCK_PREFIX
    backend, calls = FakeRedis(), []
    for key in [LOCK_PREFIX+kind, QUOTES_ENTRY_KEY,FUNDS_ENTRY_KEY]:
        backend.set(key,'automatic-owner',ex=30)
    before = dict(backend.values), dict(backend.expiry)
    client = api(backend,job_runner=lambda kind, **kwargs: calls.append((kind,kwargs)) or 'published')
    assert client.post(f'/internal/jobs/v1/{kind}/refresh',headers=TOKEN).status_code == 409
    response = client.post(f'/internal/jobs/v1/{kind}/refresh',headers=MANUAL)
    assert response.status_code == 200 and response.json()['state'] == 'SUCCEEDED'
    assert calls == [(kind,{'mode':'manual'})]
    assert before == (backend.values,backend.expiry)
    assert 'jobId' not in response.json()


def test_missing_mode_defaults_to_auto():
    calls = []
    response = api(job_runner=lambda kind, **kwargs:calls.append(kwargs) or 'published').post(ROUTES[0][0],headers=TOKEN)
    assert response.status_code == 200 and calls == [{'mode':'auto'}]


def mode_worker(queue, url, call, keys, token, guard, deadline, read, parent, started):
    queue.put(('ok',{'mode':call.mode,'quota':keys,'guard':guard}))


def test_manual_source_child_sees_mode_and_no_auto_admission_changes():
    backend = ControlRedis()
    occupied = [QUOTES_ENTRY_KEY,FUNDS_ENTRY_KEY, 'task', 'symbol-interval', ordinary_key('module')]
    occupied += [f'{PREFIX}slot:{group}:{i}' for group in ('global','ths') for i in (0,1)]
    for key in occupied:
        backend.set(key,'auto-owner',ex=300)
    before = dict(backend.values),dict(backend.expiry)
    executor = SourceExecutor('redis://offline',client=backend,mode='manual',worker=mode_worker)
    with executor.batch(('task','another-token'),time.monotonic()+10):
        result = executor.call(SourceCall('fake','ths',cooldown_keys=('shared','module'), interval_key='symbol-interval',mode='manual'))
    assert result == {'mode':'manual','quota':None,'guard':None}
    assert before == (backend.values,backend.expiry)
    with pytest.raises(SourceBusyError):
        SourceExecutor('redis://offline',client=backend,worker=frame_worker).call(SourceCall('fake','ths'))


@pytest.mark.parametrize('status', [401,403,429])
@pytest.mark.parametrize('mode', ['auto','manual'])
def test_confirmed_risk_cooldown_blocks_both_modes_without_extending(status,mode):
    backend = ControlRedis()
    control = SourceControl(backend)
    call = SourceCall('fake','ths',cooldown_keys=('shared','module'),cooldown_policy='market',mode=mode)
    control.cool(call,{'http_status':status,'category':'HTTP_REJECTED'})
    backend.advance(120)
    with pytest.raises(SourceCoolingError):
        SourceExecutor('redis://offline',client=backend,mode=mode,worker=frame_worker).call(call)
    assert backend.ttl(risk_key('ths')) == 7080
    assert backend.get(ordinary_key('module')) is None


@pytest.mark.parametrize('mode', ['auto','manual'])
def test_unknown_legacy_cooldown_is_not_guessed_deleted_or_extended(mode):
    backend = ControlRedis()
    backend.set('old-cooldown','1',ex=289)
    call = SourceCall('fake','sina',cooldown_keys=('old-cooldown',),mode=mode)
    with pytest.raises(SourceCoolingError) as error:
        SourceExecutor('redis://offline',client=backend,mode=mode,worker=frame_worker).call(call)
    assert error.value.ttl == 289 and backend.ttl('old-cooldown') == 289


def test_manual_ordinary_failure_does_not_read_or_write_auto_cooldown():
    class NoOrdinaryRead(ControlRedis):
        def exists(self, *keys):
            assert ordinary_key('module') not in keys
            return super().exists(*keys)
    backend=NoOrdinaryRead()
    backend.set(ordinary_key('module'),'automatic-error',ex=250)
    executor=SourceExecutor('redis://offline',client=backend,mode='manual',worker=frame_worker)
    call=SourceCall('fake','ths',cooldown_keys=('shared','module'),cooldown_policy='market',mode='manual')
    assert executor.call(call) == {'name':'fake'}
    executor.control.cool(call,{'http_status':None,'category':'FORMAT'})
    assert backend.get(ordinary_key('module')) == 'automatic-error' and backend.ttl(ordinary_key('module')) == 250


@pytest.mark.parametrize('group,fund,gap', [('ths',False,1),('ths',True,2),('sina',False,.2),('xq',False,1),('sse',False,1),('szse',False,1),('bse',False,1)])
def test_auto_and_manual_share_actual_http_start_gap(group,fund,gap,monkeypatch):
    backend=ControlRedis(); clock=[0.0]; backend.rate_now=lambda:clock[0]*1000
    monkeypatch.setattr('app.runtime.source_execution.time.monotonic',lambda:clock[0])
    monkeypatch.setattr('app.runtime.source_execution.time.sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    starts=[]
    def send(session,request,**kwargs):
        starts.append(clock[0]); response=requests.Response();response.status_code=200;response._content=b'{}';return response
    monkeypatch.setattr(requests.Session,'send',send)
    control=SourceControl(backend)
    for mode in ('auto','manual','auto'):
        token=mode+str(len(starts));keys=control.acquire(group,token) if mode == 'auto' else None
        call=SourceCall('fake',group,domains=('offline.test',),fund_profile=fund,mode=mode)
        with controlled_http(control,call,keys,token,None,20,15):
            requests.get('https://offline.test/')
        if keys:
            control.release(keys,token)
    assert starts == pytest.approx([0,gap,2*gap])


def test_manual_redis_failure_stops_before_spawn():
    class Broken(ControlRedis):
        def ping(self):
            raise redis.ConnectionError('offline failure')
    executor=SourceExecutor('redis://offline',client=Broken(),mode='manual',worker=frame_worker)
    with pytest.raises(redis.ConnectionError):
        executor.call(SourceCall('fake','ths',mode='manual'))


def test_manual_memory_pressure_stops_without_cooling(monkeypatch):
    backend=ControlRedis()
    def fail():
        raise SourceResourceError('MEMORY_PRESSURE',state={'currentBytes':800*1024**2})
    monkeypatch.setattr('app.runtime.source_execution.check_memory',fail)
    with pytest.raises(SourceResourceError):
        SourceExecutor('redis://offline',client=backend,mode='manual',worker=frame_worker).call(SourceCall('fake','ths',mode='manual'))
    assert not any('cooldown' in key or 'risk:' in key or 'slot:' in key for key in backend.values)


@pytest.mark.parametrize('stop_one', [False,True])
def test_concurrent_manual_batches_have_independent_cancellation_and_reap(stop_one):
    backend=ControlRedis();ready=mp.get_context('spawn').Event()
    first=SourceExecutor('redis://offline',client=backend,mode='manual',worker=ignore_term_worker)
    second=SourceExecutor('redis://offline',client=backend,mode='manual',worker=mode_worker)
    call=SourceCall('fake','sina',{'ready':ready,'http_started':True},budget_seconds=3.5,mode='manual')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(first.call,call)
        assert ready.wait(5)
        with second.batch(deadline=time.monotonic()+10):
            assert second.call(replace(call,parameters={},budget_seconds=10))['mode']=='manual'
            if stop_one:
                first.stop()
            with pytest.raises(SourceControlError if stop_one else TimeoutError):
                future.result(timeout=6)
            assert second.call(replace(call,parameters={},budget_seconds=10))['mode']=='manual'
    assert backend.metrics()[-1]==[]


@pytest.mark.parametrize('status', [True,False,None])
def test_manual_market_outside_window_has_no_invented_dates_or_points(flow_rows,market_rows,status):
    backend=FakeRedis();source=FakeProvider(flow_rows,market_rows)
    calendar=SimpleNamespace(day_status=lambda *args:status)
    collector=MarketCollector(source,RedisSnapshotStore(backend,240),calendar,mode='manual')
    backend.set(QUOTES_ENTRY_KEY,'auto',ex=30)
    backend.set('stock:market:v1:lock','auto',ex=240)
    backend.set('stock:market:v1:min-interval','1',ex=120)
    assert collector.collect(TRADING_AT.replace(hour=18)) == 'published'
    modules=collector._store.load()['modules']
    assert all(module['tradeDate'] is None for module in modules.values())
    assert modules['marketFundFlow']['data']['series']==[]
    assert all(item['series']==[] for item in modules['coreIndices']['data']['items'])
    assert not any(':fund-series:' in key for key in backend.values)
    assert backend.get('stock:market:v1:lock')=='auto'


@pytest.mark.parametrize('status',[False,None])
def test_manual_etf_holiday_does_not_make_price_points(status):
    backend,source=etf_setup()
    backend.set('stock:etf-monitor:v1:lock','auto',ex=240)
    backend.set('stock:etf-monitor:v1:min-interval','1',ex=120)
    record(backend,('stock:etf-monitor:v1:sina:cooldown',),'sina','auto',{'category':'FORMAT'})
    assert EtfCollector(source,EtfStore(backend),EtfCalendar(status),mode='manual').collect(AT)=='published'
    snapshot=EtfStore(backend).load()
    assert snapshot['tradeDate'] is None and snapshot['items'][0]['quote']['tradeDate'] is None
    assert snapshot['items'][0]['priceSeries']==[]
    assert not any(':price-series:' in key for key in backend.values)
    assert backend.get('stock:etf-monitor:v1:lock')=='auto'


def test_manual_stock_outside_window_preserves_real_source_date_and_no_points():
    from app.stock_monitor.service import ENABLED_KEY, SAMPLE_LOCK_KEY, LAST_REQUEST_PREFIX, SERIES_PREFIX
    backend=RedisClient();backend.set(ENABLED_KEY,json.dumps(STOCKS));source=QuoteSource()
    backend.set(SAMPLE_LOCK_KEY,'auto',ex=240)
    for stock in STOCKS:
        backend.set(LAST_REQUEST_PREFIX+stock['symbol'],'auto',ex=120)
    assert StockMonitorSampler(MonitorStore(backend),source,Calendar(),xq_enabled=True,mode='manual').sample(AT.replace(day=29,hour=18))=='published'
    for stock in STOCKS:
        quote=MonitorStore(backend).quote(stock['symbol'])
        assert quote['tradeDate']=='2026-09-28' and quote['sourceTime'].startswith('2026-09-28T09:32:00')
    assert not any(key.startswith(SERIES_PREFIX) for key in backend.values)
    assert backend.get(SAMPLE_LOCK_KEY)=='auto'


def test_stock_transaction_rechecks_source_monotonicity_after_concurrent_writes():
    backend=RedisClient();store=MonitorStore(backend)
    newer=normalize_quote('SH600000',{'time':'2026-09-28T15:00:01+08:00','current':12},AT.replace(hour=15,minute=2))
    older=normalize_quote('SH600000',{'time':'2026-09-28T15:00:00+08:00','current':10},AT.replace(hour=15,minute=3))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda quote:store.write_quote(quote,append_point=False),[newer,older]))
    assert store.quote('SH600000')['price']==12
    sampler=StockMonitorSampler(store,QuoteSource(),Calendar(),xq_enabled=True,mode='manual')
    assert sampler._close_confirmed('SH600000',AT.date())
    second=MonitorStore(RedisClient());second.write_quote(older,append_point=False)
    assert not StockMonitorSampler(second,QuoteSource(),Calendar())._close_confirmed('SH600000',AT.date())


def test_manual_calendar_bypasses_admission_but_keeps_current_year_and_coverage():
    from app.calendar.service import CalendarService, CACHE_KEY, LOCK_KEY, AUTO_RETRY_KEY, MANUAL_INTERVAL_KEY
    from tests.test_trading_calendar import Source
    backend=FakeRedis()
    for key in (LOCK_KEY,AUTO_RETRY_KEY,MANUAL_INTERVAL_KEY):
        backend.set(key,'auto-owner',ex=600)
    before={key:(backend.get(key),backend.ttl(key)) for key in backend.values}
    source=Source(['2026-01-05','2026-12-31'])
    manual=CalendarService(backend,source,mode='manual')
    assert manual.refresh(AT,on_demand=True)=='refreshed'
    assert before=={key:(backend.get(key),backend.ttl(key)) for key in before}
    old=backend.get(CACHE_KEY)
    source.values=['2025-12-31']
    assert manual.refresh(AT)=='failed' and backend.get(CACHE_KEY)==old
    source.values=['2026-01-05','2026-09-27']
    assert manual.refresh(AT)=='failed' and backend.get(CACHE_KEY)==old
    assert CalendarService(backend,source).refresh(AT)=='locked'


def test_manual_etf_profiles_ignore_existing_batch_and_code_intervals_without_changes():
    from app.etf_monitor.profiles import collect_profiles, LOCK_KEY, INTERVAL_PREFIX, ProfileBatchError
    from tests.test_etf_profiles import Source, Clock
    source=Source(Clock());backend=FakeRedis();symbols=['SH510050','SZ159919']
    for key in [LOCK_KEY,*(INTERVAL_PREFIX+symbol for symbol in symbols)]:
        backend.set(key,'auto-owner',ex=1800)
    before=dict(backend.values),dict(backend.expiry)
    result=collect_profiles(source,backend,symbols,mode='manual')
    assert result['sourceStatus']==dict.fromkeys(symbols,'OK')
    assert [call[0] for call in source.calls]==['510050','159919']
    assert before==(backend.values,backend.expiry)
    with pytest.raises(ProfileBatchError) as error:
        collect_profiles(source,backend,symbols)
    assert error.value.status_code==409


@pytest.mark.parametrize('route,body',ROUTES[4:])
def test_internal_routes_manual_bypass_occupied_entry_but_keep_source_gate_and_code_limit(route,body):
    from tests.test_etf_profiles import rows
    source=SimpleNamespace(quotes=lambda:[{'代码':'sh510050','名称':'50ETF','最新价':2}],
        profile=lambda symbol,**kwargs:rows(symbol), all_a_stocks=lambda:[])
    backend=FakeRedis();backend.set(QUOTES_ENTRY_KEY,'auto-owner',ex=30)
    backend.set('stock:etf-monitor:v1:profiles:python:lock','auto-owner',ex=210)
    client=api(backend,etf_factory=lambda:source,exchange_factory=lambda:source)
    response=client.post(route,json=body,headers=MANUAL)
    # 雪球关闭仍拒绝，公开字典与同花顺资料继续执行。
    assert response.status_code==(503 if route.endswith('asset-allocation') or route.startswith('/internal/stock-monitor') and route.endswith('profiles') else 200)
    assert backend.get(QUOTES_ENTRY_KEY)=='auto-owner'
    if body and route.endswith('profiles') and 'etf-monitor' in route:
        assert client.post(route,json={'symbols':['SH510050']*11},headers=MANUAL).status_code==422


def test_concurrent_market_fund_commits_merge_series_dedupe_and_chain_versions():
    from app.market.snapshot import SNAPSHOT_KEY, UPDATES_CHANNEL, FUND_SERIES_PREFIX
    from app.stock_monitor.service import ENABLED_KEY, MONITOR_UPDATES_CHANNEL
    backend=FakeRedis();backend.set(ENABLED_KEY,json.dumps(STOCKS[:1]));store=RedisSnapshotStore(backend,240)
    def snapshot(minute):
        timestamp=f'2026-09-28T10:{minute:02d}:00+08:00'
        point={'collectedAt':timestamp,'inflow':10,'outflow':3,'netAmount':7}
        return {'schemaVersion':1,'provider':'akshare','generatedAt':timestamp,'modules':{'marketFundFlow':{
            'status':'FRESH','tradeDate':'2026-09-28','tradeDateBasis':'CALENDAR',
            'lastSuccessAt':timestamp,'lastAttemptAt':timestamp,'message':None,
            'data':{'source':'THS_INDIVIDUAL_AGGREGATE','latest':{**point,'riseCount':1,'fallCount':0,'flatCount':0,'stockCount':1},'series':[point]}}}},point
    with ThreadPoolExecutor(max_workers=2) as pool:
        def save(minute):
            payload,point=snapshot(minute)
            store.save(payload,trade_date='2026-09-28',fund_points={'SH600000':point})
        list(pool.map(save,[2,0]))
    save(2)  # 同一个实际完成时间不能重复写资金点。
    current=json.loads(backend.get(SNAPSHOT_KEY))
    assert current['modules']['marketFundFlow']['lastSuccessAt'].startswith('2026-09-28T10:02')
    assert len(current['modules']['marketFundFlow']['data']['series'])==2
    assert len(json.loads(backend.get(FUND_SERIES_PREFIX+'2026-09-28:SH600000')))==2
    notices=[json.loads(value) for action,key,value in backend.events if action=='publish' and key==UPDATES_CHANNEL]
    assert [notice['previousSnapshotId'] for notice in notices[1:]]==[notice['snapshotId'] for notice in notices[:-1]]
    monitor=[json.loads(value) for action,key,value in backend.events if action=='publish' and key==MONITOR_UPDATES_CHANNEL]
    assert [notice['baseStateId'] for notice in monitor[1:]]==[notice['stateId'] for notice in monitor[:-1]]


def test_concurrent_etf_publish_keeps_latest_quote_and_unions_actual_points():
    backend,source=etf_setup();store=EtfStore(backend)
    assert EtfCollector(source,store,EtfCalendar(),mode='manual').collect(AT)=='published'
    first=store.load();second=json.loads(json.dumps(first))
    second['generatedAt']='2026-09-28T09:34:00+08:00'
    item=second['items'][0];item['quote'].update(collectedAt=second['generatedAt'],price=3)
    item['priceSeries']=[{'collectedAt':second['generatedAt'],'price':3}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(store.publish,[second,first]))
    current=store.load()
    assert current['generatedAt']==second['generatedAt'] and current['items'][0]['quote']['price']==3
    assert len(current['items'][0]['priceSeries'])==2
    from app.etf_monitor.collector import UPDATES_CHANNEL
    notices=[json.loads(value) for action,key,value in backend.events if action=='publish' and key==UPDATES_CHANNEL]
    assert [notice['baseStateId'] for notice in notices[1:]]==[notice['stateId'] for notice in notices[:-1]]


def test_manual_market_normalization_failure_does_not_pollute_auto_cooldown(flow_rows,market_rows):
    backend=FakeRedis();source=FakeProvider(flow_rows,market_rows);source.fail.add('flow:industry')
    collector=MarketCollector(source,RedisSnapshotStore(backend,240),FakeCalendar(source),mode='manual')
    assert collector.collect(TRADING_AT)=='partial'
    assert not any('cooldown:' in key or ':interval:' in key or ':slot:' in key for key in backend.values)
    source.fail.clear()
    assert collector.collect(TRADING_AT)=='published'


def test_dictionary_manual_forces_one_source_call_auto_reuses_cache_and_failure_keeps_it():
    from app.etf_monitor.dictionary import KEY, save_dictionary
    from app.api import etf_monitor as module
    from datetime import datetime
    backend=FakeRedis();calls=[]
    now=datetime.now(module.SHANGHAI)
    source=SimpleNamespace(quotes=lambda:calls.append('sina') or [{'代码':'sh510050','名称':'更新ETF','最新价':2}])
    save_dictionary(backend,[{'代码':'sh510050','名称':'旧ETF','最新价':2}],now)
    before=backend.get(KEY)
    client=api(backend,etf_factory=lambda:source)
    path='/internal/etf-monitor/v1/dictionary'
    assert client.post(path,headers=TOKEN).status_code==200 and calls==[]
    response=client.post(path,headers=MANUAL)
    assert response.status_code==200 and calls==['sina']
    assert response.json()['etfs'][0]['name']=='更新ETF'
    assert backend.get(KEY)!=before
    current=backend.get(KEY)
    def fail():
        calls.append('failed-sina');raise ConnectionError('offline source')
    source.quotes=fail
    assert client.post(path,headers=MANUAL).status_code==502
    assert backend.get(KEY)==current
    assert client.post(path,headers=TOKEN).status_code==200
    assert calls==['sina','failed-sina']


def test_actual_lua_manual_ignores_slots_and_intervals_but_keeps_risk_and_rates():
    import fakeredis
    backend=fakeredis.FakeRedis(decode_responses=True);control=SourceControl(backend)
    interval='stock:test:interval';backend.set(interval,'auto-owner',ex=120)
    backend.set(ordinary_key('module'),'ordinary',ex=300)
    call=SourceCall('fake','ths',cooldown_keys=('module',),interval_key=interval,mode='manual')
    control.request_turn(call,None,'manual',None,time.monotonic()+5)
    assert backend.get(interval)=='auto-owner' and backend.ttl(ordinary_key('module'))>=299
    assert backend.get(PREFIX+'rate:ths') is not None
    assert list(backend.scan_iter(match=PREFIX+'slot:*'))==[]
    control.cool(call,{'http_status':401,'category':'HTTP_REJECTED'})
    with pytest.raises(SourceCoolingError):
        control.request_turn(call,None,'manual',None,time.monotonic()+5)


def mixed_http_parent(backend,barrier,queue,mode):
    from tests.test_source_execution import http_frame_worker
    barrier.wait(timeout=10)
    executor=SourceExecutor('redis://offline',client=backend,mode=mode,worker=http_frame_worker)
    queue.put(executor.call(SourceCall('fake','ths',{'backend':backend},budget_seconds=15,domains=('offline.test',),mode=mode)))


def test_spawn_auto_and_manual_actual_http_starts_share_redis_rate_without_manual_quota():
    from tests.test_source_execution import RedisManager
    context=mp.get_context('spawn')
    with RedisManager(ctx=context) as manager:
        backend=manager.ControlRedis();barrier=context.Barrier(2);queue=context.Queue()
        parents=[context.Process(target=mixed_http_parent,args=(backend,barrier,queue,mode)) for mode in ('auto','manual')]
        try:
            for parent in parents:
                parent.start()
            starts=sorted(point for _ in parents for point in queue.get(timeout=20))
            for parent in parents:
                parent.join(timeout=3)
                assert parent.exitcode==0
            assert len(starts)==4 and all(later-earlier>=.98 for earlier,later in zip(starts,starts[1:]))
            maximum,groups,releases,remaining=backend.metrics()
            assert maximum==1 and groups=={'ths':1} and releases==1 and remaining==[]
        finally:
            for parent in parents:
                if parent.is_alive():
                    parent.kill()
                parent.join(timeout=2)
            queue.close()


def test_manual_pressure_after_spawn_reaps_term_ignoring_child_without_auto_key_pollution(monkeypatch):
    from app.runtime.resources import MEMORY_LIMIT_BYTES
    backend=ControlRedis();ready=mp.get_context('spawn').Event();high=threading.Event()
    monkeypatch.setattr('app.runtime.resources.memory_state',lambda:{'currentBytes':MEMORY_LIMIT_BYTES if high.is_set() else 0})
    executor=SourceExecutor('redis://offline',client=backend,mode='manual',worker=ignore_term_worker)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(executor.call,SourceCall('fake','ths',{'ready':ready},budget_seconds=900,mode='manual'))
        assert ready.wait(5);high.set()
        with pytest.raises(SourceResourceError):
            pending.result(timeout=4)
    assert not any(child.is_alive() for child in mp.active_children())
    assert not any('risk:' in key or ':ordinary' in key or ':slot:' in key for key in backend.values)


def test_manual_redis_loss_after_spawn_reaps_child(monkeypatch):
    class BrokenReads(ControlRedis):
        failed=False
        def get(self,key):
            if self.failed and key==PREFIX+'health':
                raise redis.ConnectionError('offline control unavailable')
            return super().get(key)
    backend=BrokenReads();ready=mp.get_context('spawn').Event()
    executor=SourceExecutor('redis://offline',client=backend,mode='manual',worker=ignore_term_worker)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(executor.call,SourceCall('fake','ths',{'ready':ready},budget_seconds=900,mode='manual'))
        assert ready.wait(5);backend.failed=True
        with pytest.raises(redis.ConnectionError):
            pending.result(timeout=4)
    assert not any(child.is_alive() for child in mp.active_children())


def test_manual_stale_quote_does_not_downgrade_newer_success():
    from app.stock_monitor.service import ENABLED_KEY
    backend=RedisClient();backend.set(ENABLED_KEY,json.dumps(STOCKS[:1]));store=MonitorStore(backend)
    newer=normalize_quote('SH600000',{'time':'2026-09-28T10:00:00+08:00','current':12},AT.replace(hour=10,minute=0))
    store.write_quote(newer,append_point=False)
    assert StockMonitorSampler(store,QuoteSource(),Calendar(),xq_enabled=True,mode='manual').sample(AT.replace(hour=18))=='published'
    assert store.quote('SH600000')==newer


def test_etf_failure_with_old_cached_item_does_not_overwrite_interleaved_success():
    backend,source=etf_setup();store=EtfStore(backend)
    assert EtfCollector(source,store,EtfCalendar(),mode='manual').collect(AT)=='published'
    failure=json.loads(json.dumps(store.load()))
    failure['generatedAt']='2026-09-28T09:38:00+08:00';failure['items'][0]['quote']['status']='STALE'
    assert EtfCollector(source,store,EtfCalendar(),mode='manual').collect(AT.replace(minute=34))=='published'
    newest=store.load()['items'][0]
    store.publish(failure)
    assert store.load()['items'][0]==newest


@pytest.mark.parametrize('calendar_status,at_hour', [(True,18),(False,10),(None,10)])
def test_manual_market_failed_undated_refresh_clears_snapshot_series_and_keeps_history(flow_rows,market_rows,calendar_status,at_hour):
    from app.stock_monitor.service import ENABLED_KEY
    from app.market.snapshot import FUND_SERIES_PREFIX
    backend=FakeRedis();backend.set(ENABLED_KEY,json.dumps(STOCKS[:1]));source=FakeProvider(flow_rows,market_rows)
    store=RedisSnapshotStore(backend,240)
    assert MarketCollector(source,store,FakeCalendar(source),mode='manual').collect(TRADING_AT)=='published'
    history={key:value for key,value in backend.values.items() if key.startswith(FUND_SERIES_PREFIX)}
    source.fail.update(['market','index','flow:industry','flow:concept'])
    calendar=SimpleNamespace(day_status=lambda *args:calendar_status)
    assert MarketCollector(source,store,calendar,mode='manual').collect(TRADING_AT.replace(hour=at_hour))=='partial'
    modules=store.load()['modules']
    assert all(module['tradeDate'] is None and module['status']=='STALE' for module in modules.values())
    assert modules['marketFundFlow']['data']['series']==[]
    assert all(item['series']==[] for item in modules['coreIndices']['data']['items'])
    assert history=={key:value for key,value in backend.values.items() if key.startswith(FUND_SERIES_PREFIX)}


@pytest.mark.parametrize('calendar_status,at_hour', [(True,18),(False,10),(None,10)])
def test_manual_etf_failed_undated_refresh_clears_dates_and_series_without_history_writes(calendar_status,at_hour):
    from app.etf_monitor.collector import PRICE_PREFIX
    backend,source=etf_setup();store=EtfStore(backend)
    assert EtfCollector(source,store,EtfCalendar(),mode='manual').collect(AT)=='published'
    history={key:value for key,value in backend.values.items() if key.startswith(PRICE_PREFIX)}
    source.fail=True
    assert EtfCollector(source,store,EtfCalendar(calendar_status),mode='manual').collect(AT.replace(hour=at_hour))=='partial'
    snapshot=store.load();item=snapshot['items'][0]
    assert snapshot['tradeDate'] is None and item['quote']['tradeDate'] is None
    assert item['quote']['status']=='STALE' and item['priceSeries']==[] and item['fundSeries']==[]
    assert history=={key:value for key,value in backend.values.items() if key.startswith(PRICE_PREFIX)}
