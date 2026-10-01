"""手动任务子进程入口复用现有业务工作流。"""

import json

from app import job_worker


class FakeClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_worker_dispatches_each_workflow_and_writes_outcome(monkeypatch, tmp_path):
    client = FakeClient()
    calls = []
    monkeypatch.setattr(job_worker, "get_settings", lambda: type(
        "Settings", (), {"service_log_level": "info", "redis_url": type(
            "Secret", (), {"get_secret_value": lambda self: "redis://test"}
        )()}
    )())
    monkeypatch.setattr(job_worker.redis.Redis, "from_url", lambda *args, **kwargs: client)
    monkeypatch.setattr(job_worker, "run_calendar", lambda *args, **kwargs: calls.append(
        ("calendar", kwargs)) or "refreshed")
    monkeypatch.setattr(job_worker, "run_market", lambda *args, **kwargs: calls.append(
        ("market", kwargs)) or "partial")
    monkeypatch.setattr(job_worker, "run_monitor", lambda *args, **kwargs: calls.append(
        ("monitor", kwargs)) or "disabled")
    for kind, expected in (
        ("calendar", "refreshed"), ("market", "partial"), ("monitor", "disabled")
    ):
        result_path = tmp_path / f"{kind}.json"
        job_worker.run(kind, result_path)
        assert json.loads(result_path.read_text(encoding="utf-8")) == {"outcome": expected}
    assert calls == [
        ("calendar", {"manual": True}), ("market", {}), ("monitor", {})
    ]
    assert client.closed
