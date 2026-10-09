"""同步手动任务鉴权、并发拒绝与终态响应。"""

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.jobs_api import LOCK_PREFIX, _run_default
from app.main import create_app
from tests.test_snapshot import FakeRedis


class RedisClient(FakeRedis):
    def close(self):
        pass


def client_for(redis_client, runner):
    app = create_app(
        scheduler_enabled=False,
        settings=load_settings({"STOCK_MONITOR_INTERNAL_TOKEN": "test-internal"}),
        redis_factory=lambda: redis_client,
        job_runner=runner,
    )
    return TestClient(app)


def headers():
    return {"X-Internal-Token": "test-internal"}


def test_all_jobs_require_existing_internal_token_and_have_no_status_route():
    client = client_for(RedisClient(), lambda _kind: "published")
    for kind in ("calendar", "market", "monitor", "etf"):
        assert client.post(f"/internal/jobs/v1/{kind}/refresh").status_code == 401
        assert client.get(f"/internal/jobs/v1/{kind}/status", headers=headers()).status_code == 404


def test_post_waits_for_result_and_overlap_returns_409_without_queuing():
    gate = threading.Event()
    started = threading.Event()
    calls = []

    def runner(kind):
        calls.append(kind)
        started.set()
        assert gate.wait(2)
        return "published"

    client = client_for(RedisClient(), runner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(client.post, "/internal/jobs/v1/market/refresh", headers=headers())
        assert started.wait(1)
        assert not pending.done()
        overlap = client.post("/internal/jobs/v1/market/refresh", headers=headers())
        assert overlap.status_code == 409
        assert overlap.json()["state"] == "SKIPPED"
        assert overlap.json()["outcome"] == "locked"
        assert calls == ["market"]
        gate.set()
        response = pending.result(timeout=2)
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "kind", "state", "outcome", "startedAt", "finishedAt", "message"
    }
    assert payload["kind"] == "market" and payload["state"] == "SUCCEEDED"
    assert payload["outcome"] == "published"
    assert payload["startedAt"] and payload["finishedAt"]


def test_business_outcomes_are_distinct_terminal_states():
    outcomes = {
        "calendar": "throttled", "market": "partial", "monitor": "disabled",
        "etf": "published",
    }
    client = client_for(RedisClient(), lambda kind: outcomes[kind])
    for kind, state in (
        ("calendar", "SKIPPED"), ("market", "PARTIAL"),
        ("monitor", "SKIPPED"), ("etf", "SUCCEEDED")
    ):
        response = client.post(f"/internal/jobs/v1/{kind}/refresh", headers=headers())
        assert response.status_code == 200
        assert response.json()["state"] == state
        assert response.json()["outcome"] == outcomes[kind]


def test_business_failure_and_worker_crash_are_not_success():
    failed = client_for(RedisClient(), lambda _kind: "failed")
    response = failed.post("/internal/jobs/v1/calendar/refresh", headers=headers())
    assert response.status_code == 200
    assert response.json()["state"] == "FAILED"

    def crash(_kind):
        raise RuntimeError("secret credential")

    crashed = client_for(RedisClient(), crash)
    response = crashed.post("/internal/jobs/v1/calendar/refresh", headers=headers())
    assert response.status_code == 500
    assert response.json()["state"] == "FAILED"
    assert "secret credential" not in response.json()["message"]


def test_lock_redis_failure_is_503_and_does_not_run():
    class BrokenRedis(RedisClient):
        def set(self, *args, **kwargs):
            raise ConnectionError("redis unavailable")

    calls = []
    client = client_for(BrokenRedis(), lambda kind: calls.append(kind))
    response = client.post("/internal/jobs/v1/market/refresh", headers=headers())
    assert response.status_code == 503
    assert response.json()["state"] == "FAILED"
    assert calls == []



def test_default_dispatch_reuses_workflows_in_service_parent(monkeypatch):
    import app.jobs_api as module
    calls = []
    for key in ('calendar','market','monitor','etf'):
        monkeypatch.setattr(module,'run_'+key,lambda *args,_key=key,**kwargs:calls.append((_key,args,kwargs)) or 'published')
    settings,client = load_settings({}),RedisClient()
    for key in ('calendar','market','monitor','etf'):
        assert _run_default(key,settings,client) == 'published'
    assert [call[0] for call in calls] == ['calendar','market','monitor','etf']
    assert all(call[1] == (settings,client) for call in calls)
    assert calls[0][2] == {'manual':True}


def test_resource_failure_returns_503_and_releases_all_locks():
    from app.resources import SourceResourceError
    from app.collection_gate import ENTRY_KEY
    backend = RedisClient()
    def fail(kind):
        raise SourceResourceError('PROCESS_EXIT',exitcode=-9)
    response = client_for(backend,fail).post('/internal/jobs/v1/market/refresh',headers=headers())
    assert response.status_code == 503
    assert response.json()['outcome'] == 'resource' and response.json()['state'] == 'FAILED'
    assert backend.get(LOCK_PREFIX+'market') is None and backend.get(ENTRY_KEY) is None


def test_cross_kind_refresh_busy_immediately():
    from app.collection_gate import ENTRY_KEY
    backend,calls = RedisClient(),[]
    backend.set(ENTRY_KEY,'another-kind',ex=30)
    response = client_for(backend,lambda kind:calls.append(kind)).post('/internal/jobs/v1/etf/refresh',headers=headers())
    assert response.status_code == 409 and response.json()['outcome'] == 'locked'
    assert calls == []
