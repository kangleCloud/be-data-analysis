"""共享配额/HTTP速率和 spawn 回收的离线模拟，无真实 Redis/源请求。"""

import multiprocessing as mp
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis
import requests

from app.runtime.source_execution import (
    ACQUIRE_SCRIPT, RENEW_SCRIPT, RELEASE_SCRIPT, RATE_SCRIPT, TASK_RENEW_SCRIPT,
    GLOBAL_LIMIT, SOURCE_LIMIT, PREFIX, SourceCall, SourceCallError, SourceControl, SourceControlError, SourceCoolingError,
    SourceExecutor, SourceBusyError, controlled_http,
)
from tests.test_snapshot import FakeRedis


class ControlRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.mutex = threading.RLock()
        self.max_global = 0
        self.max_groups = {}
        self.fail_control = False
        self.renewals = 0
        self.releases = 0
        self.rate_now = lambda: time.monotonic()*1000

    def set(self, *args, **kwargs):
        with self.mutex:
            return super().set(*args, **kwargs)

    def eval(self, script, count, *arguments):
        with self.mutex:
            if self.fail_control:
                raise redis.ConnectionError("private-control-password")
            keys, args = arguments[:count], arguments[count:]
            if script == ACQUIRE_SCRIPT:
                global_slot = next((i for i in range(int(args[2])) if self.get(keys[i]) is None), None)
                group_slot = next((i for i in range(int(args[2]),count) if self.get(keys[i]) is None), None)
                if global_slot is None or group_slot is None:
                    return []
                for i in (global_slot, group_slot):
                    self.set(keys[i], args[0], ex=args[1])
                active = [key for key in list(self.values) if key.startswith(PREFIX+'slot:') and self.get(key)]
                self.max_global = max(self.max_global, sum(':global:' in key for key in active))
                group = keys[group_slot].split(':')[-2]
                self.max_groups[group] = max(self.max_groups.get(group, 0), sum(f':{group}:' in key for key in active))
                return [global_slot+1, group_slot+1]
            if script == RENEW_SCRIPT:
                if any(self.get(key) != args[0] for key in keys):
                    return 0
                for key in keys:
                    self.expiry[key] = self.now+args[1]
                self.renewals += 1
                return 1
            if script == RELEASE_SCRIPT:
                for key in keys:
                    if self.get(key) == args[0]:
                        self.values.pop(key)
                        self.expiry.pop(key, None)
                self.releases += 1
                return 1
            if script == TASK_RENEW_SCRIPT:
                if self.get(keys[0]) != args[0]:
                    return 0
                self.expiry[keys[0]] = self.now+args[1]
                return 1
            if script == RATE_SCRIPT:
                token, interval, extra, guard, symbol_interval = args
                if any(self.get(key) != token for key in keys[:2]):
                    return [-1, 0]
                if guard and self.get(keys[4]) != guard:
                    return [-1, 0]
                for key in keys[6:]:
                    if self.get(key) is not None:
                        return [-2, self.ttl(key)]
                owner = self.get(keys[5]) if symbol_interval else None
                if owner and owner != token:
                    return [-3, self.ttl(keys[5])]
                now = self.rate_now()
                remaining = max(float(self.get(key) or 0) for key in keys[2:4])-now
                if remaining > 0:
                    return [0, remaining]
                if symbol_interval and not owner:
                    self.set(keys[5], token, ex=symbol_interval)
                self.set(keys[2], now+interval, ex=30)
                self.set(keys[3], now+extra, ex=30)
                return [1, 0]
            return super().eval(script, count, *arguments)


def frame_worker(queue, url, call, keys, token, guard, deadline, read, parent, started_event):
    time.sleep(call.parameters.get('delay', 0.05))
    queue.put(('ok', {'name': call.function}))


def ignore_term_worker(queue, url, call, keys, token, guard, deadline, read, parent, started_event):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    call.parameters['ready'].set()
    if call.parameters.get('http_started'):
        started_event.set()
    time.sleep(60)


def test_cross_entry_executors_are_nonqueuing_and_single_source():
    backend = ControlRedis()
    ready = mp.get_context('spawn').Event()
    executor = SourceExecutor('redis://offline', client=backend, worker=ignore_term_worker)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(executor.call, SourceCall('fake','ths',{'ready':ready},900))
        assert ready.wait(5)
        other = SourceExecutor('redis://offline',client=backend,worker=frame_worker)
        with pytest.raises(SourceBusyError):
            other.call(SourceCall('fake','sina',budget_seconds=5))
        executor.stop()
        with pytest.raises(SourceControlError):
            first.result(timeout=4)
    assert backend.max_global == 1 and backend.max_groups == {'ths':1}
    assert backend.releases == 1 and backend.metrics()[-1] == []


def test_http_start_spacing_covers_session_and_different_executors(monkeypatch):
    backend = ControlRedis()
    clock = [0.0]
    backend.rate_now = lambda: clock[0]*1000
    monkeypatch.setattr('app.runtime.source_execution.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('app.runtime.source_execution.time.sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    calls = []
    def send(session, request, **kwargs):
        calls.append((request.url, clock[0], kwargs['timeout']))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{}'
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    for token, path in [('first','https://xueqiu.com/'), ('second','https://stock.xueqiu.com/v5/stock/quote.json')]:
        control = SourceControl(backend)
        keys = control.acquire('xq', token)
        call = SourceCall('fake','xq', domains=('xueqiu.com','stock.xueqiu.com'))
        with controlled_http(control, call, keys, token, None, 20, 15):
            requests.Session().get(path, timeout=(2,7))
        control.release(keys, token)
    assert [entry[1] for entry in calls] == pytest.approx([0,1])
    assert all(entry[2] == (2,7) for entry in calls)


def test_ths_market_and_fund_intervals_are_shared(monkeypatch):
    backend = ControlRedis()
    clock = [0.0]
    backend.rate_now = lambda: clock[0]*1000
    monkeypatch.setattr('app.runtime.source_execution.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('app.runtime.source_execution.time.sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    control = SourceControl(backend)
    keys = control.acquire('ths','token')
    fund = SourceCall('fund_info_ths','ths',fund_profile=True)
    market = SourceCall('stock_fund_flow_individual','ths')
    points = []
    for call in (fund,market,fund):
        control.request_turn(call, keys, 'token', None, 20)
        points.append(clock[0])
    assert points == pytest.approx([0,1,2])


def test_cooldown_blocks_next_http_without_renewing_ttl(monkeypatch):
    backend = ControlRedis()
    control = SourceControl(backend)
    keys = control.acquire('ths','token')
    call = SourceCall('fake','ths',cooldown_keys=('cooling',),cooldown_policy='market')
    control.cool(call, {'http_status':429,'category':'HTTP_REJECTED','exception_type':'HTTPError'})
    backend.advance(120)
    for key in keys:
        backend.set(key, 'token', ex=30)
    with pytest.raises(SourceCoolingError) as error:
        control.request_turn(call, keys, 'token', None, time.monotonic()+3)
    assert error.value.ttl == 7080
    control.cool(call, {'http_status':429,'category':'HTTP_REJECTED','exception_type':'HTTPError'})
    assert backend.ttl('cooling') == 7080

# Manager 后端由独立服务进程提供；不同父进程共享原子控制状态。
from multiprocessing.managers import BaseManager
from types import SimpleNamespace
from contextlib import closing

from app.runtime.source_execution import SourceNotStartedError, SourceThrottledError, completed


def control_metrics(self):
    with self.mutex:
        return self.max_global, self.max_groups, self.releases, [
            key for key in list(self.values) if key.startswith(PREFIX+'slot:') and self.get(key)]


ControlRedis.metrics = control_metrics


class RedisManager(BaseManager):
    pass


RedisManager.register('ControlRedis', ControlRedis)


def parent_entry(backend, barrier, queue, group, lane="quotes"):
    executor = SourceExecutor('redis://offline', client=backend, worker=frame_worker)
    barrier.wait(timeout=10)
    try:
        function = 'stock_fund_flow_individual' if lane == 'funds' else 'fake'
        queue.put(executor.call(SourceCall(function, group, {'delay':0.8,'symbol':'即时'},10)))
    except Exception as exc:
        queue.put(type(exc).__name__)


def test_independent_parent_processes_share_atomic_quotas():
    context = mp.get_context('spawn')
    with RedisManager(ctx=context) as manager:
        backend = manager.ControlRedis()
        groups = [('ths','quotes')]*2+[('ths','funds')]*2
        barrier, queue = context.Barrier(len(groups)), context.Queue()
        parents = [context.Process(target=parent_entry, args=(backend, barrier, queue, group,lane)) for group,lane in groups]
        try:
            for process in parents:
                process.start()
            results = [queue.get(timeout=15) for _ in parents]
            for process in parents:
                process.join(timeout=3)
                assert process.exitcode == 0
            assert results.count({'name':'fake'}) == 1
            assert results.count({'name':'stock_fund_flow_individual'}) == 1
            assert results.count('SourceBusyError') == len(groups)-2
            maximum, groups_max, releases, remaining = backend.metrics()
            assert maximum == 2 and groups_max == {"ths":2}
            assert releases == 2 and remaining == []
        finally:
            for process in parents:
                if process.is_alive():
                    process.kill()
                process.join(timeout=2)
            queue.close()


def test_actual_lua_tokens_leases_rates_and_cooldown():
    import fakeredis
    client = fakeredis.FakeRedis(decode_responses=True)
    control = SourceControl(client)
    first = control.acquire('ths', 'first')
    second = control.acquire('ths', 'second')
    assert second is not None
    assert control.acquire('ths', 'fifth') is None
    assert all(0 < client.ttl(key) <= 30 for key in first)
    control.renew(first, 'first')
    with pytest.raises(SourceControlError):
        control.renew(first, 'wrong')
    fund = SourceCall('fund_info_ths', 'ths', fund_profile=True, cooldown_keys=('cool',))
    control.request_turn(fund, first, 'first', None, time.monotonic()+1)
    with pytest.raises(TimeoutError):
        control.request_turn(fund, first, 'first', None, time.monotonic()+0.03)
    assert int(client.get(PREFIX+'rate:ths:fund')) > int(client.get(PREFIX+'rate:ths'))
    client.set('cool', '1', ex=300)
    with pytest.raises(SourceCoolingError) as error:
        control.request_turn(fund, first, 'first', None, time.monotonic()+1)
    assert 0 < error.value.ttl <= 300
    # 已过期/被接管的旧令牌不能删除新租约。
    client.set(first[0], 'replacement', ex=30)
    control.release(first, 'first')
    assert client.get(first[0]) == 'replacement'
    assert client.get(first[1]) is None
    control.release(second,"second")


@pytest.mark.parametrize('group,interval', [('sina', .2), ('xq', 1), ('sse', 1), ('szse', 1), ('bse', 1)])
def test_every_page_http_has_shared_group_gap(monkeypatch, group, interval):
    backend, clock = ControlRedis(), [0.0]
    backend.rate_now = lambda: clock[0]*1000
    monkeypatch.setattr('app.runtime.source_execution.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('app.runtime.source_execution.time.sleep', lambda value: clock.__setitem__(0, clock[0]+value))
    points = []
    def send(session, request, **kwargs):
        points.append(clock[0])
        assert kwargs['timeout'] == (3, 3)
        response = requests.Response()
        response.status_code, response._content = 200, b'{}'
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    control = SourceControl(backend)
    keys = control.acquire(group, 'token')
    call = SourceCall('fake', group, domains=('offline.test',))
    with controlled_http(control, call, keys, 'token', None, 10, 15):
        for page in range(3):
            requests.Session().get(f'https://offline.test/page/{page}', timeout=3)
    assert points == pytest.approx([0, interval, interval*2])


def test_same_symbol_interval_is_reserved_only_at_first_http(monkeypatch):
    backend, clock = ControlRedis(), [0.0]
    backend.rate_now = lambda: clock[0]*1000
    monkeypatch.setattr('app.runtime.source_execution.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('app.runtime.source_execution.time.sleep', lambda value: clock.__setitem__(0, clock[0]+value))
    control = SourceControl(backend)
    keys = control.acquire('xq', 'first')
    call = SourceCall('quote', 'xq', interval_key='stock:monitor:v1:sample:lastRequest:SH600000')
    assert backend.get(call.interval_key) is None
    control.request_turn(call, keys, 'first', None, 10)
    control.request_turn(call, keys, 'first', None, 10)  # 同一次函数的会话与数据请求。
    assert clock[0] == pytest.approx(1)
    assert backend.ttl(call.interval_key) == 120
    control.release(keys,'first')
    second = control.acquire('xq','second')
    with pytest.raises(SourceThrottledError):
        control.request_turn(call, second, 'second', None, 10)


def test_quota_wait_budget_never_starts_worker():
    backend = ControlRedis()
    control = SourceControl(backend)
    held = [control.acquire('ths',token) for token in ('one','two')]
    executor = SourceExecutor('redis://offline', client=backend, worker=frame_worker)
    with pytest.raises(SourceNotStartedError):
        executor.call(SourceCall('fake', 'ths', budget_seconds=2.05))
    assert backend.releases == 0
    for keys, token in zip(held, ('one','two')):
        control.release(keys, token)


def test_early_cancel_kills_term_ignoring_child_before_releasing_slots():
    backend = ControlRedis()
    context = mp.get_context('spawn')
    ready = context.Event()
    executor = SourceExecutor('redis://offline', client=backend, worker=ignore_term_worker)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor.call, SourceCall('fake', 'ths', {'ready': ready}, 900))
        assert ready.wait(timeout=5)
        started = time.monotonic()
        executor.stop()
        with pytest.raises(SourceControlError):
            future.result(timeout=4)
        assert time.monotonic()-started < 2.5
    assert backend.releases == 1
    assert backend.metrics()[-1] == []
    assert not any(process.is_alive() for process in mp.active_children())


def test_redis_control_failure_aborts_before_spawn_and_no_secret_message():
    backend = ControlRedis()
    backend.fail_control = True
    executor = SourceExecutor('redis://offline', client=backend, worker=frame_worker)
    with pytest.raises(redis.ConnectionError):
        executor.call(SourceCall('fake', 'ths'))
    assert backend.metrics()[-1] == []


def test_shortened_renewal_schedule_preserves_owned_slots(monkeypatch):
    backend = ControlRedis()
    monkeypatch.setattr('app.runtime.source_execution.RENEW_SECONDS', .05)
    executor = SourceExecutor('redis://offline', client=backend, worker=frame_worker)
    assert executor.call(SourceCall('fake', 'ths', {'delay': .3}, 5)) == {'name':'fake'}
    assert backend.renewals >= 2
    assert backend.releases == 1


def test_close_completed_iterator_cancels_inflight_before_more_candidates():
    backend, context = ControlRedis(), mp.get_context('spawn')
    ready = context.Event()
    executor = SourceExecutor('redis://offline', client=backend, worker=ignore_term_worker)
    source = SimpleNamespace(executor=executor)
    def fast():
        return 'fast'
    later = []
    actions = [('fast','ths',fast), ('slow','ths',lambda: executor.call(SourceCall('fake','ths',{'ready':ready},900))),
               ('later','ths',lambda: later.append(True))]
    with executor.batch(), closing(completed(actions, source=source)) as results:
        key, value, error, finished_at = next(results)
        assert (key,value,error) == ('fast','fast',None)
        assert finished_at <= time.monotonic()
        # 模拟业务发布失败后的关闭；不得准入 later，也不得残留 slow。
    assert later == []
    assert backend.metrics()[-1] == []


def test_no_http_timeout_does_not_start_cooldown(monkeypatch):
    import sys
    import queue
    from app.runtime.source_execution import _source_worker
    backend, messages = ControlRedis(), queue.Queue()
    control = SourceControl(backend)
    keys = control.acquire('sina','token')
    def function():
        raise TimeoutError('等待初始化超时')
    monkeypatch.setitem(sys.modules, 'akshare', SimpleNamespace(fake=function))
    monkeypatch.setattr('app.runtime.source_execution._client', lambda url: backend)
    event = threading.Event()
    _source_worker(messages,'redis://offline', SourceCall('fake','sina',cooldown_keys=('cool',),cooldown_policy='etf'),
                   keys,'token',None,time.monotonic()+10,15,os.getpid(),event)
    status, metadata = messages.get_nowait()
    assert status == 'error' and metadata['category'] == 'TIMEOUT'
    assert not event.is_set() and backend.get('cool') is None


def test_http_rejection_prevents_following_page_and_preserves_short_timeout(monkeypatch):
    backend = ControlRedis()
    control = SourceControl(backend)
    keys = control.acquire('ths','token')
    call = SourceCall('fake','ths',domains=('offline.test',),cooldown_keys=('cool',),cooldown_policy='market')
    calls = []
    def send(session, request, **kwargs):
        calls.append(kwargs)
        response = requests.Response()
        response.status_code, response._content = 429, b'private response'
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    with controlled_http(control, call, keys, 'token', None, time.monotonic()+10, 15):
        with pytest.raises(requests.HTTPError):
            requests.get('https://offline.test/page/1', timeout=(2,4))
        with pytest.raises(SourceCoolingError):
            requests.get('https://offline.test/page/2', timeout=(2,4))
    assert len(calls) == 1 and calls[0]['timeout'] == (2,4)
    assert backend.ttl('cool') == 7200


def test_deadline_kills_started_term_ignoring_worker_within_original_budget():
    backend, context = ControlRedis(), mp.get_context('spawn')
    ready = context.Event()
    executor = SourceExecutor('redis://offline', client=backend, worker=ignore_term_worker)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        executor.call(SourceCall('fake','ths',{'ready':ready, 'http_started':True},3.2))
    assert time.monotonic()-started < 3.6
    assert ready.is_set() and backend.metrics()[-1] == []


def test_redis_read_failure_after_spawn_stops_and_reaps_child():
    class FailingReads(ControlRedis):
        fail_reads = False
        def get(self, key):
            if self.fail_reads and key.startswith(PREFIX+'slot:'):
                raise redis.ConnectionError('secret must not be logged')
            return super().get(key)
    backend, context = FailingReads(), mp.get_context('spawn')
    ready = context.Event()
    executor = SourceExecutor('redis://offline', client=backend, worker=ignore_term_worker)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor.call, SourceCall('fake','ths',{'ready':ready},900))
        assert ready.wait(timeout=5)
        backend.fail_reads = True
        with pytest.raises(redis.ConnectionError):
            future.result(timeout=3)
        backend.fail_reads = False
    # Redis失效时不能安全删名额，依靠原30秒租约回收；源进程已经回收。
    assert not any(process.is_alive() for process in mp.active_children())
    backend.advance(30)
    assert backend.metrics()[-1] == []


def lost_parent_worker(queue, url, call, keys, token, guard, deadline, read, parent, started_event):
    import sys
    import app.runtime.source_execution as execution
    backend = call.parameters['backend']
    execution._client = lambda url: backend
    def function(**parameters):
        time.sleep(60)
    sys.modules['akshare'] = SimpleNamespace(fake=function)
    execution._source_worker(queue,url,call,keys,token,guard,deadline,read,-1,started_event)


def test_source_watchdog_stops_on_parent_identity_loss():
    context = mp.get_context('spawn')
    with RedisManager(ctx=context) as manager:
        backend = manager.ControlRedis()
        executor = SourceExecutor('redis://offline', client=backend, worker=lost_parent_worker)
        with pytest.raises(SourceControlError, match='源控制失效'):
            executor.call(SourceCall('fake','sina',{'backend':backend},20))
        assert backend.metrics()[-1] == []
        assert not any(process.is_alive() and process.pid != manager._process.pid for process in mp.active_children())


def http_frame_worker(queue, url, call, keys, token, guard, deadline, read, parent, started_event):
    backend = call.parameters['backend']
    points = []
    def send(session, request, **kwargs):
        points.append(time.monotonic())
        response = requests.Response()
        response.status_code, response._content = 200, b'{}'
        return response
    requests.Session.send = send
    with controlled_http(SourceControl(backend), call, keys, token, guard, deadline, read, started_event):
        for page in range(2):
            requests.Session().get(f'https://offline.test/page/{page}')
    queue.put(('ok',points))


def http_parent_entry(backend, barrier, queue, lane="quotes", group="sina"):
    barrier.wait(timeout=10)
    executor = SourceExecutor('redis://offline', client=backend, worker=http_frame_worker)
    function = 'stock_fund_flow_individual' if lane == 'funds' else 'fake'
    queue.put(executor.call(SourceCall(function,group,{'backend':backend,'symbol':'即时'},20,('offline.test',))))


def test_actual_http_start_gaps_across_independent_parents_and_source_children():
    context = mp.get_context('spawn')
    with RedisManager(ctx=context) as manager:
        backend = manager.ControlRedis()
        barrier, queue = context.Barrier(1), context.Queue()
        parents = [context.Process(target=http_parent_entry, args=(backend,barrier,queue)) for _ in range(3)]
        try:
            points = []
            for process in parents:
                process.start()
                points.extend(queue.get(timeout=15))
                process.join(timeout=3)
            points.sort()
            for process in parents:
                process.join(timeout=3)
                assert process.exitcode == 0
            assert len(points) == 6
            assert all(later-earlier >= .18 for earlier,later in zip(points,points[1:]))
            assert backend.metrics()[-1] == []
        finally:
            for process in parents:
                if process.is_alive():
                    process.kill()
                process.join(timeout=2)
            queue.close()


@pytest.mark.parametrize('category,status,kind', [
    ('NETWORK',None,'ConnectionError'),('TIMEOUT',None,'ReadTimeout'),
    ('FORMAT',None,'AttributeError'),('FORMAT',None,'ValueError'),
    ('HTTP_REJECTED',403,'HTTPError'),('HTTP_REJECTED',429,'HTTPError'),
    ('SOURCE_REJECTED',None,'RateLimitError'),
])
def test_market_child_and_parent_share_cooldown_scope_without_expansion(category,status,kind):
    import fakeredis
    from app.runtime.source_execution import market_cooldown
    client = fakeredis.FakeRedis(decode_responses=True)
    control = SourceControl(client)
    call = SourceCall('fake','ths',cooldown_keys=('shared','module'),cooldown_policy='market')
    metadata = {'http_status':status,'category':category,'exception_type':kind}
    control.cool(call,metadata)
    shared, seconds = market_cooldown(metadata)
    key, other = ('shared','module') if shared else ('module','shared')
    assert client.ttl(key) == seconds and client.get(other) is None
    client.expire(key,seconds-120)
    control.cool(call,metadata)  # 父层重写同键也不得续期/扩范围。
    assert client.ttl(key) == seconds-120 and client.get(other) is None


def test_ordinary_market_http_failure_does_not_block_sibling_module(monkeypatch):
    backend = ControlRedis()
    control = SourceControl(backend)
    keys = control.acquire('ths','first')
    call = SourceCall('fake','ths',domains=('offline.test',),cooldown_keys=('ths','industry'),cooldown_policy='market')
    other = SourceCall('fake','ths',domains=('offline.test',),cooldown_keys=('ths','concept'),cooldown_policy='market')
    sends = []
    def send(session,request,**kwargs):
        sends.append(request.url)
        if len(sends) == 1:
            raise requests.ReadTimeout('private raw request')
        response = requests.Response()
        response.status_code,response._content = 200,b'{}'
        return response
    monkeypatch.setattr(requests.Session,'send',send)
    with controlled_http(control,call,keys,'first',None,time.monotonic()+5,15):
        with pytest.raises(requests.ReadTimeout):
            requests.get('https://offline.test/industry')
        with pytest.raises(SourceCoolingError):
            requests.get('https://offline.test/industry/page/2')
    assert backend.get('ths') is None and backend.ttl('industry') == 300
    control.release(keys,'first')
    sibling = control.acquire('ths','second')
    with controlled_http(control,other,sibling,'second',None,time.monotonic()+5,15):
        assert requests.get('https://offline.test/concept').status_code == 200
    assert len(sends) == 2


def test_actual_http_gap_between_two_concurrent_ths_channels():
    context = mp.get_context('spawn')
    with RedisManager(ctx=context) as manager:
        backend = manager.ControlRedis()
        barrier,queue = context.Barrier(2),context.Queue()
        parents = [context.Process(target=http_parent_entry,args=(backend,barrier,queue,lane,'ths'))
                   for lane in ('quotes','funds')]
        try:
            for process in parents:
                process.start()
            points = sorted(point for _ in parents for point in queue.get(timeout=20))
            for process in parents:
                process.join(timeout=3)
                assert process.exitcode == 0
            assert len(points) == 4
            assert all(later-earlier >= .98 for earlier,later in zip(points,points[1:]))
            maximum,groups,releases,remaining = backend.metrics()
            assert maximum == 2 and groups == {'ths':2} and releases == 2 and remaining == []
        finally:
            for process in parents:
                if process.is_alive():
                    process.kill()
                process.join(timeout=2)
            queue.close()
