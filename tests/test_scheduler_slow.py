"""自动轮短任务优先，失效截止时间停止当前源任务。"""

import asyncio
import threading

import pytest
from app.core.config import load_settings
from tests.test_snapshot import FakeRedis


def test_round_order_short_tasks_first_with_one_parent(monkeypatch):
    import app.runtime.workflows as module
    calls = []
    for name in ('run_monitor','run_etf','_market'):
        monkeypatch.setattr(module,name,lambda *a,_name=name,**kw:calls.append(_name) or 'published')
    assert module.run_quotes(load_settings({}),FakeRedis()) == {
        'monitor':'published','etf':'published','market':'published'}
    assert calls == ['run_monitor','run_etf','_market']


def test_scheduler_shutdown_cancels_and_waits_current_round(monkeypatch):
    import app.runtime.scheduler as module
    entered,stopped = threading.Event(),threading.Event()
    def work(settings,cancel,*args):
        entered.set()
        assert cancel.wait(2)
        stopped.set()
    async def run():
        async def no_sleep(seconds):
            return None
        monkeypatch.setattr(module.asyncio,'sleep',no_sleep)
        monkeypatch.setattr(module,'MAX_START_LAG_SECONDS',86400)
        monkeypatch.setattr(module,'_round',work)
        task = asyncio.create_task(module.run_scheduler(load_settings({})))
        assert await asyncio.to_thread(entered.wait,1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert stopped.is_set()


def test_calendar_shutdown_cancels_and_waits_source(monkeypatch):
    from app.calendar.scheduler import _run_cancellable
    entered,stopped=threading.Event(),threading.Event()
    def work(settings,cancel,*args):
        entered.set()
        assert cancel.wait(2)
        stopped.set()
        from app.runtime.source_execution import SourceControlError
        raise SourceControlError('已取消源进程')
    async def run():
        task=asyncio.create_task(_run_cancellable(work,load_settings({})))
        assert await asyncio.to_thread(entered.wait,1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert stopped.is_set()
