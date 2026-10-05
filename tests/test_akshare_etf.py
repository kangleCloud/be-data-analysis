"""AKShare 同花顺资料入口与域名隔离的离线审计。"""

import pandas as pd
import pytest
from queue import Empty

from app.providers.akshare_etf import ALLOWED_HOSTS, AkShareEtfProvider, _worker


class Queue:
    def __init__(self):
        self.value = None

    def put(self, value):
        self.value = value


def test_ths_worker_calls_akshare_with_six_digit_code_and_only_audited_host(monkeypatch):
    import akshare
    import requests

    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(requests, "get", get)
    def fund_info_ths(symbol):
        assert symbol == "510050"
        requests.get(f"https://fund.10jqka.com.cn/{symbol}/interduce.html", timeout=15)
        return pd.DataFrame([{"字段": "基金代码", "值": symbol}])

    monkeypatch.setattr(akshare, "fund_info_ths", fund_info_ths)
    queue = Queue()
    _worker(queue, "profile", {"symbol": "510050"}, 8)
    assert queue.value == ("ok", [{"字段": "基金代码", "值": "510050"}])
    assert calls[0][1] == {"timeout": 8, "allow_redirects": False}
    assert requests.get is get
    assert ALLOWED_HOSTS["profile"] == {"fund.10jqka.com.cn"}
    assert not {"sse_scale", "szse_scale"} & ALLOWED_HOSTS.keys()


def test_ths_worker_blocks_unaudited_host_before_network(monkeypatch):
    import akshare
    import requests

    def forbidden_get(*_args, **_kwargs):
        pytest.fail("不得访问未审计域名")
    monkeypatch.setattr(requests, "get", forbidden_get)
    def bad_source(symbol):
        requests.get("https://push2.eastmoney.com/test")
    monkeypatch.setattr(akshare, "fund_info_ths", bad_source)
    queue = Queue()
    _worker(queue, "profile", {"symbol": "510050"}, 8)
    assert queue.value == ("error", "RuntimeError")


def test_provider_passes_remaining_budget_without_old_profile_methods(monkeypatch):
    provider = AkShareEtfProvider()
    calls = []
    monkeypatch.setattr(provider, "_call", lambda *args, **kwargs: calls.append((args, kwargs)))
    provider.profile("510050", budget_seconds=12)
    assert calls == [(("profile",), {"symbol": "510050", "budget_seconds": 12})]
    assert not hasattr(provider, "sse_scale")
    assert not hasattr(provider, "szse_scale")


def test_profile_process_wait_is_capped_by_remaining_batch_budget(monkeypatch):
    waits = []
    class EmptyQueue:
        def get(self, timeout):
            waits.append(timeout)
            raise Empty()
        def close(self):
            pass
    class Process:
        def start(self):
            pass
        def join(self, timeout):
            assert 0 <= timeout <= 2
        def is_alive(self):
            return False
    class Context:
        def Queue(self):
            return EmptyQueue()
        def Process(self, **_kwargs):
            return Process()
    monkeypatch.setattr("app.providers.akshare_etf.mp.get_context", lambda _mode: Context())
    monkeypatch.setattr("app.providers.akshare_etf.time.monotonic", lambda: 0)
    with pytest.raises(TimeoutError):
        AkShareEtfProvider().profile("510050", budget_seconds=10)
    assert waits == [8]
    with pytest.raises(TimeoutError):
        AkShareEtfProvider().profile("510050", budget_seconds=2)
    assert waits == [8]
