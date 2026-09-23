"""采集程序与健康接口的环境配置。"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Mapping
from urllib.parse import urlparse

from pydantic import SecretStr


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


def _integer(environ: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """从环境变量加载配置。"""
    values = os.environ if environ is None else environ
    redis_url = values.get("REDIS_URL", "redis://localhost:6379/2").strip()
    parsed = urlparse(redis_url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or parsed.path != "/2":
        raise ValueError("REDIS_URL 必须是指向 Redis DB 2 的 redis(s) URL")
    port = _integer(values, "SERVICE_PORT", 8000)
    if port > 65535:
        raise ValueError("SERVICE_PORT 必须在 1-65535 范围内")
    level = values.get("SERVICE_LOG_LEVEL", "info").lower()
    if level not in {"critical", "error", "warning", "info", "debug"}:
        raise ValueError("SERVICE_LOG_LEVEL 不受支持")
    source_timeout = _integer(values, "SOURCE_TIMEOUT_SECONDS", 15)
    lock_seconds = _integer(values, "REDIS_LOCK_SECONDS", 240)
    # 六个源调用（含交易日历）最多各尝试两次，锁必须覆盖整轮调用。
    if lock_seconds <= 12 * source_timeout + 30:
        raise ValueError("REDIS_LOCK_SECONDS 必须覆盖采集最大执行时间")
    return Settings(
        service_host=values.get("SERVICE_HOST", "0.0.0.0"),
        service_port=port,
        service_log_level=level,
        redis_url=SecretStr(redis_url),
        source_timeout_seconds=source_timeout,
        redis_lock_seconds=lock_seconds,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
