"""东方财富公开建议接口名称解析。"""

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from app.models.domain import StockInstrument
from app.providers.base import (
    AmbiguousSymbolError,
    ProviderError,
    SymbolNotFoundError,
)
from app.providers.http import ExternalHttpError, create_http_client, request_json

# 网页搜索接口公开使用的协议参数，不是用户凭据。
PUBLIC_SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"
A_SHARE_MARKETS = {"沪A", "深A", "北A"}
SYMBOL_PATTERN = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class _CacheEntry:
    instrument: StockInstrument
    expires_at: float


class EastmoneyStockSymbolResolver:
    """按股票全名精确解析中国 A 股代码。"""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        retry_count: int,
        cache_ttl_seconds: int,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._retry_count = retry_count
        self._cache_ttl_seconds = cache_ttl_seconds
        self._client = client or create_http_client(timeout_seconds)
        self._client.headers.update(
            {
                "Accept": "application/json",
                "Referer": "https://so.eastmoney.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        )
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "eastmoney"

    def resolve(self, name: str) -> StockInstrument:
        normalized_name = name.strip()
        cached = self._get_cached(normalized_name)
        if cached is not None:
            return cached

        payload = self._request(normalized_name)
        matches = self._parse_exact_matches(payload, normalized_name)
        if not matches:
            raise SymbolNotFoundError(normalized_name)
        if len(matches) > 1:
            raise AmbiguousSymbolError(normalized_name)

        instrument = matches[0]
        self._set_cached(normalized_name, instrument)
        return instrument

    def _request(self, name: str) -> dict[str, Any]:
        try:
            payload = request_json(
                self._client,
                f"{self._base_url}/api/suggest/get",
                params={
                    "input": name,
                    "type": "14",
                    "token": PUBLIC_SUGGEST_TOKEN,
                    "count": "20",
                },
                retry_count=self._retry_count,
            )
        except ExternalHttpError as exc:
            raise ProviderError("证券名称解析请求失败") from exc
        if not isinstance(payload, dict):
            raise ProviderError("证券名称解析响应非法")
        return payload

    @staticmethod
    def _parse_exact_matches(
        payload: dict[str, Any],
        name: str,
    ) -> list[StockInstrument]:
        table = payload.get("QuotationCodeTable")
        if not isinstance(table, dict) or str(table.get("Status")) != "0":
            raise ProviderError("证券名称解析返回失败状态")
        data = table.get("Data") or []
        if not isinstance(data, list):
            raise ProviderError("证券名称解析数据非法")

        matches: dict[str, StockInstrument] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("Code") or "").strip()
            item_name = str(item.get("Name") or "").strip()
            market = str(item.get("SecurityTypeName") or "").strip()
            if item_name != name or market not in A_SHARE_MARKETS:
                continue
            if not SYMBOL_PATTERN.fullmatch(symbol):
                continue
            matches[symbol] = StockInstrument(
                symbol=symbol,
                name=item_name,
                exchange=market,
            )
        return list(matches.values())

    def _get_cached(self, name: str) -> StockInstrument | None:
        with self._cache_lock:
            entry = self._cache.get(name)
            if entry is None or entry.expires_at <= self._clock():
                self._cache.pop(name, None)
                return None
            return entry.instrument

    def _set_cached(self, name: str, instrument: StockInstrument) -> None:
        if self._cache_ttl_seconds == 0:
            return
        with self._cache_lock:
            self._cache[name] = _CacheEntry(
                instrument=instrument,
                expires_at=self._clock() + self._cache_ttl_seconds,
            )
