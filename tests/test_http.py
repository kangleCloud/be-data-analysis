"""源请求上限、异常分类和分页进度的离线回归。"""

from http.client import RemoteDisconnected
import requests
import pytest
from tqdm.std import tqdm
from app.providers.http import bounded_timeout, error_metadata, quiet_progress


@pytest.mark.parametrize("original,expected", [
    (None, (5, 15)), (30, (5, 15)), (3, (3, 3)),
    ((None, None), (5, 15)), ((2, 7), (2, 7)),
    ((30, 60), (5, 15)), ((None, 7), (5, 7)), ((2, None), (2, 15)),
])
def test_timeout_bounds_never_widen_shorter_limits(original, expected):
    assert bounded_timeout(original, 15) == expected


def test_error_chain_preserves_remote_disconnect_without_message():
    try:
        try:
            raise RemoteDisconnected("private-url-token")
        except RemoteDisconnected as cause:
            raise requests.ConnectionError("private-url-token") from cause
    except requests.ConnectionError as exc:
        assert error_metadata(exc) == {
            "exception_type": "ConnectionError", "root_type": "RemoteDisconnected",
            "http_status": None, "category": "NETWORK",
        }


def test_progress_is_disabled_and_patch_is_restored(capsys):
    original = tqdm.__dict__.get("__init__")
    with quiet_progress():
        for _ in tqdm(range(3), disable=False, desc="PRIVATE_PROGRESS"):
            pass
    assert tqdm.__dict__.get("__init__") is original
    assert "PRIVATE_PROGRESS" not in capsys.readouterr().err


def test_akshare_standard_progress_factory_is_silent_in_source_scope(capsys):
    from akshare.utils.tqdm import get_tqdm
    with quiet_progress():
        for _ in get_tqdm()(range(8), disable=False, desc='OFFLINE_PAGES'):
            pass
    output = capsys.readouterr()
    assert 'OFFLINE_PAGES' not in output.err and '8/8' not in output.err
