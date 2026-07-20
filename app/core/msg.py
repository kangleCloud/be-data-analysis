"""统一 HTTP 响应构造函数。"""

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def response(status_code: int, message: str, data: Any) -> JSONResponse:
    """创建 code/msg/data 结构的 JSON 响应。"""
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder({"code": status_code, "msg": message, "data": data}),
    )


def ok(data: Any, message: str = "操作成功") -> JSONResponse:
    return response(200, message, data)


def fail(status_code: int, message: str, data: Any = None) -> JSONResponse:
    return response(status_code, message, data)
