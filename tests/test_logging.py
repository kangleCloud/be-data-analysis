"""上海日志字段及已知故障脱敏分类。"""

import logging
from datetime import datetime, timezone
import pytest
import redis
import app.cli as cli
from app.core.config import load_settings
from app.core.logging import ShanghaiFormatter, log_failure, server_log_config


def test_shanghai_timestamp_pid_task_and_duration():
    record = logging.LogRecord("app.cli", logging.INFO, "", 0,
                               "任务总耗时 %.2f 秒", (1.25,), None)
    record.created = datetime(2026, 10, 8, 0, tzinfo=timezone.utc).timestamp()
    formatter = ShanghaiFormatter("%(asctime)s pid=%(process)d task=collect %(message)s")
    text = formatter.format(record)
    assert text.startswith("2026-10-08T08:00:00.000+08:00 pid=")
    assert "task=collect 任务总耗时 1.25 秒" in text
    for name in ("default", "access"):
        config = server_log_config()["formatters"][name]
        assert config["()"] is ShanghaiFormatter
        assert "task=serve" in config["fmt"] and "%(process)d" in config["fmt"]


@pytest.mark.parametrize("exception,category", [
    (redis.ConnectionError, "CONNECTION"), (redis.AuthenticationError, "AUTHENTICATION"),
    (redis.TimeoutError, "TIMEOUT"),
])
def test_cli_redis_failures_are_short_and_classified(monkeypatch, caplog, exception, category):
    import sys
    monkeypatch.setattr(sys, "argv", ["app", "collect"])
    monkeypatch.setattr("app.core.config.get_settings", lambda: load_settings({}))
    monkeypatch.setattr("app.core.logging.configure_logging", lambda *_args: None)
    def fail(*_args, **_kwargs):
        raise exception("private-password-and-host")
    monkeypatch.setattr("redis.Redis.from_url", fail)
    with caplog.at_level("INFO"):
        assert cli.main() == 1
    assert category in caplog.text
    assert "总耗时" in caplog.text
    assert "private-password-and-host" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_only_unexpected_program_errors_keep_traceback(caplog):
    try:
        raise RuntimeError("unexpected program failure")
    except RuntimeError as exc:
        log_failure(logging.getLogger("test.program"), "collect", exc)
    assert caplog.records[-1].exc_info is not None


def test_unexpected_error_preserves_exception_chain_without_raw_values(caplog):
    private_inner = "private-inner-response"
    private_outer = "private-outer-password"
    def inner():
        raise ValueError(private_inner)
    try:
        try:
            inner()
        except ValueError as cause:
            raise RuntimeError(private_outer) from cause
    except RuntimeError as exc:
        log_failure(logging.getLogger("test.program"), "etf", exc)
    assert "in inner" in caplog.text
    assert "ValueError（异常详情已脱敏）" in caplog.text
    assert "RuntimeError（异常详情已脱敏）" in caplog.text
    assert "private-inner-response" not in caplog.text
    assert "private-outer-password" not in caplog.text
