"""跨语言 Redis JSON 快照存储。"""

import json
from typing import Any, Protocol
from uuid import uuid4

SNAPSHOT_KEY = "stock:market:v1:snapshot"
LOCK_KEY = "stock:market:v1:lock"
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
        if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
            raise ValueError("Redis 快照版本不受支持")
        return snapshot

    def save(self, snapshot: dict[str, Any]) -> None:
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        self._client.set(SNAPSHOT_KEY, payload)
