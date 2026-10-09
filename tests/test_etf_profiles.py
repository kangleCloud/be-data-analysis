"""同花顺资料转换、独立批次锁及频率预算。"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_settings
from app.etf_normalize import ths_profile
from app.etf_profiles import INTERVAL_PREFIX, LOCK_KEY, ProfileBatchError, collect_profiles
from app.main import create_app
from tests.test_etf_api import HEADERS, RedisClient


def rows(code="510050", **fields):
    values = {"基金代码": code, "基金全称": "50ETF基金", **fields}
    return [{"字段": key, "值": value} for key, value in values.items()]


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr("app.etf_profiles.time.monotonic", value.monotonic)
    monkeypatch.setattr("app.etf_profiles.time.sleep", value.sleep)
    return value


class Source:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.fail = set()
        self.duration = 0

    def profile(self, code, *, budget_seconds):
        self.calls.append((code, self.clock.now, budget_seconds))
        self.clock.now += min(self.duration, budget_seconds)
        if code in self.fail:
            raise ConnectionError("模拟源失败")
        return rows(code)


def test_normalize_maps_nullable_fields_without_inventing_listing_or_index():
    profile = ths_profile(rows(**{
        "基金类型": "股票型", "投资类型": "指数型", "基金经理": "经理甲",
        "基金管理人": "管理公司乙", "基金托管人": "银行丙",
        "成立日期": "2004年12月30日", "业绩比较基准": "上证50指数收益率",
    }), "SH510050", "2026-10-03T10:00:01+08:00")
    assert profile["fundManager"] == "经理甲"
    assert profile["manager"] == "管理公司乙"
    assert profile["custodian"] == "银行丙"
    assert profile["establishedDate"] == "2004-12-30"
    assert profile["investmentType"] == "指数型"
    assert profile["performanceBenchmark"] == "上证50指数收益率"
    assert not {"listingDate", "listingStatus", "shareCount", "shareDate",
                "trackingIndexCode", "trackingIndexName"} & profile.keys()
    nullable = ths_profile(rows(**{"基金经理": "--", "成立日期": "错误日期",
                                   "基金类型": float("nan")}), "SH510050", "now")
    assert nullable["fundManager"] is None
    assert nullable["establishedDate"] is None
    assert nullable["fundType"] is None


@pytest.mark.parametrize("raw", [[], [{"item": "基金代码", "value": "510050"}],
    rows("159919"), [{"字段": "基金代码", "值": "510050"}],
    rows() + [{"字段": "基金全称", "值": "冲突名称"}],
])
def test_normalize_rejects_empty_changed_fields_mismatch_and_conflicts(raw):
    with pytest.raises(ValueError):
        ths_profile(raw, "SH510050", "now")


def test_partial_success_spacing_and_30minute_per_symbol_limit(clock):
    source, client = Source(clock), RedisClient()
    source.fail.add("159919")
    result = collect_profiles(source, client, ["SH510050", "SZ159919"])
    assert result["sourceStatus"] == {"SH510050": "OK", "SZ159919": "ERROR"}
    assert [call[1] for call in source.calls] == [0, 0]
    assert [row["symbol"] for row in result["profiles"]] == ["SH510050"]
    assert client.get(LOCK_KEY) is None
    with pytest.raises(ProfileBatchError) as error:
        collect_profiles(source, client, ["SH510050", "SZ159919"])
    assert error.value.status_code == 429
    assert error.value.states == {"SH510050": "SKIPPED", "SZ159919": "SKIPPED"}
    assert len(source.calls) == 2
    client.advance(1800)
    collect_profiles(source, client, ["SH510050"])
    assert len(source.calls) == 3


def test_budget_marks_remaining_symbols_skipped_without_attempt_or_reservation(clock):
    source, client = Source(clock), RedisClient()
    source.duration = 179
    result = collect_profiles(source, client, ["SH510050", "SZ159919"])
    assert result["sourceStatus"] == {"SH510050": "OK", "SZ159919": "SKIPPED"}
    assert len(source.calls) == 1
    assert client.get(f"{INTERVAL_PREFIX}SZ159919") is None
    assert clock.now <= 180


def test_python_lock_excludes_batches_without_competing_with_java_refresh_lock(clock):
    source, client = Source(clock), RedisClient()
    client.set("stock:etf-monitor:v1:refresh:lock", "java-owned")
    collect_profiles(source, client, ["SH510050"])
    assert client.get("stock:etf-monitor:v1:refresh:lock") == "java-owned"
    client.set(LOCK_KEY, "python-owned")
    with pytest.raises(ProfileBatchError) as error:
        collect_profiles(source, client, ["SZ159919"])
    assert error.value.status_code == 409
    assert client.get(LOCK_KEY) == "python-owned"
    assert len(source.calls) == 1


def test_all_failed_raises_explicit_failure_and_releases_lock(clock):
    source, client = Source(clock), RedisClient()
    source.fail.add("510050")
    with pytest.raises(ProfileBatchError) as error:
        collect_profiles(source, client, ["SH510050"])
    assert error.value.status_code == 502
    assert error.value.states == {"SH510050": "ERROR"}
    assert client.get(LOCK_KEY) is None


def test_api_validates_new_contract_and_returns_failure_statuses_with_xq_off(clock):
    source, redis_client = Source(clock), RedisClient()
    app = create_app(scheduler_enabled=False,
        settings=load_settings({"STOCK_MONITOR_INTERNAL_TOKEN": "service-secret"}),
        etf_factory=lambda: source, redis_factory=lambda: redis_client)
    client = TestClient(app)
    path = "/internal/etf-monitor/v1/profiles"
    assert client.post(path, json={"symbols": ["SH510050"]}).status_code == 401
    for body in ({"symbols": []}, {"symbols": ["bad"]},
                 {"symbols": ["SH510050"] * 11},
                 {"symbols": ["SH510050"], "asOfDate": "20260930"}):
        assert client.post(path, json=body, headers=HEADERS).status_code == 422
    source.fail.add("510050")
    response = client.post(path, json={"symbols": ["SH510050"]}, headers=HEADERS)
    assert response.status_code == 502
    assert response.json()["detail"]["sourceStatus"] == {"SH510050": "ERROR"}
    assert client.post(path, json={"symbols": ["SH510050"]}, headers=HEADERS).status_code == 429
    redis_client.set(LOCK_KEY, "other-batch")
    assert client.post(path, json={"symbols": ["SZ159919"]}, headers=HEADERS).status_code == 409


def test_422_logs_field_and_reason_without_request_values(caplog):
    client = TestClient(create_app(scheduler_enabled=False,
        settings=load_settings({"STOCK_MONITOR_INTERNAL_TOKEN": "service-secret"})))
    path = "/internal/etf-monitor/v1/profiles"
    response = client.post(path, json={"symbols": ["SH510050"],
                                      "asOfDate": "do-not-log-private"}, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "asOfDate"]
    assert "body.asOfDate: extra_forbidden" in caplog.text
    assert "do-not-log-private" not in caplog.text
    assert "service-secret" not in caplog.text
    caplog.clear()
    response = client.post(path, json={"symbols": ["invalid-symbol"]}, headers=HEADERS)
    assert response.status_code == 422
    assert "数量 1，无效代码 1，重复代码 False" in caplog.text
    assert "invalid-symbol" not in caplog.text


def test_no_http_budget_skip_releases_code_reservation_and_preserves_order(clock):
    from app.source_execution import SourceNotStartedError
    class NoRequestSource(Source):
        def profile(self, code, *, budget_seconds):
            if code == '510050':
                raise SourceNotStartedError('quota wait ended')
            return rows(code)
    client = RedisClient()
    result = collect_profiles(NoRequestSource(clock), client, ['SH510050', 'SZ159919'])
    assert list(result['sourceStatus']) == ['SH510050','SZ159919']
    assert result['sourceStatus'] == {'SH510050':'SKIPPED','SZ159919':'OK'}
    assert client.get(INTERVAL_PREFIX+'SH510050') is None
    assert client.get(INTERVAL_PREFIX+'SZ159919') is not None
    assert [item['symbol'] for item in result['profiles']] == ['SZ159919']
