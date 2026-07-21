"""应用业务异常。"""

from typing import Any


class AppError(Exception):
    """可安全返回给调用方的业务异常。"""

    def __init__(self, status_code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.data = data


class InvalidQueryError(AppError):
    """查询参数之间存在业务冲突。"""

    def __init__(self, message: str) -> None:
        super().__init__(400, message)


class DataProviderUnavailableError(AppError):
    """外部行情数据源不可用。"""

    def __init__(self) -> None:
        super().__init__(502, "数据源暂时不可用")


class ResourceNotFoundError(AppError):
    """调用方查询的业务资源不存在。"""

    def __init__(self, message: str) -> None:
        super().__init__(404, message)


class ResourceConflictError(AppError):
    """调用方查询条件对应多个业务资源。"""

    def __init__(self, message: str) -> None:
        super().__init__(409, message)
