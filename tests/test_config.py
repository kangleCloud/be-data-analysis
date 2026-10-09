"""采集环境配置校验。"""

import pytest
from urllib.parse import unquote, urlparse

from app.core.config import load_settings


def test_defaults_and_redis_secret():
    settings = load_settings({"REDIS_URL": "redis://:test-secret@localhost:6379/2"})
    assert settings.redis_url.get_secret_value().endswith("/2")
    assert "test-secret" not in repr(settings)


def test_separate_redis_fields_build_db2_url():
    settings = load_settings({
        "REDIS_URL": "redis://cache.example",
        "REDIS_PASSWORD": "a@b:c",
        "REDIS_PORT": "6380",
        "REDIS_DB": "2",
    })
    url = settings.redis_url.get_secret_value()
    parsed = urlparse(url)
    assert (parsed.hostname, parsed.port, parsed.path) == ("cache.example", 6380, "/2")
    assert unquote(parsed.password) == "a@b:c"
    assert "a@b:c" not in repr(settings)


def test_redis_values_are_not_validated():
    settings = load_settings({
        "REDIS_URL": "http://localhost/0",
        "REDIS_PORT": "70000",
        "REDIS_DB": "0",
        "REDIS_LOCK_SECONDS": "100",
    })
    assert settings.redis_url.get_secret_value() == "http://localhost:70000/0"
    assert settings.redis_lock_seconds == 100


def test_stock_monitor_is_disabled_by_default_and_tokens_are_secret():
    defaults = load_settings({})
    assert not defaults.stock_monitor_xq_enabled
    settings = load_settings({
        "STOCK_MONITOR_XQ_ENABLED": "true",
        "XUEQIU_TOKEN": "private-xq-token",
        "STOCK_MONITOR_INTERNAL_TOKEN": "private-service-token",
    })
    assert settings.stock_monitor_xq_enabled
    assert settings.xueqiu_token.get_secret_value() == "private-xq-token"
    assert "private-xq-token" not in repr(settings)
    assert "private-service-token" not in repr(settings)


@pytest.mark.parametrize("key,value", [
    ("SOURCE_TIMEOUT_SECONDS", "0"),
    ("SERVICE_PORT", "65536"),
])
def test_invalid_number(key, value):
    with pytest.raises(ValueError, match=key):
        load_settings({key: value})


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_environment_file_selection_and_environment_override(monkeypatch, environment):
    paths = []
    def fake_values(path, **kwargs):
        paths.append(path.name)
        assert kwargs == {"interpolate": False}
        return {"REDIS_URL": "file-cache", "REDIS_PORT": "6379", "REDIS_DB": "2",
                "REDIS_PASSWORD": "literal-$password"}
    monkeypatch.setattr("app.core.config.dotenv_values", fake_values)
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("REDIS_URL", "runtime-cache")
    for key in ("REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    parsed = urlparse(load_settings().redis_url.get_secret_value())
    assert paths == [f".env.{environment}"]
    assert parsed.hostname == "runtime-cache"
    assert unquote(parsed.password) == "literal-$password"
