"""FastAPI 路由和异常响应测试。"""

from datetime import date

from fastapi.testclient import TestClient

from app.models.domain import AssetType, PriceBar
from app.providers.base import AmbiguousSymbolError, ProviderError
from app.providers.mock import MockMarketDataProvider
from app.service.market_data import MarketDataService


def test_app_registers_public_routes(test_app):
    paths = set(test_app.openapi()["paths"])
    assert "/health" in paths
    assert "/api/v1/stocks/{symbol}/history" in paths
    assert "/api/v1/stocks/{symbol}/latest" in paths
    assert "/api/v1/stocks/history" in paths
    assert "/api/v1/stocks/latest" in paths
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
        "price_change",
        "change_percent",
        "turnover_rate",
        "pre_close",
        "ma5",
        "ma5_volume",
        "ma10",
        "ma10_volume",
        "ma20",
        "ma20_volume",
    }


def test_stock_history_post_resolves_fourtech_name(client):
    response = client.post(
        "/api/v1/stocks/history",
        json={
            "name": "四方科技",
            "start_date": "2024-01-02",
            "end_date": "2024-01-03",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["symbol"] == "603339"
    assert data["name"] == "四方科技"
    assert [item["trade_date"] for item in data["items"]] == [
        "2024-01-02",
        "2024-01-03",
    ]


def test_stock_latest_supports_get_and_post(client):
    get_response = client.get("/api/v1/stocks/603339/latest")
    post_response = client.post(
        "/api/v1/stocks/latest",
        json={"name": "四方科技"},
    )

    assert get_response.status_code == 200
    assert get_response.json()["data"]["item"]["trade_date"] == "2024-01-04"
    assert post_response.status_code == 200
    assert post_response.json()["data"]["symbol"] == "603339"
    assert post_response.json()["data"]["name"] == "四方科技"


def test_stock_lookup_requires_exactly_one_identifier(client):
    neither = client.post(
        "/api/v1/stocks/latest",
        json={},
    )
    both = client.post(
        "/api/v1/stocks/latest",
        json={"name": "四方科技", "symbol": "603339"},
    )

    assert neither.status_code == 400
    assert both.status_code == 400
    assert neither.json()["msg"] == "请求参数非法"


def test_unknown_stock_name_returns_404(client):
    response = client.post(
        "/api/v1/stocks/latest",
        json={"name": "不存在的股票"},
    )

    assert response.status_code == 404
    assert response.json() == {"code": 404, "msg": "未找到匹配的 A 股", "data": None}


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


class _EmptyProvider:
    name = "empty"
    is_mock = False

    def fetch_history(self, asset_type, symbol, start_date, end_date):
        return ()


class _FailingResolver:
    name = "failing-resolver"

    def resolve(self, name):
        raise ProviderError("resolver internal detail")


class _AmbiguousResolver:
    name = "ambiguous-resolver"

    def resolve(self, name):
        raise AmbiguousSymbolError(name)


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


def test_latest_empty_history_returns_404(test_app):
    test_app.state.market_data_service = MarketDataService(_EmptyProvider())

    with TestClient(test_app) as client:
        response = client.get("/api/v1/stocks/603339/latest")

    assert response.status_code == 404
    assert response.json()["msg"] == "未找到最新日线行情"


def test_name_resolver_failures_use_stable_error_responses(test_app):
    test_app.state.market_data_service = MarketDataService(
        MockMarketDataProvider(),
        _FailingResolver(),
    )
    with TestClient(test_app) as client:
        unavailable = client.post(
            "/api/v1/stocks/latest",
            json={"name": "四方科技"},
        )

    test_app.state.market_data_service = MarketDataService(
        MockMarketDataProvider(),
        _AmbiguousResolver(),
    )
    with TestClient(test_app) as client:
        ambiguous = client.post(
            "/api/v1/stocks/latest",
            json={"name": "同名股票"},
        )

    assert unavailable.status_code == 502
    assert unavailable.json()["msg"] == "数据源暂时不可用"
    assert "internal" not in unavailable.text
    assert ambiguous.status_code == 409
    assert ambiguous.json()["msg"] == "股票名称对应多个 A 股代码"
