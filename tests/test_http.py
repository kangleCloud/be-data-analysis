"""外部 HTTP 共用重试行为测试。"""

import httpx

from app.providers.http import request_json


class _Client:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.call_count = 0

    def get(self, url, **kwargs):
        self.call_count += 1
        return next(self._responses)


def _response(status_code, payload=None):
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", "https://example.test"),
    )


def test_request_json_retries_retryable_status(monkeypatch):
    client = _Client([_response(503), _response(200, {"ok": True})])
    sleeps = []
    monkeypatch.setattr("app.providers.http.time.sleep", sleeps.append)

    payload = request_json(
        client,
        "https://example.test",
        params={},
        retry_count=1,
    )

    assert payload == {"ok": True}
    assert client.call_count == 2
    assert sleeps == [0.3]
