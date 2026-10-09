"""采集程序与健康接口的环境配置。"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, unquote, urlparse, urlunparse

from pydantic import SecretStr
from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    """运行配置；Redis URL 可能包含密码，不参与对象输出。"""

    service_host: str = "0.0.0.0"
    service_port: int = 8000
    service_log_level: str = "info"
    redis_url: SecretStr = field(
        default_factory=lambda: SecretStr("redis://localhost:6379/2"), repr=False
    )
    source_timeout_seconds: int = 15
    redis_lock_seconds: int = 240
    stock_monitor_xq_enabled: bool = False
    xueqiu_token: SecretStr = field(default_factory=lambda: SecretStr(""), repr=False)
    stock_monitor_internal_token: SecretStr = field(
        default_factory=lambda: SecretStr(""), repr=False
    )


def _integer(environ: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """本地默认 .env.dev；生产 .env.prod；环境变量覆盖文件值。"""
    if environ is None:
        environment = os.environ.get("APP_ENV", "dev")
        if environment not in {"dev", "prod"}:
            raise ValueError("APP_ENV 必须为 dev 或 prod")
        path = Path(__file__).resolve().parents[2] / f".env.{environment}"
        values = {key: value for key, value in dotenv_values(path, interpolate=False).items()
                  if value is not None}
        values.update(os.environ)
    else:
        values = environ
    parsed = urlparse(values.get("REDIS_URL", "redis://localhost").strip())
    redis_port = values.get("REDIS_PORT")
    if redis_port is None:
        redis_port = str(parsed.port or 6379)
    redis_db = values.get("REDIS_DB", parsed.path.removeprefix("/") or "2")
    password = values.get("REDIS_PASSWORD", unquote(parsed.password or ""))
    username = unquote(parsed.username or "")
    credentials = ""
    if username or password:
        credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    hostname = parsed.hostname or parsed.path or "localhost"
    if ":" in hostname:
        hostname = f"[{hostname}]"
    redis_url = urlunparse((
        parsed.scheme or "redis",
        f"{credentials}{hostname}:{redis_port}",
        f"/{redis_db}",
        "",
        parsed.query,
        "",
    ))
    port = _integer(values, "SERVICE_PORT", 8000)
    if port > 65535:
        raise ValueError("SERVICE_PORT 必须在 1-65535 范围内")
    level = values.get("SERVICE_LOG_LEVEL", "info").lower()
    if level not in {"critical", "error", "warning", "info", "debug"}:
        raise ValueError("SERVICE_LOG_LEVEL 不受支持")
    source_timeout = _integer(values, "SOURCE_TIMEOUT_SECONDS", 15)
    lock_seconds = int(values.get("REDIS_LOCK_SECONDS", "240"))
    xq_flag = values.get("STOCK_MONITOR_XQ_ENABLED", "false").strip().lower()
    if xq_flag not in {"true", "false", "1", "0"}:
        raise ValueError("STOCK_MONITOR_XQ_ENABLED 必须为 true 或 false")
    return Settings(
        service_host=values.get("SERVICE_HOST", "0.0.0.0"),
        service_port=port,
        service_log_level=level,
        redis_url=SecretStr(redis_url),
        source_timeout_seconds=source_timeout,
        redis_lock_seconds=lock_seconds,
        stock_monitor_xq_enabled=xq_flag in {"true", "1"},
        xueqiu_token=SecretStr(values.get("XUEQIU_TOKEN", "")),
        stock_monitor_internal_token=SecretStr(
            values.get("STOCK_MONITOR_INTERNAL_TOKEN", "")
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
