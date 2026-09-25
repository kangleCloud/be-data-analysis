"""Redis JSON 契约和锁释放。"""

import json

import pytest

from app.snapshot import LOCK_KEY, SNAPSHOT_KEY, UPDATES_CHANNEL, RedisSnapshotStore


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.events = []
        self.fail_set = False
        self.fail_publish = False

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if self.fail_set and key == SNAPSHOT_KEY:
            return False
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.events.append(("set", key, value))
        return True

    def publish(self, channel, payload):
        self.events.append(("publish", channel, payload))
        if self.fail_publish:
            raise ConnectionError("publish failed")
        return 0

    def eval(self, _script, _count, key, token):
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


def test_snapshot_is_plain_utf8_json_and_atomic_single_key():
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    assert SNAPSHOT_KEY == "stock:market:v2:snapshot"
    assert UPDATES_CHANNEL == "stock:market:v2:updates"
    store.save({
        "schemaVersion": 2,
        "generatedAt": "2026-09-23T10:00:00+08:00",
        "modules": {"industryHeatmap": {"data": "半导体"}},
    })
    assert list(client.values) == [SNAPSHOT_KEY]
    assert json.loads(client.values[SNAPSHOT_KEY])["modules"]["industryHeatmap"]["data"] == "半导体"
    assert "半导体" in client.values[SNAPSHOT_KEY]
    assert [event[:2] for event in client.events] == [
        ("set", SNAPSHOT_KEY), ("publish", UPDATES_CHANNEL)
    ]
    assert json.loads(client.events[1][2]) == {
        "schemaVersion": 2,
        "generatedAt": json.loads(client.values[SNAPSHOT_KEY])["generatedAt"],
    }


def test_failed_set_does_not_publish():
    client = FakeRedis()
    client.fail_set = True
    with pytest.raises(RuntimeError, match="写入失败"):
        RedisSnapshotStore(client, 240).save({
            "schemaVersion": 2, "generatedAt": "2026-09-23T10:00:00+08:00"
        })
    assert client.events == []
    assert client.get(SNAPSHOT_KEY) is None


def test_failed_publish_keeps_saved_snapshot(caplog):
    client = FakeRedis()
    client.fail_publish = True
    snapshot = {"schemaVersion": 2, "generatedAt": "2026-09-23T10:00:00+08:00"}
    RedisSnapshotStore(client, 240).save(snapshot)
    assert json.loads(client.get(SNAPSHOT_KEY)) == snapshot
    assert "更新通知发送失败" in caplog.text


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
    client.set(SNAPSHOT_KEY, '{"schemaVersion":1}')
    with pytest.raises(ValueError, match="版本"):
        RedisSnapshotStore(client, 240).load()
