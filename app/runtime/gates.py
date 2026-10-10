"""行情、资金两个固定入口；租约原子获取，忙时拒绝且不排队。"""

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

QUOTES_ENTRY_KEY = 'stock:source-control:v1:entry:quotes'
FUNDS_ENTRY_KEY = 'stock:source-control:v1:entry:funds'
ENTRY_KEYS = {'quotes': (QUOTES_ENTRY_KEY,), 'funds': (FUNDS_ENTRY_KEY,),
              'market': (QUOTES_ENTRY_KEY, FUNDS_ENTRY_KEY)}
_ENTRY = ContextVar('collection_entries', default=())
RENEW_SECONDS = 10
ACQUIRE = """
for i=1,#KEYS do if redis.call('exists',KEYS[i]) == 1 then return 0 end end
for i=1,#KEYS do redis.call('set',KEYS[i],ARGV[1],'EX',ARGV[2]) end
return 1
"""
RENEW = """
for i=1,#KEYS do if redis.call('get',KEYS[i]) ~= ARGV[1] then return 0 end end
for i=1,#KEYS do redis.call('expire',KEYS[i],ARGV[2]) end
return 1
"""
RELEASE = """
for i=1,#KEYS do if redis.call('get',KEYS[i]) == ARGV[1] then redis.call('del',KEYS[i]) end end
return 1
"""


def check_entry():
    for entry in _ENTRY.get():
        if (entry['cancel'].is_set() or entry['failed'].is_set()
                or any(entry['client'].get(key) != entry['token'] for key in entry['keys'])):
            from app.runtime.source_execution import SourceControlError
            raise SourceControlError('采集入口已取消或锁失效')


def entry_guard(lane):
    key = ENTRY_KEYS[lane][0]
    for entry in _ENTRY.get():
        if key in entry['keys']:
            return key,entry['token']
    raise RuntimeError('当前线程没有对应采集入口')


@contextmanager
def collection_entry(client, cancel=None, *, lane='quotes', mode='auto'):
    if mode == 'manual':
        if cancel is not None and cancel.is_set():
            from app.runtime.source_execution import SourceControlError
            raise SourceControlError('采集已取消')
        # 保留 Redis 可用性检查，不占用自动任务入口。
        if not client.ping():
            from app.runtime.source_execution import SourceControlError
            raise SourceControlError("Redis 控制不可用")
        yield True
        return
    entries = _ENTRY.get()
    check_entry()
    owned = {key for entry in entries for key in entry['keys']}
    keys = tuple(key for key in ENTRY_KEYS[lane] if key not in owned)
    if not keys:
        yield True
        return
    token = uuid4().hex
    if not client.eval(ACQUIRE, len(keys), *keys, token, 30):
        yield False
        return
    stop, failed = threading.Event(), threading.Event()
    state = {'client': client, 'keys': keys, 'token': token,
             'cancel': cancel or threading.Event(), 'failed': failed}
    context_token = _ENTRY.set((*entries, state))

    def renew():
        while not stop.wait(RENEW_SECONDS):
            try:
                if not client.eval(RENEW, len(keys), *keys, token, 30):
                    failed.set()
                    return
            except Exception:
                failed.set()
                return

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        check_entry()
        yield True
    finally:
        stop.set()
        thread.join(timeout=1.2)
        _ENTRY.reset(context_token)
        client.eval(RELEASE, len(keys), *keys, token)
