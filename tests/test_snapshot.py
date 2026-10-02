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
        self.transactions = []

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

    def pipeline(self, transaction=True):
        assert transaction
        client = self

        class Pipeline:
            def __init__(self):
                self.commands = []

            def set(self, key, value):
                self.commands.append(("set", key, value))
                return self

            def delete(self, key):
                self.commands.append(("delete", key))
                return self

            def publish(self, channel, payload):
                self.commands.append(("publish", channel, payload))
                return self

            def execute(self):
                if client.fail_set and any(
                    command[:2] == ("set", SNAPSHOT_KEY) for command in self.commands
                ):
                    raise RuntimeError("模拟 Redis 事务失败")
                if client.fail_publish and any(command[0] == "publish" for command in self.commands):
                    raise ConnectionError("模拟 Redis 事务通知失败")
                client.transactions.append(tuple(self.commands))
                for command in self.commands:
                    if command[0] == "set":
                        client.set(command[1], command[2])
                    elif command[0] == "delete":
                        client.values.pop(command[1], None)
                        client.expiry.pop(command[1], None)
                    else:
                        client.publish(command[1], command[2])
                return [True] * len(self.commands)

        return Pipeline()

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
    notice = json.loads(client.events[1][2])
    assert notice == {
        "schemaVersion": 1,
        "snapshotId": json.loads(client.values[SNAPSHOT_KEY])["snapshotId"],
        "previousSnapshotId": None,
        "changedModules": ["industrySectors"],
    }


def test_failed_set_does_not_publish():
    client = FakeRedis()
    client.fail_set = True
    with pytest.raises(RuntimeError, match="事务失败"):
        RedisSnapshotStore(client, 240).save({
            "schemaVersion": 1, "generatedAt": "2026-09-23T10:00:00+08:00"
        })
    assert client.events == []
    assert client.get(SNAPSHOT_KEY) is None


def test_failed_transaction_does_not_save_or_publish():
    client = FakeRedis()
    client.fail_publish = True
    snapshot = {"schemaVersion": 1, "generatedAt": "2026-09-23T10:00:00+08:00"}
    with pytest.raises(ConnectionError, match="通知失败"):
        RedisSnapshotStore(client, 240).save(snapshot)
    assert client.get(SNAPSHOT_KEY) is None
    assert client.events == []


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


def test_fund_series_dedupes_sample_time_and_keeps_two_cross_year_dates():
    from app.snapshot import FUND_DATES_KEY, FUND_SERIES_PREFIX

    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    symbol = "SH600000"
    for day in ("2026-09-30", "2026-12-31", "2027-01-04"):
        point = {"collectedAt": f"{day}T10:00:00+08:00", "inflow": 100,
                 "outflow": 70, "netAmount": 30}
        snapshot = {"schemaVersion": 1, "generatedAt": point["collectedAt"]}
        store.save(snapshot, trade_date=day, fund_points={symbol: point})
        store.save(snapshot, trade_date=day, fund_points={symbol: point})
        assert len(json.loads(client.get(f"{FUND_SERIES_PREFIX}{day}:{symbol}"))) == 1
    assert json.loads(client.get(FUND_DATES_KEY)) == ["2026-12-31", "2027-01-04"]
    assert client.get(f"{FUND_SERIES_PREFIX}2026-09-30:{symbol}") is None
    assert client.get(f"{FUND_SERIES_PREFIX}2026-12-31:{symbol}") is not None
    assert client.get(f"{FUND_SERIES_PREFIX}2027-01-04:{symbol}") is not None
    assert len([event for event in client.events if event[0] == "publish"]) == 12


def test_market_snapshot_ids_link_and_only_changed_modules_are_listed():
    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    first = {"schemaVersion": 1, "generatedAt": "2026-09-23T10:00:00+08:00",
             "modules": {"industrySectors": {"status": "FRESH"},
                         "marketFundFlow": {"status": "FRESH"}}}
    store.save(first)
    first_id = store.load()["snapshotId"]
    second = {**first, "generatedAt": "2026-09-23T10:02:00+08:00",
              "modules": {"industrySectors": {"status": "FRESH"},
                          "marketFundFlow": {"status": "STALE"}}}
    store.save(second)
    second_id = store.load()["snapshotId"]
    notices = [json.loads(event[2]) for event in client.events
               if event[:2] == ("publish", UPDATES_CHANNEL)]
    assert first_id != second_id
    assert notices[0] == {
        "schemaVersion": 1, "snapshotId": first_id, "previousSnapshotId": None,
        "changedModules": ["industrySectors", "marketFundFlow"],
    }
    assert notices[1] == {
        "schemaVersion": 1, "snapshotId": second_id,
        "previousSnapshotId": first_id, "changedModules": ["marketFundFlow"],
    }
    assert client.transactions[1][-1][:2] == ("publish", UPDATES_CHANNEL)


def test_fund_point_market_and_monitor_events_follow_atomic_business_writes():
    from app.snapshot import FUND_SERIES_PREFIX
    from app.stock_monitor import MONITOR_STATE_KEY, MONITOR_UPDATES_CHANNEL

    client = FakeRedis()
    store = RedisSnapshotStore(client, 240)
    symbol = "SH600000"
    for minute in (0, 2):
        timestamp = f"2026-09-23T10:{minute:02d}:00+08:00"
        store.save({"schemaVersion": 1, "generatedAt": timestamp, "modules": {}},
                   trade_date="2026-09-23", fund_points={symbol: {
                       "collectedAt": timestamp, "inflow": 100, "outflow": 40,
                       "netAmount": 60,
                   }})
    first, second = client.transactions
    for transaction in (first, second):
        names = [command[:2] for command in transaction]
        assert ("set", SNAPSHOT_KEY) in names
        assert ("set", f"{FUND_SERIES_PREFIX}2026-09-23:{symbol}") in names
        assert ("set", MONITOR_STATE_KEY) in names
        assert names[-2:] == [
            ("publish", MONITOR_UPDATES_CHANNEL), ("publish", UPDATES_CHANNEL)
        ]
    notices = [json.loads(event[2]) for event in client.events
               if event[:2] == ("publish", MONITOR_UPDATES_CHANNEL)]
    assert notices[0]["baseStateId"] is None
    assert notices[1]["baseStateId"] == notices[0]["stateId"]
    assert notices[0]["stateId"] != notices[1]["stateId"]
    assert notices[1]["stateId"] == client.get(MONITOR_STATE_KEY)
    assert notices[0]["changedSymbols"] == [symbol]
