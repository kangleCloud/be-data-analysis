"""应用环境配置加载模块。"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Mapping
from urllib.parse import urlparse

from pydantic import SecretStr

DEFAULT_SERVICE_HOST = "0.0.0.0"
DEFAULT_SERVICE_PORT = 8000
DEFAULT_SERVICE_LOG_LEVEL = "info"
DEFAULT_DATA_PROVIDER = "baidu_finance"
DEFAULT_SYMBOL_RESOLVER = "eastmoney"
DEFAULT_BAIDU_FINANCE_BASE_URL = "https://finance.pae.baidu.com"
DEFAULT_EASTMONEY_SEARCH_BASE_URL = "https://searchapi.eastmoney.com"
DEFAULT_EXTERNAL_HTTP_TIMEOUT_SECONDS = 10.0
DEFAULT_EXTERNAL_HTTP_RETRY_COUNT = 2
DEFAULT_SYMBOL_CACHE_TTL_SECONDS = 3600
VALID_LOG_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}
VALID_DATA_PROVIDERS = {"baidu_finance", "mock"}
VALID_SYMBOL_RESOLVERS = {"eastmoney", "mock"}


@dataclass(frozen=True)
class Settings:
    """服务启动及数据源配置。"""

    service_host: str = DEFAULT_SERVICE_HOST
    service_port: int = DEFAULT_SERVICE_PORT
    service_log_level: str = DEFAULT_SERVICE_LOG_LEVEL
    data_provider: str = DEFAULT_DATA_PROVIDER
    symbol_resolver: str = DEFAULT_SYMBOL_RESOLVER
    baidu_finance_base_url: str = DEFAULT_BAIDU_FINANCE_BASE_URL
    eastmoney_search_base_url: str = DEFAULT_EASTMONEY_SEARCH_BASE_URL
    external_http_timeout_seconds: float = DEFAULT_EXTERNAL_HTTP_TIMEOUT_SECONDS
    external_http_retry_count: int = DEFAULT_EXTERNAL_HTTP_RETRY_COUNT
    symbol_cache_ttl_seconds: int = DEFAULT_SYMBOL_CACHE_TTL_SECONDS
    baidu_ab_sr: SecretStr | None = field(default=None, repr=False)


def _optional_string(environ: Mapping[str, str], key: str, default: str) -> str:
    value = (environ.get(key) or "").strip()
    return value or default


def _load_port(environ: Mapping[str, str]) -> int:
    raw_value = _optional_string(environ, "SERVICE_PORT", str(DEFAULT_SERVICE_PORT))
    try:
        port = int(raw_value)
    except ValueError as exc:
        raise ValueError("SERVICE_PORT 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SERVICE_PORT 必须在 1-65535 范围内")
    return port


def _load_choice(
    environ: Mapping[str, str],
    key: str,
    default: str,
    choices: set[str],
) -> str:
    value = _optional_string(environ, key, default).lower()
    if value not in choices:
        supported = ", ".join(sorted(choices))
        raise ValueError(f"{key} 只支持 {supported}")
    return value


def _load_url(environ: Mapping[str, str], key: str, default: str) -> str:
    value = _optional_string(environ, key, default).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{key} 必须是有效的 HTTP(S) 地址")
    return value


def _load_positive_float(
    environ: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    raw_value = _optional_string(environ, key, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是数字") from exc
    if value <= 0:
        raise ValueError(f"{key} 必须大于 0")
    return value


def _load_non_negative_int(
    environ: Mapping[str, str],
    key: str,
    default: int,
) -> int:
    raw_value = _optional_string(environ, key, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是整数") from exc
    if value < 0:
        raise ValueError(f"{key} 不能小于 0")
    return value


def _load_optional_secret(
    environ: Mapping[str, str],
    key: str,
) -> SecretStr | None:
    value = (environ.get(key) or "").strip()
    return SecretStr(value) if value else None


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """从指定映射或进程环境变量加载配置。"""
    raw_environ = environ if environ is not None else os.environ
    return Settings(
        service_host=_optional_string(raw_environ, "SERVICE_HOST", DEFAULT_SERVICE_HOST),
        service_port=_load_port(raw_environ),
        service_log_level=_load_choice(
            raw_environ,
            "SERVICE_LOG_LEVEL",
            DEFAULT_SERVICE_LOG_LEVEL,
            VALID_LOG_LEVELS,
        ),
        data_provider=_load_choice(
            raw_environ,
            "DATA_PROVIDER",
            DEFAULT_DATA_PROVIDER,
            VALID_DATA_PROVIDERS,
        ),
        symbol_resolver=_load_choice(
            raw_environ,
            "SYMBOL_RESOLVER",
            DEFAULT_SYMBOL_RESOLVER,
            VALID_SYMBOL_RESOLVERS,
        ),
        baidu_finance_base_url=_load_url(
            raw_environ,
            "BAIDU_FINANCE_BASE_URL",
            DEFAULT_BAIDU_FINANCE_BASE_URL,
        ),
        eastmoney_search_base_url=_load_url(
            raw_environ,
            "EASTMONEY_SEARCH_BASE_URL",
            DEFAULT_EASTMONEY_SEARCH_BASE_URL,
        ),
        external_http_timeout_seconds=_load_positive_float(
            raw_environ,
            "EXTERNAL_HTTP_TIMEOUT_SECONDS",
            DEFAULT_EXTERNAL_HTTP_TIMEOUT_SECONDS,
        ),
        external_http_retry_count=_load_non_negative_int(
            raw_environ,
            "EXTERNAL_HTTP_RETRY_COUNT",
            DEFAULT_EXTERNAL_HTTP_RETRY_COUNT,
        ),
        symbol_cache_ttl_seconds=_load_non_negative_int(
            raw_environ,
            "SYMBOL_CACHE_TTL_SECONDS",
            DEFAULT_SYMBOL_CACHE_TTL_SECONDS,
        ),
        baidu_ab_sr=_load_optional_secret(raw_environ, "BAIDU_AB_SR"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的应用配置。"""
    return load_settings()
