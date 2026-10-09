"""跨自动采集、CLI、内部同步接口的单入口；忙时立即拒绝，不排队。"""

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

ENTRY_KEY = 'stock:source-control:v1:entry'
_ENTRY = ContextVar('collection_entry', default=None)
RENEW = """
if redis.call('get',KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('expire',KEYS[1],ARGV[2])
"""
RELEASE = """
if redis.call('get',KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('del',KEYS[1])
"""


def check_entry():
    entry = _ENTRY.get()
    if entry and (entry['cancel'].is_set() or entry['failed'].is_set()
                  or entry['client'].get(ENTRY_KEY) != entry['token']):
        from app.source_execution import SourceControlError
        raise SourceControlError('采集入口已取消或锁失效')


@contextmanager
def collection_entry(client, cancel=None):
    if _ENTRY.get() is not None:
        check_entry()
        yield True
        return
    token = uuid4().hex
    if not client.set(ENTRY_KEY,token,nx=True,ex=30):
        yield False
        return
    stop, failed = threading.Event(), threading.Event()
    state = {'client':client,'token':token,'cancel':cancel or threading.Event(),'failed':failed}
    context_token = _ENTRY.set(state)
    def renew():
        while not stop.wait(10):
            try:
                if not client.eval(RENEW,1,ENTRY_KEY,token,30):
                    failed.set()
                    return
            except Exception:
                failed.set()
                return
    thread = threading.Thread(target=renew,daemon=True)
    thread.start()
    try:
        check_entry()
        yield True
    finally:
        stop.set()
        thread.join(timeout=1.2)
        _ENTRY.reset(context_token)
        client.eval(RELEASE,1,ENTRY_KEY,token)
