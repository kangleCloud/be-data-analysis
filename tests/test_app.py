"""健康接口与受保护的个股监控内部接口。"""

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_and_internal_routes():
    client = TestClient(create_app(scheduler_enabled=False))
    assert client.get("/health").json() == {
        "code": 200, "msg": "服务正常", "data": {"status": "ok"}
    }
    assert client.get("/api/v1/stocks/600000/latest").status_code == 404
    assert set(create_app(scheduler_enabled=False).openapi()["paths"]) == {
        "/health", "/internal/stock-monitor/v1/exchange-dictionary",
        "/internal/stock-monitor/v1/profiles",
        "/internal/jobs/v1/{kind}/refresh",
        "/internal/etf-monitor/v1/dictionary",
        "/internal/etf-monitor/v1/profiles",
        "/internal/etf-monitor/v1/asset-allocation",
    }
