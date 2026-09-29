"""雪球源请求仅使用模拟响应，避免未授权联调访问。"""

import pytest

from app.providers.xueqiu import XueqiuProvider, XueqiuSourceError


class Response:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP 失败")

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_xueqiu_quote_and_profile_use_token_without_exposing_it():
    session = Session([
        Response(),
        Response({"data": {"quote": {"time": 1790577000000, "current": 10.2}}}),
        Response({"data": {"company": {"industry": "银行"}}}),
    ])
    source = XueqiuProvider("private-token", session=session)
    assert source.quote("SH600000")["current"] == 10.2
    assert source.profile("SH600000")["industry"] == "银行"
    assert len(session.calls) == 3
    assert session.calls[1][1]["params"] == {"symbol": "SH600000"}
    assert "private-token" not in repr(source)


@pytest.mark.parametrize("status", [403, 429])
def test_xueqiu_403_and_429_request_cooldown(status):
    source = XueqiuProvider("private-token", session=Session([
        Response(), Response(status_code=status),
    ]))
    with pytest.raises(XueqiuSourceError) as error:
        source.quote("SH600000")
    assert error.value.cooldown


def test_xueqiu_token_error_requests_cooldown():
    source = XueqiuProvider("private-token", session=Session([
        Response(), Response({"error_code": 400016, "error_description": "token invalid"}),
    ]))
    with pytest.raises(XueqiuSourceError) as error:
        source.quote("SH600000")
    assert error.value.cooldown
