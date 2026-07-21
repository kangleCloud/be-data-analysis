"""外部数据源共用的 HTTP 客户端与有限重试。"""

import time
from typing import Any

import httpx

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ExternalHttpError(Exception):
    """外部 HTTP 请求或响应失败，不携带请求详情。"""


def create_http_client(timeout_seconds: float) -> httpx.Client:
    """创建带连接池、固定超时且不跟随跳转的客户端。"""
    return httpx.Client(
        timeout=timeout_seconds,
        follow_redirects=False,
    )


def request_json(
    client: httpx.Client,
    url: str,
    params: dict[str, str],
    retry_count: int,
) -> Any:
    """请求 JSON，仅重试连接错误、限流和服务端故障。"""
    for attempt in range(retry_count + 1):
        try:
            response = client.get(
                url,
                params=params,
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            if attempt < retry_count:
                time.sleep(0.3 * (2**attempt))
                continue
            raise ExternalHttpError("外部请求失败") from exc

        if response.status_code in RETRYABLE_STATUS_CODES and attempt < retry_count:
            time.sleep(0.3 * (2**attempt))
            continue
        try:
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalHttpError("外部响应失败") from exc

    raise ExternalHttpError("外部请求超过重试次数")
