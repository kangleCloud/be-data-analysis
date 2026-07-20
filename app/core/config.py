"""应用环境配置加载模块。"""

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

DEFAULT_SERVICE_HOST = "0.0.0.0"
DEFAULT_SERVICE_PORT = 8000
DEFAULT_SERVICE_LOG_LEVEL = "info"
DEFAULT_DATA_PROVIDER = "mock"
VALID_LOG_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}
VALID_DATA_PROVIDERS = {"mock"}


@dataclass(frozen=True)
class Settings:
    """服务启动及数据源配置。"""

    service_host: str = DEFAULT_SERVICE_HOST
    service_port: int = DEFAULT_SERVICE_PORT
    service_log_level: str = DEFAULT_SERVICE_LOG_LEVEL
    data_provider: str = DEFAULT_DATA_PROVIDER


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
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内缓存的应用配置。"""
    return load_settings()
