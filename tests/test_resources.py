"""cgroup总内存与源进程资源退出，不推断未取得的OOM。"""

import multiprocessing as mp
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.runtime.resources import MEMORY_LIMIT_BYTES,SourceResourceError,check_memory,memory_state
from app.runtime.source_execution import SourceCall,SourceExecutor
from tests.test_source_execution import ControlRedis,ignore_term_worker


def exit_worker(queue,url,call,keys,token,guard,deadline,read,parent,started):
    os.kill(os.getpid(),signal.SIGKILL)


def test_cgroup_v2_and_missing_counters(tmp_path):
    assert memory_state(tmp_path)=={'currentBytes':None,'peakBytes':None,'oomKill':None}
    (tmp_path/'memory.current').write_text(str(MEMORY_LIMIT_BYTES))
    (tmp_path/'memory.peak').write_text(str(MEMORY_LIMIT_BYTES+1))
    (tmp_path/'memory.events').write_text('oom 2\noom_kill 1\n')
    state = memory_state(tmp_path)
    assert state == {'currentBytes':MEMORY_LIMIT_BYTES,'peakBytes':MEMORY_LIMIT_BYTES+1,'oomKill':1}
    with pytest.raises(SourceResourceError):
        check_memory(lambda:state)
    check_memory(lambda:{'currentBytes':MEMORY_LIMIT_BYTES-1})


def test_cgroup_v1_usage_is_total_memory(tmp_path):
    (tmp_path/'memory').mkdir()
    (tmp_path/'memory/memory.usage_in_bytes').write_text('123')
    (tmp_path/'memory/memory.max_usage_in_bytes').write_text('456')
    assert memory_state(tmp_path)=={'currentBytes':123,'peakBytes':456,'oomKill':None}


def test_pressure_stops_admission_without_source_cooldown(monkeypatch):
    backend=ControlRedis()
    monkeypatch.setattr('app.runtime.resources.memory_state',lambda:{'currentBytes':MEMORY_LIMIT_BYTES})
    source=SourceExecutor('redis://offline',client=backend,worker=exit_worker)
    with pytest.raises(SourceResourceError):
        source.call(SourceCall('fake','ths',cooldown_keys=('cool',),cooldown_policy='market'))
    assert backend.max_global==0 and backend.get('cool') is None


def test_pressure_reaps_current_term_ignoring_child_and_releases(monkeypatch):
    backend,ready=ControlRedis(),mp.get_context('spawn').Event()
    high=threading.Event()
    monkeypatch.setattr('app.runtime.resources.memory_state',lambda:{'currentBytes':MEMORY_LIMIT_BYTES if high.is_set() else 0})
    source=SourceExecutor('redis://offline',client=backend,worker=ignore_term_worker)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(source.call,SourceCall('fake','ths',{'ready':ready},900,cooldown_keys=('cool',),cooldown_policy='market'))
        assert ready.wait(5)
        high.set()
        with pytest.raises(SourceResourceError):
            pending.result(timeout=4)
    assert backend.metrics()[-1]==[] and backend.get('cool') is None
    assert not any(child.is_alive() for child in mp.active_children())


def test_sigkill_is_resource_exit_not_proven_oom(caplog):
    backend=ControlRedis()
    source=SourceExecutor('redis://offline',client=backend,worker=exit_worker)
    with pytest.raises(SourceResourceError) as exc:
        source.call(SourceCall('fake','ths',budget_seconds=10,cooldown_keys=('cool',),cooldown_policy='market'))
    assert exc.value.exitcode==-9 and exc.value.reason=='PROCESS_EXIT'
    assert exc.value.state['oomKill'] == memory_state()['oomKill']
    assert backend.get('cool') is None and backend.metrics()[-1]==[]
