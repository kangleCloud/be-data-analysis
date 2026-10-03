"""内部任务的独立进程入口，隔离市场源的主线程信号超时。"""

import json
import logging
import sys
from pathlib import Path
from typing import Literal

import redis

from app.core.config import get_settings
from app.workflows import run_calendar, run_etf, run_market, run_monitor

Kind = Literal["calendar", "market", "monitor", "etf"]


def run(kind: Kind, result_path: Path) -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.service_log_level.upper())
    client = redis.Redis.from_url(
        settings.redis_url.get_secret_value(), decode_responses=True,
        socket_timeout=5, socket_connect_timeout=5,
    )
    try:
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
        client.close()


if __name__ == "__main__":
    run(sys.argv[1], Path(sys.argv[2]))
