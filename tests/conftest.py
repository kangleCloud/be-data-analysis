"""pytest 公共 fixture。"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        service_host="127.0.0.1",
        service_port=8000,
        service_log_level="debug",
        data_provider="mock",
    )


@pytest.fixture
def test_app(settings):
    return create_app(settings)


@pytest.fixture
def client(test_app):
    return TestClient(test_app)
