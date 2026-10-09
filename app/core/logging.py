"""上海时间日志与已知基础设施故障的脱敏分类。"""

import copy
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
import requests

SHANGHAI = ZoneInfo("Asia/Shanghai")


class ShanghaiFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, SHANGHAI).isoformat(timespec="milliseconds")


def configure_logging(level: str, task: str) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(ShanghaiFormatter(
            f"%(asctime)s pid=%(process)d task={task} %(levelname)s:%(name)s:%(message)s"
        ))


def server_log_config() -> dict:
    from uvicorn.config import LOGGING_CONFIG
    config = copy.deepcopy(LOGGING_CONFIG)
    for name in ("default", "access"):
        config["formatters"][name] = {
            "()": ShanghaiFormatter,
            "fmt": "%(asctime)s pid=%(process)d task=serve %(levelname)s:%(name)s:%(message)s",
        }
    return config


def redis_failure_kind(exc: BaseException) -> str | None:
    if isinstance(exc, redis.AuthenticationError):
        return "AUTHENTICATION"
    if isinstance(exc, redis.TimeoutError):
        return "TIMEOUT"
    if isinstance(exc, redis.ConnectionError):
        return "CONNECTION"
    if isinstance(exc, redis.RedisError):
        return "REDIS_ERROR"
    return None


def log_failure(logger: logging.Logger, task: str, exc: BaseException) -> None:
    category = redis_failure_kind(exc)
    if category:
        logger.error("任务 %s Redis 故障，分类 %s", task, category)
    elif isinstance(exc, (requests.RequestException, TimeoutError)) or (
        type(exc).__name__ in {"SourceDataError", "SourceCallError", "EtfSourceError", "XueqiuSourceError"}
        and getattr(exc, "category", None) != "UNEXPECTED"
    ):
        logger.warning("任务 %s 源故障，异常 %s", task, type(exc).__name__)
    else:
        # 保留所有调用帧与异常链，但异常文本可能携带响应或凭据，必须脱敏。
        redacted: dict[int, BaseException] = {}
        def redact(error: BaseException) -> BaseException:
            if id(error) not in redacted:
                safe = RuntimeError(f"{type(error).__name__}（异常详情已脱敏）")
                redacted[id(error)] = safe
                safe.__traceback__ = error.__traceback__
                safe.__suppress_context__ = error.__suppress_context__
                if error.__cause__ is not None:
                    safe.__cause__ = redact(error.__cause__)
                if error.__context__ is not None:
                    safe.__context__ = redact(error.__context__)
            return redacted[id(error)]
        safe = redact(exc)
        logger.exception("任务 %s 非预期程序异常，类型 %s", task, type(exc).__name__,
                         exc_info=(type(safe), safe, safe.__traceback__))
