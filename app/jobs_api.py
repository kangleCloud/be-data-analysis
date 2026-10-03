"""受保护的同步手动任务入口。"""

import hmac
import json
import logging
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import redis
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from app.core.config import Settings

LOGGER = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
Kind = Literal["calendar", "market", "monitor", "etf"]
LOCK_PREFIX = "stock:jobs:v1:lock:"
# 市场三个源最多约 120 + 120 + 900 秒，另留日历与进程开销。
JOB_TIMEOUT_SECONDS = {"calendar": 60, "market": 1380, "monitor": 300, "etf": 120}
LOCK_SECONDS = 1440
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
MESSAGES = {
    "refreshed": "交易日历刷新成功",
    "published": "采集发布成功",
    "partial": "部分数据源失败，已按模块降级",
    "skipped": "当前不在交易窗口或不是交易日",
    "throttled": "任务触发间隔未满足",
    "locked": "同类采集正在运行",
    "cooldown": "数据源冷却中",
    "disabled": "雪球采集总闸关闭",
    "missing_token": "雪球令牌未配置",
    "failed": "源刷新失败，原缓存已保留",
}


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _state(outcome: str) -> str:
    if outcome in {"refreshed", "published"}:
        return "SUCCEEDED"
    if outcome == "partial":
        return "PARTIAL"
    if outcome in {"skipped", "throttled", "locked", "cooldown", "disabled"}:
        return "SKIPPED"
    return "FAILED"


def _run_default(kind: Kind) -> str:
    """在线程池路由外的独立进程执行，保证 SIGALRM 只在主线程使用。"""
    with tempfile.TemporaryDirectory(prefix="stock-job-") as directory:
        result_path = Path(directory) / "result.json"
        process = subprocess.run(
            [sys.executable, "-m", "app.job_worker", kind, str(result_path)],
            timeout=JOB_TIMEOUT_SECONDS[kind], check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(f"任务子进程退出码 {process.returncode}")
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        outcome = payload.get("outcome")
        if not isinstance(outcome, str) or outcome not in MESSAGES:
            raise ValueError("任务子进程返回无效结果")
        return outcome


def create_jobs_router(
    settings: Settings, *, redis_factory: Callable[[], Any] | None = None,
    runner: Callable[[Kind], str] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/internal/jobs/v1", tags=["内部手动任务"])

    def authorize(token: str | None) -> None:
        expected = settings.stock_monitor_internal_token.get_secret_value()
        if not expected:
            raise HTTPException(status_code=503, detail="内部接口令牌未配置")
        if token is None or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="内部接口认证失败")

    @router.post("/{kind}/refresh", summary="同步执行手动任务")
    def refresh(
        kind: Kind,
        x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    ) -> JSONResponse:
        authorize(x_internal_token)
        started_at = _now()
        try:
            client = redis_factory() if redis_factory else redis.Redis.from_url(
                settings.redis_url.get_secret_value(), decode_responses=True,
                socket_timeout=5, socket_connect_timeout=5,
            )
        except Exception as exc:
            LOGGER.warning("手动任务 %s 无法连接 Redis，异常 %s", kind, type(exc).__name__)
            return JSONResponse(status_code=503, content={
                "kind": kind, "state": "FAILED", "outcome": "failed",
                "startedAt": started_at, "finishedAt": _now(), "message": "任务基础设施不可用",
            })
        token = uuid4().hex
        lock_key = f"{LOCK_PREFIX}{kind}"
        acquired = False
        status_code = 200
        outcome = "failed"
        message = "任务执行失败"
        try:
            acquired = bool(client.set(lock_key, token, nx=True, ex=LOCK_SECONDS))
            if not acquired:
                status_code, outcome, message = 409, "locked", MESSAGES["locked"]
            else:
                try:
                    outcome = (runner or _run_default)(kind)
                    if not isinstance(outcome, str) or outcome not in MESSAGES:
                        raise ValueError("任务返回无效结果")
                    message = MESSAGES[outcome]
                except Exception as exc:
                    LOGGER.warning("手动任务 %s 失败，异常 %s", kind, type(exc).__name__)
                    status_code, outcome, message = 500, "failed", "任务执行失败"
        except Exception as exc:
            LOGGER.warning("手动任务 %s Redis 锁失败，异常 %s", kind, type(exc).__name__)
            status_code, outcome, message = 503, "failed", "任务基础设施不可用"
        finally:
            if acquired:
                try:
                    client.eval(RELEASE_SCRIPT, 1, lock_key, token)
                except Exception as exc:
                    LOGGER.warning("手动任务 %s 锁释放失败，异常 %s", kind, type(exc).__name__)
                    status_code, outcome, message = 503, "failed", "任务基础设施不可用"
            try:
                client.close()
            except Exception as exc:
                LOGGER.warning("手动任务 %s Redis 连接关闭失败，异常 %s", kind, type(exc).__name__)
        return JSONResponse(status_code=status_code, content={
            "kind": kind, "state": _state(outcome), "outcome": outcome,
            "startedAt": started_at, "finishedAt": _now(), "message": message,
        })

    return router
