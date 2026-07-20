"""应用日志初始化。"""

import logging


def configure_logging(level: str) -> None:
    """配置统一日志格式，不覆盖宿主已经安装的处理器。"""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
