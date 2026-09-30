"""Redis JSON 契约和锁释放。"""

import json

import pytest

from app.snapshot import (
    LOCK_KEY, RENEW_LOCK_SCRIPT, SNAPSHOT_KEY, UPDATES_CHANNEL,
    RedisSnapshotStore,
)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiry = {}
        self.now = 0
        self.events = []
        self.fail_set = False
        self.fail_publish = False

    def get(self, key):
        if key in self.expiry and self.expiry[key] <= self.now:
            self.values.pop(key, None)
            self.expiry.pop(key, None)
        return self.values.get(key)

    def exists(self, *keys):
        return sum(self.get(key) is not None for key in keys)

    def advance(self, seconds):
        self.now += seconds

    def set(self, key, value, nx=False, ex=None):
        if self.fail_set and key == SNAPSHOT_KEY:
            return False
        if nx and self.get(key) is not None:
            return False
        self.values[key] = value
        if ex is not None:
            self.expiry[key] = self.now + int(ex)
        self.events.append(("set", key, value))
        return True

    def publish(self, channel, payload):
        self.events.append(("publish", channel, payload))
        if self.fail_publish:
            raise ConnectionError("publish failed")
        return 0

    def eval(self, script, count, *args):
        if script == RENEW_LOCK_SCRIPT:
            key, token, seconds = args
            if self.get(key) != token:
                return 0
            self.expiry[key] = self.now + seconds
            return 1
        key, token = args
        if self.get(key) == token:
            del self.values[key]
            self.expiry.pop(key, None)
            return 1
        return 0


def test_snapshot_is_plain_utf8_json_and_atomic_single_key():
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    assert SNAPSHOT_KEY == "stock:market:v1:snapshot"
    assert UPDATES_CHANNEL == "stock:market:v1:updates"
    store.save({
        "schemaVersion": 1,
        "generatedAt": "2026-09-23T10:00:00+08:00",
        "modules": {"industrySectors": {"data": "半导体"}},
    })
    assert list(client.values) == [SNAPSHOT_KEY]
    assert json.loads(client.values[SNAPSHOT_KEY])["modules"]["industrySectors"]["data"] == "半导体"
    assert "半导体" in client.values[SNAPSHOT_KEY]
    assert [event[:2] for event in client.events] == [
        ("set", SNAPSHOT_KEY), ("publish", UPDATES_CHANNEL)
    ]
    assert json.loads(client.events[1][2]) == {
        "schemaVersion": 1,
        "generatedAt": json.loads(client.values[SNAPSHOT_KEY])["generatedAt"],
    }


def test_failed_set_does_not_publish():
    client = FakeRedis()
    client.fail_set = True
    with pytest.raises(RuntimeError, match="写入失败"):
        RedisSnapshotStore(client, 240).save({
            "schemaVersion": 1, "generatedAt": "2026-09-23T10:00:00+08:00"
        })
    assert client.events == []
    assert client.get(SNAPSHOT_KEY) is None


def test_failed_publish_keeps_saved_snapshot(caplog):
    client = FakeRedis()
    client.fail_publish = True
    snapshot = {"schemaVersion": 1, "generatedAt": "2026-09-23T10:00:00+08:00"}
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


def test_lock_renewal_extends_long_request_and_rejects_wrong_owner():
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    token = store.acquire()
    client.advance(200)
    assert not store.renew("wrong-owner")
    assert store.renew(token)
    client.advance(200)
    assert client.get(LOCK_KEY) == token
    store.release(token)


def test_background_renewal_keeps_long_pagination_lock(monkeypatch):
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    token = store.acquire()
    client.advance(200)
    waits = []

    def wait(_interval):
        waits.append(1)
        return len(waits) > 1

    monkeypatch.setattr(store._renew_stop, "wait", wait)
    store.start_renewal(token)
    store._renew_thread.join(timeout=1)
    assert client.expiry[LOCK_KEY] == 440
    client.advance(200)
    assert client.get(LOCK_KEY) == token
    store.stop_renewal()
    store.release(token)


def test_slot_reservation_is_atomic_and_enforces_interval():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    client = FakeRedis()
    first = RedisSnapshotStore(client, 240)
    second = RedisSnapshotStore(client, 240)
    at = datetime(2026, 9, 23, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert first.reserve_slot(at)
    assert not second.reserve_slot(at)
    assert not second.reserve_slot(at.replace(minute=1))
    client.advance(120)
    assert second.reserve_slot(at.replace(minute=2))


def test_unsupported_snapshot_version_is_rejected():
    client = FakeRedis()
    client.set(SNAPSHOT_KEY, '{"schemaVersion":2}')
    with pytest.raises(ValueError, match="版本"):
        RedisSnapshotStore(client, 240).load()
