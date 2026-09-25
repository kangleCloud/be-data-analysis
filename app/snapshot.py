"""跨语言 Redis JSON 快照存储。"""

import json
import logging
from typing import Any, Protocol
from uuid import uuid4

SNAPSHOT_KEY = "stock:market:v2:snapshot"
UPDATES_CHANNEL = "stock:market:v2:updates"
LOCK_KEY = "stock:market:v2:lock"
LOGGER = logging.getLogger(__name__)
RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class SnapshotStore(Protocol):
    def acquire(self) -> str | None: ...

    def release(self, token: str) -> None: ...

    def load(self) -> dict[str, Any] | None: ...

    def save(self, snapshot: dict[str, Any]) -> None: ...


class RedisSnapshotStore:
    """单键原子发布；保留上次成功数据供降级使用。"""

    def __init__(self, client: Any, lock_seconds: int) -> None:
        self._client = client
        self._lock_seconds = lock_seconds

    def acquire(self) -> str | None:
        token = uuid4().hex
        acquired = self._client.set(LOCK_KEY, token, nx=True, ex=self._lock_seconds)
        return token if acquired else None

    def release(self, token: str) -> None:
        self._client.eval(RELEASE_LOCK_SCRIPT, 1, LOCK_KEY, token)

    def load(self) -> dict[str, Any] | None:
        raw = self._client.get(SNAPSHOT_KEY)
        if raw is None:
            return None
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 2:
            raise ValueError("Redis 快照版本不受支持")
        return snapshot

    def save(self, snapshot: dict[str, Any]) -> None:
        if snapshot.get("schemaVersion") != 2 or not isinstance(
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
