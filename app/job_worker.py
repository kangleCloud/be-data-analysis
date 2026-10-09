"""内部任务的独立进程入口，隔离市场源的主线程信号超时。"""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Literal

import redis

from app.core.config import get_settings
from app.core.logging import configure_logging, log_failure
from app.workflows import run_calendar, run_etf, run_market, run_monitor

Kind = Literal["calendar", "market", "monitor", "etf"]


def run(kind: Kind, result_path: Path) -> None:
    settings = get_settings()
    configure_logging(settings.service_log_level, kind)
    started = time.monotonic()
    client = None
    try:
        client = redis.Redis.from_url(
            settings.redis_url.get_secret_value(), decode_responses=True,
            socket_timeout=5, socket_connect_timeout=5,
        )
        if kind == "calendar":
            outcome = run_calendar(settings, client, manual=True)
        elif kind == "market":
            outcome = run_market(settings, client)
        elif kind == "monitor":
            outcome = run_monitor(settings, client)
        else:
            outcome = run_etf(settings, client)
        result_path.write_text(json.dumps({"outcome": outcome}), encoding="utf-8")
    finally:
        if client is not None:
            client.close()
        logging.getLogger(__name__).info("任务 %s 总耗时 %.2f 秒", kind, time.monotonic() - started)


if __name__ == "__main__":
    try:
        run(sys.argv[1], Path(sys.argv[2]))
    except Exception as exc:
        log_failure(logging.getLogger(__name__), sys.argv[1], exc)
        sys.exit(1)
