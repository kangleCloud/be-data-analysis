"""同步手动任务鉴权、并发拒绝与终态响应。"""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.jobs_api import JOB_TIMEOUT_SECONDS, LOCK_PREFIX, _run_default
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
    for kind in ("calendar", "market", "monitor"):
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
    }
    client = client_for(RedisClient(), lambda kind: outcomes[kind])
    for kind, state in (
        ("calendar", "SKIPPED"), ("market", "PARTIAL"), ("monitor", "SKIPPED")
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


def test_worker_process_result_is_read_and_timeout_is_bounded(monkeypatch):
    seen = []

    def fake_run(argv, *, timeout, check):
        seen.append((argv, timeout, check))
        assert argv[1:4] == ["-m", "app.job_worker", "market"]
        from pathlib import Path
        Path(argv[4]).write_text('{"outcome":"partial"}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("app.jobs_api.subprocess.run", fake_run)
    assert _run_default("market") == "partial"
    assert seen[0][1] == JOB_TIMEOUT_SECONDS["market"]


def test_worker_timeout_returns_failed_and_releases_api_lock(monkeypatch):
    def timeout(argv, *, timeout, check):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr("app.jobs_api.subprocess.run", timeout)
    redis_client = RedisClient()
    client = client_for(redis_client, _run_default)
    response = client.post("/internal/jobs/v1/market/refresh", headers=headers())
    assert response.status_code == 500
    assert response.json()["state"] == "FAILED"
    assert redis_client.get(f"{LOCK_PREFIX}market") is None
