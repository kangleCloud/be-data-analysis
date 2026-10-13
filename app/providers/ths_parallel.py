"""固定AKShare即时资金的两页窗口；连接复用，解析仍由原函数完成。"""

import importlib
import importlib.metadata
import logging
import re
import threading
import time
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

import requests

from app.runtime.source_execution import SourceControl, SourceCall, SourceControlError, RENEW_SECONDS
from app.providers.ths_paging import IndividualPaging

LOGGER = logging.getLogger(__name__)


def ths_signature() -> str:
    """复用原AKShare的ths.js及MiniRacer流程，逐页生成hexin-v。"""
    module = importlib.import_module('akshare.stock_feature.stock_fund_flow')
    js = module.py_mini_racer.MiniRacer()
    try:
        js.eval(module._get_file_content_ths('ths.js'))
        return js.call('v')
    finally:
        # AKShare在Linux使用旧版py-mini-racer，其上下文由对象回收，不提供close。
        close = getattr(js, 'close', None)
        if callable(close):
            close()


class SlidingIndividualPages:
    """两条固定线程各独占一个Session；请求中和未消费响应合计最多两页。"""

    def __init__(self, paging: IndividualPaging, send: Any, control: SourceControl,
                 call: SourceCall, keys: tuple[str, str] | None, token: str,
                 guard: tuple[str, str] | None, deadline: float,
                 *, signature_factory=ths_signature) -> None:
        if call.group != 'ths' or importlib.metadata.version('akshare') != '1.18.97':
            raise SourceControlError('资金分页适配仅支持THS组AKShare 1.18.97')
        self.paging, self.physical_send, self.control, self.call = paging, send, control, call
        self.keys, self.token, self.guard, self.deadline = keys, token, guard, deadline
        self.signature_factory = signature_factory
        self.pools = [ThreadPoolExecutor(max_workers=1, thread_name_prefix=f'ths-page-{lane}')
                      for lane in range(2)]
        self.sessions = [None, None]
        self.cancelled = threading.Event()
        self.pending = {}
        self.last_response = None
        self.extra_acquired = 0
        self.quota_seconds = 0.0
        self.peak_window = 0

    @staticmethod
    def _discard(response):
        response.close()
        response._content = b''

    def _retire_response(self):
        if self.last_response is not None:
            # 原函数开始下一次请求时，上页已经解析，清除旧r仍引用的HTML。
            self._discard(self.last_response)
            self.last_response = None

    def _run(self, lane, request, options, keys, quota_token, sign):
        stop, failed = threading.Event(), threading.Event()
        response = None

        def renew():
            while not stop.wait(RENEW_SECONDS):
                try:
                    self.control.check(keys, quota_token, self.guard)
                    if keys is not None:
                        self.control.renew(keys, quota_token)
                except Exception:
                    failed.set()
                    return

        renewal = threading.Thread(target=renew, daemon=True) if lane == 1 else None
        if renewal is not None:
            renewal.start()
        try:
            self._check()
            if sign:
                request.headers['hexin-v'] = self.signature_factory()
            if self.sessions[lane] is None:
                self.sessions[lane] = requests.Session()
            response = self.physical_send(self.sessions[lane], request, options,
                                          quota_keys=keys, quota_token=quota_token, cancelled=self.cancelled)
            self.control.check(keys, quota_token, self.guard)
            self._check()
            if failed.is_set():
                raise SourceControlError('资金分页控制失效')
            return response
        except BaseException:
            if response is not None:
                self._discard(response)
            raise
        finally:
            if renewal is not None:
                stop.set()
                renewal.join(timeout=1.2)
            if lane == 1 and keys is not None:
                self.control.release(keys, quota_token)

    def _check(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise SourceControlError('资金分页已取消或超过预算')

    def _submit(self, page, lane, request, options, *, sign):
        keys, quota_token = self.keys, self.token
        if lane == 1:
            quota_token = self.token + ':page'
            started = time.monotonic()
            keys = self.control.acquire('ths', quota_token) if self.call.mode == 'auto' else None
            self.quota_seconds += time.monotonic() - started
            if self.call.mode == 'auto' and keys is None:
                LOGGER.info('资金分页无额外配额，串行继续 page=%d', page)
                return False
            self.extra_acquired += int(keys is not None)
        try:
            prepared = request.copy()
            if page:
                url = urlsplit(prepared.url)
                prepared.url = urlunsplit(url._replace(
                    path=re.sub(r'/page/\d+/', f'/page/{page}/', url.path)))
            future = self.pools[lane].submit(self._run, lane, prepared,
                                            {**options, 'stream': False, 'allow_redirects': False},
                                            keys, quota_token, sign)
        except BaseException:
            if lane == 1 and keys is not None:
                self.control.release(keys, quota_token)
            raise
        self.pending[page] = (future, lane)
        self.peak_window = max(self.peak_window, len(self.pending))
        LOGGER.info('资金分页提交 page=%d channel=%d transport=%s window=%d',
                    page, lane, 'parallel' if len(self.pending) == 2 else 'serial', len(self.pending))
        return True

    def send(self, page: int, session: requests.Session | None,
             request: requests.PreparedRequest, options: dict[str, Any]) -> requests.Response:
        self._retire_response()
        self._check()
        if page not in self.pending:
            if self.pending:
                self.paging.fail('MISSING_OR_OUT_OF_ORDER_PAGE')
            self._submit(page, 0, request, options, sign=False)
        # 消费前页后窗口空出一格；当前页即使尚未完成也可补下一页。
        if page and page < self.paging.total and len(self.pending) < 2:
            busy_lanes = {lane for _, lane in self.pending.values()}
            lane = 1 if 0 in busy_lanes else 0
            self._submit(page+1, lane, request, options, sign=True)
        future, lane = self.pending[page]
        response = future.result()
        del self.pending[page]
        self.last_response = response
        LOGGER.info('资金分页按页交付 page=%d channel=%d transport=%s buffered=%d',
                    page, lane, 'parallel' if self.pending else 'serial', len(self.pending))
        return response

    def _close_session(self, lane):
        if self.sessions[lane] is not None:
            self.sessions[lane].close()
            self.sessions[lane] = None

    def close(self) -> None:
        self.cancelled.set()
        # 不取消已获额外名额的工作项，保证其finally释放；Session在所属通道关闭。
        closes = [pool.submit(self._close_session, lane) for lane, pool in enumerate(self.pools)]
        for pool in self.pools:
            pool.shutdown(wait=True)
        for future, _ in self.pending.values():
            try:
                response = future.result()
            except Exception:
                pass
            else:
                self._discard(response)
        self.pending.clear()
        self._retire_response()
        for future in closes:
            future.result()
        LOGGER.info('资金分页窗口汇总 pages=%s extraAcquired=%d quotaWait=%.3f peakWindow=%d',
                    self.paging.total, self.extra_acquired, self.quota_seconds, self.peak_window)
