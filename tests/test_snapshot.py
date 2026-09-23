"""Redis JSON 契约和锁释放。"""

import json

import pytest

from app.snapshot import LOCK_KEY, SNAPSHOT_KEY, RedisSnapshotStore


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, _script, _count, key, token):
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


def test_snapshot_is_plain_utf8_json_and_atomic_single_key():
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    store.save({"schemaVersion": 1, "modules": {"industryHeatmap": {"data": "半导体"}}})
    assert list(client.values) == [SNAPSHOT_KEY]
    assert json.loads(client.values[SNAPSHOT_KEY])["modules"]["industryHeatmap"]["data"] == "半导体"
    assert "半导体" in client.values[SNAPSHOT_KEY]


def test_lock_excludes_overlap_and_only_owner_can_release():
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    token = store.acquire()
    assert token is not None
    assert store.acquire() is None
    store.release("wrong-owner")
    assert client.get(LOCK_KEY) == token
    store.release(token)
    assert store.acquire() is not None


def test_unsupported_snapshot_version_is_rejected():
    client = FakeRedis()
    client.set(SNAPSHOT_KEY, '{"schemaVersion":2}')
    with pytest.raises(ValueError, match="版本"):
        RedisSnapshotStore(client, 240).load()
