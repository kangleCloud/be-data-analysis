"""FastAPI 路由和异常响应测试。"""

from datetime import date

from fastapi.testclient import TestClient

from app.models.domain import AssetType, PriceBar
from app.providers.base import ProviderError
from app.service.market_data import MarketDataService


def test_app_registers_public_routes(test_app):
    paths = set(test_app.openapi()["paths"])
    assert "/health" in paths
    assert "/api/v1/stocks/{symbol}/history" in paths
    assert "/api/v1/funds/{symbol}/history" in paths


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "code": 200,
        "msg": "服务正常",
        "data": {"status": "ok"},
    }


def test_stock_history_returns_standardized_mock_data(client):
    response = client.get(
        "/api/v1/stocks/600000/history",
        params={"start_date": "2024-01-02", "end_date": "2024-01-04"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    assert payload["msg"] == "行情查询成功"
    assert payload["data"]["asset_type"] == "stock"
    assert payload["data"]["symbol"] == "600000"
    assert payload["data"]["provider"] == "mock"
    assert payload["data"]["mock_data"] is True
    assert [item["trade_date"] for item in payload["data"]["items"]] == [
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    assert set(payload["data"]["items"][0]) == {
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    }


def test_fund_history_filters_date_range(client):
    response = client.get(
        "/api/v1/funds/510300/history",
        params={"start_date": "2024-01-03", "end_date": "2024-01-03"},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["asset_type"] == "fund"
    assert [item["trade_date"] for item in payload["items"]] == ["2024-01-03"]


def test_empty_history_is_success(client):
    response = client.get(
        "/api/v1/stocks/600000/history",
        params={"start_date": "2025-01-01", "end_date": "2025-01-02"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_invalid_symbol_returns_400(client):
    response = client.get(
        "/api/v1/stocks/6000/history",
        params={"start_date": "2024-01-02", "end_date": "2024-01-04"},
    )

    assert response.status_code == 400
    assert response.json() == {"code": 400, "msg": "请求参数非法", "data": None}


def test_invalid_date_returns_400(client):
    response = client.get(
        "/api/v1/stocks/600000/history",
        params={"start_date": "not-a-date", "end_date": "2024-01-04"},
    )

    assert response.status_code == 400
    assert response.json()["msg"] == "请求参数非法"


def test_reversed_date_range_returns_400(client):
    response = client.get(
        "/api/v1/stocks/600000/history",
        params={"start_date": "2024-01-04", "end_date": "2024-01-02"},
    )

    assert response.status_code == 400
    assert response.json()["msg"] == "开始日期不能晚于结束日期"


class _FailingProvider:
    name = "failing"
    is_mock = False

    def fetch_history(self, asset_type, symbol, start_date, end_date):
        raise ProviderError("upstream unavailable")


class _UnexpectedProvider:
    name = "unexpected"
    is_mock = False

    def fetch_history(self, asset_type, symbol, start_date, end_date):
        raise RuntimeError("sensitive internal detail")


def test_provider_failure_returns_502(test_app):
    test_app.state.market_data_service = MarketDataService(_FailingProvider())

    with TestClient(test_app) as client:
        response = client.get(
            "/api/v1/stocks/600000/history",
            params={"start_date": "2024-01-02", "end_date": "2024-01-04"},
        )

    assert response.status_code == 502
    assert response.json() == {"code": 502, "msg": "数据源暂时不可用", "data": None}


def test_unexpected_failure_hides_details(test_app):
    test_app.state.market_data_service = MarketDataService(_UnexpectedProvider())

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/funds/510300/history",
            params={"start_date": "2024-01-02", "end_date": "2024-01-04"},
        )

    assert response.status_code == 500
    assert response.json() == {"code": 500, "msg": "服务内部错误", "data": None}
    assert "sensitive" not in response.text
