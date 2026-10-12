"""源进程异常退出分类与回收，不根据SIGKILL推断系统原因。"""

import multiprocessing as mp
import os
import signal

import pytest

from app.runtime.resources import SourceResourceError
from app.runtime.source_execution import SourceCall,SourceExecutor
from tests.test_source_execution import ControlRedis


def exit_worker(queue,url,call,keys,token,guard,deadline,read,parent,started):
    os.kill(os.getpid(),signal.SIGKILL)


def test_sigkill_is_resource_exit_not_proven_oom(caplog):
    backend=ControlRedis()
    source=SourceExecutor('redis://offline',client=backend,worker=exit_worker)
    with pytest.raises(SourceResourceError) as exc:
        source.call(SourceCall('fake','ths',budget_seconds=10,cooldown_keys=('cool',),cooldown_policy='market'))
    assert exc.value.exitcode==-9 and exc.value.reason=='PROCESS_EXIT'
    assert backend.get('cool') is None and backend.metrics()[-1]==[]


def code_exit_worker(queue,url,call,keys,token,guard,deadline,read,parent,started):
    os._exit(call.parameters['code'])


@pytest.mark.parametrize('code', [0,72])
def test_process_exit_without_result_is_classified_by_exitcode_and_reaped(code):
    backend=ControlRedis()
    source=SourceExecutor('redis://offline',client=backend,worker=code_exit_worker)
    with pytest.raises(SourceResourceError) as exc:
        source.call(SourceCall('fake','sina',{'code':code},budget_seconds=10))
    assert exc.value.reason=='PROCESS_EXIT' and exc.value.exitcode==code
    assert not hasattr(exc.value,'state')
    assert backend.metrics()[-1]==[] and not any(child.is_alive() for child in mp.active_children())
