"""市场采集、个股采样、日历刷新和健康服务入口。"""

import argparse
import logging
import time

LOGGER = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="A 股数据采集程序")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect", help="执行一次市场采集")
    collect.add_argument("--force", action="store_true", help="主动触发，但不绕过风控")
    subparsers.add_parser("calendar-refresh", help="刷新共享交易日历")
    subparsers.add_parser("monitor-sample", help="执行一次个股采样")
    subparsers.add_parser("etf-collect", help="执行一次 ETF 行情采集")
    subparsers.add_parser("serve", help="启动健康接口与调度")
    sources = subparsers.add_parser("sources", help="只读输出第三方数据源说明，不检测源健康")
    sources.add_argument("--json", action="store_true", help="输出完整静态元数据 JSON")
    args = parser.parse_args()
    if args.command == "sources":
        from app.source_catalog import render_sources
        print(render_sources(as_json=args.json))
        return 0

    import redis
    import uvicorn
    from app.core.config import get_settings
    from app.core.logging import configure_logging, log_failure, server_log_config
    from app.workflows import run_calendar, run_etf, run_market, run_monitor

    settings = get_settings()
    configure_logging(settings.service_log_level, args.command)

    if args.command == "serve":
        uvicorn.run(
            "app.main:app", host=settings.service_host,
            port=settings.service_port, log_level=settings.service_log_level,
            log_config=server_log_config(),
        )
        return 0

    if args.command == "monitor-sample" and not settings.stock_monitor_xq_enabled:
        LOGGER.info("雪球生产采集已关闭，跳过个股采样")
        return 0

    started = time.monotonic()
    client = None
    try:
        client = redis.Redis.from_url(
            settings.redis_url.get_secret_value(), decode_responses=True,
            socket_timeout=5, socket_connect_timeout=5,
        )
        if args.command == "calendar-refresh":
            outcome = run_calendar(settings, client)
        elif args.command == "monitor-sample":
            outcome = run_monitor(settings, client)
        elif args.command == "etf-collect":
            outcome = run_etf(settings, client)
        else:
            outcome = run_market(settings, client)
        LOGGER.info("%s 结果: %s", args.command, outcome)
        return 0 if outcome in {
            "refreshed", "published", "skipped", "throttled", "locked",
            "cooldown", "disabled",
        } else 1
    except Exception as exc:
        log_failure(LOGGER, args.command, exc)
        return 1
    finally:
        if client is not None:
            client.close()
        LOGGER.info("任务 %s 总耗时 %.2f 秒", args.command, time.monotonic() - started)
