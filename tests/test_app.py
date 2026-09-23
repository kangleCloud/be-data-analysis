"""只保留健康接口。"""

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_only():
    client = TestClient(create_app())
    assert client.get("/health").json() == {
        "code": 200, "msg": "服务正常", "data": {"status": "ok"}
    }
    assert client.get("/api/v1/stocks/600000/latest").status_code == 404
    assert set(create_app().openapi()["paths"]) == {"/health"}
