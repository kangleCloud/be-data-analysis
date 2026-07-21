"""环境配置校验测试。"""

import pytest

from app.core.config import Settings, load_settings
from app.main import create_app


def test_config_defaults():
    assert load_settings({}) == Settings()


@pytest.mark.parametrize("value", ["zero", "0", "65536"])
def test_invalid_port_is_rejected(value):
    with pytest.raises(ValueError, match="SERVICE_PORT"):
        load_settings({"SERVICE_PORT": value})


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValueError, match="SERVICE_LOG_LEVEL"):
        load_settings({"SERVICE_LOG_LEVEL": "verbose"})


def test_invalid_provider_is_rejected_by_config():
    with pytest.raises(ValueError, match="DATA_PROVIDER"):
        load_settings({"DATA_PROVIDER": "unknown"})


def test_invalid_symbol_resolver_is_rejected_by_config():
    with pytest.raises(ValueError, match="SYMBOL_RESOLVER"):
        load_settings({"SYMBOL_RESOLVER": "unknown"})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("EXTERNAL_HTTP_TIMEOUT_SECONDS", "0"),
        ("EXTERNAL_HTTP_TIMEOUT_SECONDS", "slow"),
        ("EXTERNAL_HTTP_RETRY_COUNT", "-1"),
        ("SYMBOL_CACHE_TTL_SECONDS", "-1"),
    ],
)
def test_invalid_external_http_config_is_rejected(key, value):
    with pytest.raises(ValueError, match=key):
        load_settings({key: value})


def test_ab_sr_is_loaded_as_secret_without_repr_leak():
    secret_value = "test-sensitive-cookie"
    settings = load_settings({"BAIDU_AB_SR": secret_value})

    assert settings.baidu_ab_sr is not None
    assert settings.baidu_ab_sr.get_secret_value() == secret_value
    assert secret_value not in repr(settings)


def test_unknown_provider_prevents_app_startup():
    with pytest.raises(ValueError, match="不支持的数据源"):
        create_app(Settings(data_provider="unknown"))
