"""每次请求显式传递采集模式，认证由调用路由先完成。"""

from typing import Literal
from fastapi import HTTPException

CollectionMode = Literal["auto", "manual"]


def collection_mode(value: str | None) -> CollectionMode:
    if value is None:
        return "auto"
    if value not in {"auto", "manual"}:
        raise HTTPException(status_code=400, detail="X-Collection-Mode 必须为 auto 或 manual")
    return value
