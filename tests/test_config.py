"""采集环境配置校验。"""

import pytest

from app.core.config import load_settings


def test_defaults_and_redis_secret():
    settings = load_settings({"REDIS_URL": "redis://:test-secret@localhost:6379/2"})
    assert settings.redis_url.get_secret_value().endswith("/2")
    assert "test-secret" not in repr(settings)


@pytest.mark.parametrize("value", ["http://localhost/2", "redis://localhost/0", "bad"])
def test_invalid_redis_url(value):
    with pytest.raises(ValueError, match="REDIS_URL"):
        load_settings({"REDIS_URL": value})


@pytest.mark.parametrize("key,value", [
    ("SOURCE_TIMEOUT_SECONDS", "0"),
    ("REDIS_LOCK_SECONDS", "-1"),
    ("SERVICE_PORT", "65536"),
    ("REDIS_LOCK_SECONDS", "100"),
])
def test_invalid_number(key, value):
    with pytest.raises(ValueError, match=key):
        load_settings({key: value})
