"""跨语言 Redis JSON 快照存储。"""

import json
import logging
import threading
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

SNAPSHOT_KEY = "stock:market:v1:snapshot"
UPDATES_CHANNEL = "stock:market:v1:updates"
LOCK_KEY = "stock:market:v1:lock"
MIN_INTERVAL_KEY = "stock:market:v1:min-interval"
COOLDOWN_KEY_PREFIX = "stock:market:v1:cooldown:"
MIN_INTERVAL_SECONDS = 120
SOURCE_COOLDOWN_SECONDS = 2 * 60 * 60
LOGGER = logging.getLogger(__name__)
RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
RENEW_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class SnapshotStore(Protocol):
    def acquire(self) -> str | None: ...

    def release(self, token: str) -> None: ...

    def start_renewal(self, token: str) -> None: ...

    def stop_renewal(self) -> None: ...

    def renew(self, token: str) -> bool: ...

    def load(self) -> dict[str, Any] | None: ...

    def save(self, snapshot: dict[str, Any]) -> None: ...

    def reserve_slot(self, at: datetime) -> bool: ...

    def cooldown_active(self, source: str) -> bool: ...

    def start_cooldown(self, source: str) -> None: ...


class RedisSnapshotStore:
    """单键原子发布；保留上次成功数据供降级使用。"""

    def __init__(self, client: Any, lock_seconds: int) -> None:
        self._client = client
        self._lock_seconds = lock_seconds
        self._renew_stop = threading.Event()
        self._renew_thread: threading.Thread | None = None

    def acquire(self) -> str | None:
        token = uuid4().hex
        acquired = self._client.set(LOCK_KEY, token, nx=True, ex=self._lock_seconds)
        return token if acquired else None

    def release(self, token: str) -> None:
        self._client.eval(RELEASE_LOCK_SCRIPT, 1, LOCK_KEY, token)

    def renew(self, token: str) -> bool:
        return bool(self._client.eval(RENEW_LOCK_SCRIPT, 1, LOCK_KEY, token, self._lock_seconds))

    def start_renewal(self, token: str) -> None:
        self._renew_stop.clear()

        def keep_alive() -> None:
            interval = max(1, min(30, self._lock_seconds / 3))
            while not self._renew_stop.wait(interval):
                try:
                    if not self.renew(token):
                        LOGGER.error("市场采集锁已失效，停止续租")
                        return
                except Exception:
                    LOGGER.exception("市场采集锁续租失败")
                    return

        self._renew_thread = threading.Thread(target=keep_alive, daemon=True)
        self._renew_thread.start()

    def stop_renewal(self) -> None:
        self._renew_stop.set()
        if self._renew_thread is not None:
            self._renew_thread.join(timeout=2)
            self._renew_thread = None

    def reserve_slot(self, at: datetime) -> bool:
        """Redis 原子占位，手动采集也遵守两分钟间隔。"""
        return bool(self._client.set(
            MIN_INTERVAL_KEY, "1", nx=True, ex=MIN_INTERVAL_SECONDS,
        ))

    def cooldown_active(self, source: str) -> bool:
        return bool(self._client.exists(f"{COOLDOWN_KEY_PREFIX}{source}"))

    def start_cooldown(self, source: str) -> None:
        self._client.set(
            f"{COOLDOWN_KEY_PREFIX}{source}", "1", ex=SOURCE_COOLDOWN_SECONDS
        )

    def load(self) -> dict[str, Any] | None:
        raw = self._client.get(SNAPSHOT_KEY)
        if raw is None:
            return None
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
            raise ValueError("Redis 快照版本不受支持")
        return snapshot

    def save(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("schemaVersion") != 1 or not isinstance(
            snapshot.get("generatedAt"), str
        ):
            raise ValueError("Redis 快照通知字段无效")
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        notice = json.dumps(
            {
                "schemaVersion": snapshot["schemaVersion"],
                "generatedAt": snapshot["generatedAt"],
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if not self._client.set(SNAPSHOT_KEY, payload):
            raise RuntimeError("Redis 快照写入失败")
        try:
            self._client.publish(UPDATES_CHANNEL, notice)
        except Exception:
            LOGGER.exception("Redis 快照已写入，但更新通知发送失败")
