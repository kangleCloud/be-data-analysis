"""应用命令行启动入口。"""

import uvicorn

from app.core.config import get_settings


def main() -> int:
    """使用环境配置启动 Uvicorn 服务。"""
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.service_host,
        port=settings.service_port,
        log_level=settings.service_log_level,
    )
    return 0
