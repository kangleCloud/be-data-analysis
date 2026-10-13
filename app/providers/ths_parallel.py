"""固定AKShare即时资金函数的相邻页传输适配；解析仍由原函数完成。"""

import importlib
import importlib.metadata
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

import requests

from app.runtime.source_execution import SourceControlError, RENEW_SECONDS

LOGGER = logging.getLogger(__name__)


def ths_signature():
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


class PairedIndividualPages:
    def __init__(self, paging, send, control, call, keys, token, guard, deadline,
                 *, signature_factory=ths_signature):
        if call.group != 'ths':
            raise SourceControlError('即时资金分页来源组必须为ths')
        if importlib.metadata.version('akshare') != '1.18.97':
            raise SourceControlError('资金分页适配仅支持AKShare 1.18.97')
        self.paging, self.physical_send, self.control, self.call = paging, send, control, call
        self.keys, self.token, self.guard, self.deadline = keys, token, guard, deadline
        self.signature_factory = signature_factory
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ths-page')
        self.cancelled = threading.Event()
        self.pending = None
        self.pending_page = None
        self.last_response = None

    def _retire_response(self):
        if self.last_response is not None:
            # 下次原函数请求时，上页已由AKShare解析；清掉旧r仍引用的原始HTML。
            self.last_response.close()
            self.last_response._content = b''
            self.last_response = None

    def _prefetch(self, request, options, keys, extra_token):
        stop, failed = threading.Event(), threading.Event()
        def renew():
            while not stop.wait(RENEW_SECONDS):
                try:
                    self.control.check(keys, extra_token, self.guard)
                    if keys is not None:
                        self.control.renew(keys, extra_token)
                except Exception:
                    failed.set()
                    return
        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                raise SourceControlError('资金分页已取消或超过预算')
            request.headers['hexin-v'] = self.signature_factory()
            with requests.Session() as session:
                response = self.physical_send(session, request, options, quota_keys=keys, quota_token=extra_token)
            self.control.check(keys, extra_token, self.guard)
            if self.cancelled.is_set() or failed.is_set() or time.monotonic() >= self.deadline:
                response.close()
                response._content = b''
                raise SourceControlError('资金分页控制失效')
            return response
        finally:
            stop.set()
            thread.join(timeout=1.2)
            if keys is not None:
                self.control.release(keys, extra_token)

    def send(self, page, session, request, options):
        self._retire_response()
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise SourceControlError('资金分页已取消或超过预算')
        if self.pending is not None:
            if page != self.pending_page:
                self.paging.fail('MISSING_OR_OUT_OF_ORDER_PAGE')
            future = self.pending
            self.pending, self.pending_page = None, None
            response = future.result()
            LOGGER.info('资金分页按页交付 page=%d transport=paired',page)
        else:
            # 首页仅发现页数；每一对的前页走主调用，后页使用一个额外名额。
            if page and page % 2 == 1 and page < self.paging.total:
                extra_token = self.token+':page'
                extra_keys = self.control.acquire('ths', extra_token) if self.call.mode == 'auto' else None
                if self.call.mode == 'manual' or extra_keys is not None:
                    prepared = request.copy()
                    url = urlsplit(prepared.url)
                    prepared.url = urlunsplit(url._replace(path=re.sub(r'/page/\d+/',f'/page/{page+1}/',url.path)))
                    options = {**options, 'stream': False, 'allow_redirects': False}
                    try:
                        self.pending = self.pool.submit(self._prefetch, prepared, options, extra_keys, extra_token)
                        self.pending_page = page+1
                    except BaseException:
                        if extra_keys is not None:
                            self.control.release(extra_keys, extra_token)
                        raise
                    LOGGER.info('资金相邻页在途 pages=%d,%d signature=akshare-ths-js',page,page+1)
                else:
                    LOGGER.info('资金分页无额外配额，串行继续 page=%d',page)
            response = self.physical_send(session, request, options)
        self.last_response = response
        return response

    def close(self):
        self.cancelled.set()
        if self.pending is not None:
            # 不取消已取得名额的工作项：让它执行finally后再退出。
            self.pool.shutdown(wait=True)
            if not self.pending.cancelled():
                try:
                    response = self.pending.result()
                except Exception:
                    pass
                else:
                    response.close()
                    response._content = b''
            self.pending = None
        else:
            self.pool.shutdown(wait=True)
        self._retire_response()
