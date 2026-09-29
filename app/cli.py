"""单次采集和可选健康服务入口。"""

import argparse
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
import uvicorn

from app.collector import MarketCollector
from app.core.config import get_settings
from app.normalize import normalize_market_fund_flow
from app.providers.akshare_market import AkShareMarketProvider
from app.providers.xueqiu import XueqiuProvider
from app.snapshot import RedisSnapshotStore
from app.stock_monitor import MonitorStore, StockMonitorSampler

LOGGER = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="A 股市场快照采集程序")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect", help="执行一次采集")
    collect_parser.add_argument("--force", action="store_true", help="允许在交易时段外补采")
    subparsers.add_parser("probe", help="只读检查大盘资金流源数据日期")
    subparsers.add_parser("serve", help="启动可选的健康接口")
    subparsers.add_parser("monitor-sample", help="运行一次个股监控盘中采样")
    args = parser.parse_args()
    settings = get_settings()
    logging.basicConfig(level=settings.service_log_level.upper())

    if args.command == "serve":
        uvicorn.run(
            "app.main:app",
            host=settings.service_host,
            port=settings.service_port,
            log_level=settings.service_log_level,
        )
        return 0

    if args.command == "probe":
        try:
            observed_at = datetime.now(ZoneInfo("Asia/Shanghai"))
            source_date, _ = normalize_market_fund_flow(
                AkShareMarketProvider(settings.source_timeout_seconds).market_fund_flow()
            )
            print(json.dumps({
                "observedAt": observed_at.isoformat(timespec="seconds"),
                "latestTradeDate": source_date,
                "sameCalendarDay": source_date == observed_at.date().isoformat(),
            }, ensure_ascii=False))
            return 0
        except Exception:
            LOGGER.exception("大盘资金流源数据检查失败")
            return 1

    if args.command == "monitor-sample":
        if not settings.stock_monitor_xq_enabled:
            LOGGER.info("雪球生产采集已关闭，跳过个股采样")
            return 0
        token = settings.xueqiu_token.get_secret_value()
        if not token:
            LOGGER.error("雪球令牌未配置，跳过个股采样")
            return 1
        client = redis.Redis.from_url(
            settings.redis_url.get_secret_value(), decode_responses=True,
            socket_timeout=5, socket_connect_timeout=5,
        )
        try:
            outcome = StockMonitorSampler(
                MonitorStore(client),
                XueqiuProvider(token, settings.source_timeout_seconds),
                AkShareMarketProvider(settings.source_timeout_seconds),
                xq_enabled=settings.stock_monitor_xq_enabled,
            ).sample(datetime.now(ZoneInfo("Asia/Shanghai")))
            LOGGER.info("个股采样结果: %s", outcome)
            return 0 if outcome in {"published", "skipped", "locked", "cooldown"} else 1
        except Exception:
            LOGGER.exception("个股采样失败")
            return 1
        finally:
            client.close()

    client = redis.Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    try:
        collector = MarketCollector(
            AkShareMarketProvider(
                settings.source_timeout_seconds, pace_ths_requests=True
            ),
            RedisSnapshotStore(client, settings.redis_lock_seconds),
        )
        outcome = collector.collect(
            datetime.now(ZoneInfo("Asia/Shanghai")), force=args.force
        )
        LOGGER.info("采集结果: %s", outcome)
        return 0 if outcome in {"published", "skipped", "throttled"} else 1
    except Exception:
        LOGGER.exception("市场快照采集失败")
        return 1
    finally:
        client.close()
