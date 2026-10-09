"""健康接口与受保护的个股监控内部接口。"""

import pytest
import redis

from fastapi.testclient import TestClient
from tests.test_snapshot import FakeRedis

from app.main import create_app


def test_health_and_internal_routes():
    client = TestClient(create_app(scheduler_enabled=False, redis_factory=FakeRedis))
    assert client.get("/health").json() == {
        "code": 200, "msg": "服务正常", "data": {"status": "ok"}
    }
    assert client.get("/api/v1/stocks/600000/latest").status_code == 404
    assert set(create_app(scheduler_enabled=False, redis_factory=FakeRedis).openapi()["paths"]) == {
        "/health", "/internal/stock-monitor/v1/exchange-dictionary",
        "/internal/stock-monitor/v1/profiles",
        "/internal/jobs/v1/{kind}/refresh",
        "/internal/etf-monitor/v1/dictionary",
        "/internal/etf-monitor/v1/profiles",
        "/internal/etf-monitor/v1/asset-allocation",
    }


@pytest.mark.parametrize("error,category", [
    (redis.ConnectionError("redis://:private-password@private-host"), "CONNECTION"),
    (redis.AuthenticationError("private-password"), "AUTHENTICATION"),
    (redis.TimeoutError("private-password"), "TIMEOUT"),
])
def test_health_failure_is_503_without_secrets_or_traceback(caplog, error, category):
    class FailedRedis(FakeRedis):
        closed = False
        def ping(self):
            raise error
        def close(self):
            self.closed = True
    backend = FailedRedis()
    client = TestClient(create_app(scheduler_enabled=False, redis_factory=lambda: backend))
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["data"]["status"] == "unavailable"
    assert backend.closed
    assert category in caplog.text
    assert "private-password" not in caplog.text + response.text
    assert all(record.exc_info is None for record in caplog.records)


def test_health_uses_one_second_ping_without_retry_or_source_calls(monkeypatch):
    calls = []
    def factory(url, **kwargs):
        calls.append(kwargs)
        return FakeRedis()
    monkeypatch.setattr("app.main.redis.Redis.from_url", factory)
    def forbidden():
        pytest.fail("健康检查不得构造行情源")
    client = TestClient(create_app(scheduler_enabled=False, exchange_factory=forbidden,
                                  etf_factory=forbidden, xq_factory=forbidden))
    assert client.get("/health").status_code == 200
    assert len(calls) == 1
    options = calls[0]
    assert options["socket_timeout"] == options["socket_connect_timeout"] == 1
    assert options["retry_on_timeout"] is False
    assert options["retry"]._retries == 0
