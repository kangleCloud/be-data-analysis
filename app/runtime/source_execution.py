"""独立源进程、跨入口配额与 HTTP 启动间隔；业务写入始终由调用父进程完成。"""

import logging
import multiprocessing as mp
import os
import threading
import time
from contextlib import contextmanager
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import copy
from dataclasses import dataclass, field
from queue import Empty
from typing import Any, Callable, Iterator
from uuid import uuid4
from urllib.parse import urlparse

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.runtime.gates import collection_entry, check_entry, entry_guard
from app.runtime.cooldown import protected_keys, record, risk_rejection
from app.runtime.resources import SourceResourceError
from app.providers.ths_paging import IndividualPaging
from app.providers.http import bounded_timeout, error_metadata, quiet_progress

LOGGER = logging.getLogger(__name__)
PREFIX = "stock:source-control:v1:"
GLOBAL_LIMIT = 6
SOURCE_LIMITS = {"ths": 3, "xq": 3, "sina": 2, "sse": 2, "szse": 2, "bse": 2}
LEASE_SECONDS = 30
RENEW_SECONDS = 10
CLEANUP_SECONDS = 2
GROUPS = {"ths", "sina", "xq", "sse", "szse", "bse"}
ACQUIRE_SCRIPT = """
local g, s = nil, nil
for i=1,tonumber(ARGV[3]) do if redis.call('exists', KEYS[i]) == 0 then g=i; break end end
for i=tonumber(ARGV[3])+1,#KEYS do if redis.call('exists', KEYS[i]) == 0 then s=i; break end end
if not g or not s then return {} end
redis.call('set', KEYS[g], ARGV[1], 'EX', ARGV[2])
redis.call('set', KEYS[s], ARGV[1], 'EX', ARGV[2])
return {g,s}
"""
RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) ~= ARGV[1] or redis.call('get', KEYS[2]) ~= ARGV[1] then return 0 end
redis.call('expire', KEYS[1], ARGV[2]); redis.call('expire', KEYS[2], ARGV[2]); return 1
"""
RELEASE_SCRIPT = """
for i=1,2 do if redis.call('get', KEYS[i]) == ARGV[1] then redis.call('del', KEYS[i]) end end
return 1
"""
TASK_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('expire', KEYS[1], ARGV[2])
"""
RATE_SCRIPT = """
if ARGV[6] ~= 'manual' and (redis.call('get', KEYS[1]) ~= ARGV[1] or redis.call('get', KEYS[2]) ~= ARGV[1]) then return {-1,0} end
if ARGV[4] ~= '' and redis.call('get', KEYS[5]) ~= ARGV[4] then return {-1,0} end
for i=8+tonumber(ARGV[7]),#KEYS do if redis.call('exists', KEYS[i]) == 1 then return {-2,redis.call('ttl', KEYS[i])} end end
local interval = tonumber(ARGV[5])
local owner = interval > 0 and redis.call('get', KEYS[6]) or false
if owner and owner ~= ARGV[8] then return {-3,redis.call('pttl', KEYS[6])} end
if interval > 0 then
  for i=8,7+tonumber(ARGV[7]) do
    local batch_owner=redis.call('get',KEYS[i])
    if batch_owner and batch_owner ~= ARGV[4] then return {-3,redis.call('pttl',KEYS[i])} end
  end
end
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
local a=tonumber(redis.call('get', KEYS[3]) or '0')
local b=tonumber(redis.call('get', KEYS[4]) or '0')
local remaining=math.max(a,b)-now
if remaining > 0 then return {0,remaining} end
local elapsed=-1
if interval > 0 and not owner then
  local previous=redis.call('get',KEYS[7])
  if previous then elapsed=now-tonumber(previous) end
  redis.call('set',KEYS[7],now,'EX',86400)
  redis.call('set', KEYS[6], ARGV[8], 'EX', interval)
  for i=8,7+tonumber(ARGV[7]) do
    if not redis.call('get',KEYS[i]) then redis.call('set',KEYS[i],ARGV[4],'EX',interval) end
  end
end
redis.call('set', KEYS[3], now+tonumber(ARGV[2]), 'PX', 30000)
redis.call('set', KEYS[4], now+tonumber(ARGV[3]), 'PX', 30000)
return {1,0,elapsed}
"""


class SourceControlError(RuntimeError):
    """配额、业务锁或 Redis 控制失效；停止整轮而不是继续源请求。"""


class SourceBusyError(SourceControlError):
    """其他采集入口占用资源，立即拒绝。"""


class SourceCoolingError(RuntimeError):
    def __init__(self, ttl: int) -> None:
        self.ttl = ttl
        super().__init__("数据源冷却中")


class SourceNotStartedError(TimeoutError):
    """尚未发起实际 HTTP 请求即耗尽预算；跳过而不是源失败。"""


class SourceThrottledError(RuntimeError):
    def __init__(self, ttl: int) -> None:
        self.ttl = ttl
        super().__init__("当前代码请求间隔未满足")


class SourceCallError(RuntimeError):
    """跨进程仅传递可脱敏的异常类别，不传原始响应或 Token。"""
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.exception_type = metadata["exception_type"]
        self.root_type = metadata["root_type"]
        self.http_status = metadata["http_status"]
        self.category = metadata["category"]
        self.reason = metadata.get("reason")
        self.fields = metadata.get("fields", ())
        self.code = metadata.get("code")
        self.bad_rows = metadata.get("bad_rows", 0)
        super().__init__(f"type={self.exception_type} root={self.root_type} "
                         f"http={self.http_status} category={self.category}")

    def diagnostic(self):
        return f"reason={self.reason} fields={','.join(self.fields) or '-'} code={self.code or '-'} badRows={self.bad_rows}"


@dataclass(frozen=True)
class SourceCall:
    function: str
    group: str
    parameters: dict[str, Any] = field(default_factory=dict, repr=False)
    budget_seconds: float = 32
    domains: tuple[str, ...] = ()
    cooldown_keys: tuple[str, ...] = ()
    cooldown_policy: str = "none"
    fund_profile: bool = False
    interval_key: str | None = None
    interval_seconds: int = 120
    mode: str = "auto"

    batch_interval_keys: tuple[str, ...] = ()
    entry_lane: str | None = None


class SourceControl:
    def __init__(self, client: Any) -> None:
        self.client = client

    def acquire(self, group: str, token: str) -> tuple[str, str] | None:
        keys = [f"{PREFIX}slot:global:{i}" for i in range(GLOBAL_LIMIT)] + [
            f"{PREFIX}slot:{group}:{i}" for i in range(SOURCE_LIMITS[group])]
        result = self.client.eval(ACQUIRE_SCRIPT, len(keys), *keys, token, LEASE_SECONDS, GLOBAL_LIMIT)
        return (keys[result[0]-1], keys[result[1]-1]) if result else None

    def release_extra(self, group: str, token: str) -> None:
        # 源进程被TERM/KILL时，其分页线程来不及finally；回收后才清除额外名额。
        keys = [f"{PREFIX}slot:global:{i}" for i in range(GLOBAL_LIMIT)] + [
            f"{PREFIX}slot:{group}:{i}" for i in range(SOURCE_LIMITS[group])]
        self.client.eval("for i=1,#KEYS do if redis.call('get',KEYS[i]) == ARGV[1] then redis.call('del',KEYS[i]) end end return 1",
                         len(keys), *keys, token+":page")

    def renew(self, keys: tuple[str, str], token: str) -> None:
        if not self.client.eval(RENEW_SCRIPT, 2, *keys, token, LEASE_SECONDS):
            raise SourceControlError("源调用配额已失效")

    def release(self, keys: tuple[str, str], token: str) -> None:
        self.client.eval(RELEASE_SCRIPT, 2, *keys, token)

    def check(self, keys: tuple[str, str] | None, token: str,
              guard: tuple[str, str] | None = None) -> None:
        if keys is None:
            self.client.get(f"{PREFIX}health")
        elif any(self.client.get(key) != token for key in keys):
            raise SourceControlError("源调用配额已失效")
        if guard is not None and self.client.get(guard[0]) != guard[1]:
            raise SourceControlError("业务任务锁已失效")

    def request_turn(self, call: SourceCall, keys: tuple[str, str] | None, token: str,
                     guard: tuple[str, str] | None, deadline: float, *, sampling_token: str | None = None, first_request: bool = True) -> None:
        interval = 0.2 if call.group == "sina" else 1
        rate_keys = [f"{PREFIX}rate:{call.group}",
                     f"{PREFIX}rate:ths:fund" if call.fund_profile else f"{PREFIX}rate:{call.group}"]
        all_keys = [*(keys or (f"{PREFIX}no-slot", f"{PREFIX}no-slot")), *rate_keys, guard[0] if guard else f"{PREFIX}no-guard",
                    call.interval_key or f"{PREFIX}no-interval",
                    f"{PREFIX}sample-time:{call.interval_key or call.function}",
                    *call.batch_interval_keys, *protected_keys(call.cooldown_keys, call.group, call.mode)]
        interval_wait_logged = False
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("源调用预算已耗尽")
            result = self.client.eval(RATE_SCRIPT, len(all_keys), *all_keys, token,
                                             int(interval * 1000), 2000 if call.fund_profile else int(interval*1000),
                                             guard[1] if guard else "", call.interval_seconds if call.interval_key and call.mode == "auto" and first_request else 0, call.mode, len(call.batch_interval_keys), sampling_token or token)
            status, value = result[:2]
            if status == -1:
                raise SourceControlError("源配额或业务锁已失效")
            if status == -2:
                raise SourceCoolingError(value)
            if status == -3:
                if value > 5000 or value <= 0:
                    LOGGER.info("限频跳过 function=%s remainingMs=%d", call.function, value)
                    raise SourceThrottledError(max(0, (value+999)//1000))
                if not interval_wait_logged:
                    LOGGER.info("限频等待 function=%s remainingMs=%d", call.function, value)
                    interval_wait_logged = True
                time.sleep(min(0.1, value/1000, max(0, deadline-time.monotonic())))
                continue
            if status == 1:
                if len(result) > 2 and result[2] >= 0:
                    LOGGER.info("有效HTTP采样间隔 function=%s seconds=%.3f", call.function, result[2]/1000)
                return
            time.sleep(min(0.1, value / 1000, max(0, deadline-time.monotonic())))

    def cool(self, call: SourceCall, metadata: dict[str, Any]) -> None:
        keys = call.cooldown_keys if call.cooldown_policy in {"market", "etf"} else ()
        record(self.client, keys, call.group, call.mode, metadata)


def market_cooldown(metadata: dict[str, Any]) -> tuple[bool, int]:
    """只有明确风控拒绝才暂停共享源；普通失败仅暂停当前市场模块。"""
    shared = risk_rejection(metadata)
    return shared, 7200 if shared else 300


def _client(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=1,
                                socket_connect_timeout=1, retry=Retry(NoBackoff(), 0))


@contextmanager
def controlled_http(control: SourceControl, call: SourceCall, keys: tuple[str, str] | None, token: str,
                    guard: tuple[str, str] | None, deadline: float, read_seconds: float,
                    started_event: Any = None) -> Iterator[None]:
    """Session.send 覆盖 get/post、雪球会话、重定向，每次物理 HTTP 都先取共享速率许可。"""
    import requests
    original = requests.Session.send
    paging = IndividualPaging(call)
    first_started = threading.Event()
    paired = None

    def physical_send(session, request, kwargs, *, quota_keys=keys, quota_token=token):
        if urlparse(request.url).hostname not in call.domains:
            raise SourceControlError("源请求域名不在审计清单中")
        control.request_turn(call, quota_keys, quota_token, guard, deadline,
                             sampling_token=token, first_request=not first_started.is_set())
        first_started.set()
        remaining = max(0.001, deadline-time.monotonic())
        options = dict(kwargs)
        connect, read = bounded_timeout(options.get("timeout"), read_seconds)
        options["timeout"] = (min(connect, remaining), min(read, remaining))
        if call.fund_profile or paging.enabled:
            options["allow_redirects"] = False
        try:
            if started_event is not None:
                started_event.set()
            response = original(session, request, **options)
            response.raise_for_status()
            return response
        except SourceResourceError:
            raise
        except Exception as exc:
            control.cool(call, error_metadata(exc))
            raise

    if paging.enabled:
        from app.providers.ths_parallel import PairedIndividualPages
        paired = PairedIndividualPages(paging, physical_send, control, call, keys, token, guard, deadline)

    def send(session: Any, request: Any, **kwargs: Any) -> Any:
        page = paging.prepare(request)
        response = paired.send(page, session, request, kwargs) if paired is not None else physical_send(session, request, kwargs)
        try:
            paging.response(page, response)
        except Exception as exc:
            control.cool(call, error_metadata(exc))
            raise
        return response

    requests.Session.send = send
    try:
        yield paging
    finally:
        try:
            if paired is not None:
                paired.close()
        finally:
            requests.Session.send = original


def _source_worker(queue: Any, url: str, call: SourceCall, keys: tuple[str, str] | None, token: str,
                   guard: tuple[str, str] | None, deadline: float, read_seconds: float, parent_pid: int,
                   started_event: Any) -> None:
    client = _client(url)
    control = SourceControl(client)
    stopped = threading.Event()
    def watchdog() -> None:
        while not stopped.wait(0.5):
            try:
                if os.getppid() != parent_pid:
                    os._exit(77)
                if time.monotonic() >= deadline:
                    os._exit(76)
                control.check(keys, token, guard)
            except redis.RedisError:
                os._exit(73)
            except SourceControlError:
                os._exit(74)
            except Exception:
                os._exit(70)
    watcher = threading.Thread(target=watchdog, daemon=True)
    watcher.start()
    try:
        # 初始化、等待和清理均在父进程确定的同一个截止时间内。
        initialized = time.monotonic()
        from app.core.logging import configure_logging
        configure_logging("info", "source:"+call.function)
        import akshare
        LOGGER.info("源初始化耗时 %.2f 秒", time.monotonic()-initialized)
        os.environ["TZ"] = "Asia/Shanghai"
        time.tzset()
        with controlled_http(control, call, keys, token, guard, deadline, read_seconds, started_event) as paging, quiet_progress():
            function = getattr(akshare, call.function)
            clear = getattr(function, "cache_clear", None)
            if clear:
                clear()
            fetched = time.monotonic()
            frame = function(**call.parameters)
            LOGGER.info("源请求耗时 %.2f 秒", time.monotonic()-fetched)
            serialized = time.monotonic()
            rows = frame.to_dict("records")
            paging.finish(rows)
            del frame
            LOGGER.info("源转换耗时 %.2f 秒 rows=%d",time.monotonic()-serialized,len(rows))
        queue.put(("ok", rows))
    except SourceThrottledError as exc:
        queue.put(("throttled", exc.ttl))
    except SourceCoolingError as exc:
        queue.put(("cooldown", exc.ttl))
    except SourceControlError:
        queue.put(("control", None))
    except redis.RedisError:
        queue.put(("control", None))
    except Exception as exc:
        metadata = error_metadata(exc)
        # AKShare 的 APIError/NetworkError 可能没有 requests.response。
        if isinstance(getattr(exc, "status_code", None), int):
            metadata["http_status"] = exc.status_code
            metadata["category"] = "HTTP_REJECTED" if exc.status_code in {401,403,429} else "HTTP_ERROR"
        if type(exc).__name__ == "APIError" and any(word in str(exc).lower() for word in ("token", "login")):
            metadata["category"] = "AUTH_REJECTED"
        if type(exc).__name__ == "NetworkError":
            metadata["category"] = "NETWORK"
        try:
            if started_event.is_set() or metadata["category"] != "TIMEOUT":
                control.cool(call, metadata)
        except redis.RedisError:
            queue.put(("control", None))
        else:
            queue.put(("error", metadata))
    finally:
        stopped.set()
        watcher.join(timeout=0.6)
        client.close()


class SourceExecutor:
    """父进程等待/续租/回收独立 spawn 源进程，不写任何业务快照。"""
    def __init__(self, redis_url: str, read_seconds: float = 15, *, client: Any = None,
                 context: Any = None, lane: str = "reference", mode: str = "auto", worker: Callable[..., None] = _source_worker) -> None:
        if mode not in {"auto", "manual"}:
            raise ValueError("采集模式必须为 auto 或 manual")
        self.redis_url, self.read_seconds = redis_url, read_seconds
        self.lane, self.mode = lane, mode
        self.control = SourceControl(client if client is not None else _client(redis_url))
        self.context = context or mp.get_context("spawn")
        self.worker = worker
        self.cancelled = threading.Event()
        self.parent_cancel: threading.Event | None = None
        self.guard: tuple[str, str] | None = None
        self.deadline: float | None = None

    @classmethod
    def configured(cls, read_seconds: float | None = None, *, lane: str = "reference", mode: str = "auto") -> "SourceExecutor":
        from app.core.config import get_settings
        settings = get_settings()
        return cls(settings.redis_url.get_secret_value(),
                   settings.source_timeout_seconds if read_seconds is None else read_seconds, lane=lane, mode=mode)

    @contextmanager
    def batch(self, guard: tuple[str, str] | None = None,
              deadline: float | None = None) -> Iterator[None]:
        with collection_entry(self.control.client, lane=self.lane, mode=self.mode) as acquired:
            if not acquired:
                raise SourceBusyError('其他采集任务正在运行')
            with self._batch_locked(guard,deadline):
                yield

    @contextmanager
    def _batch_locked(self, guard: tuple[str, str] | None = None,
              deadline: float | None = None) -> Iterator[None]:
        if self.mode == "manual":
            guard = None
        self.cancelled.clear()
        self.guard, self.deadline = guard, deadline
        stop_renewal = threading.Event()
        thread = None
        if guard is not None:
            seconds = self.control.client.ttl(guard[0])
            if seconds <= 0 or self.control.client.get(guard[0]) != guard[1]:
                raise SourceControlError("业务任务锁已失效")
            def renew_task() -> None:
                while not stop_renewal.wait(RENEW_SECONDS):
                    try:
                        if not self.control.client.eval(TASK_RENEW_SCRIPT, 1, guard[0], guard[1], seconds):
                            self.cancelled.set()
                            return
                    except Exception:
                        self.cancelled.set()
                        return
            thread = threading.Thread(target=renew_task, daemon=True)
            thread.start()
        try:
            yield
        finally:
            self.cancelled.set()
            stop_renewal.set()
            if thread is not None:
                thread.join(timeout=1.2)
            self.guard, self.deadline = None, None

    def fork(self) -> "SourceExecutor":
        """在途调用独立取消状态，仅读取父批次的锁和截止时间。"""
        executor = SourceExecutor(self.redis_url, self.read_seconds, client=self.control.client,
                                  context=self.context, lane=self.lane, mode=self.mode, worker=self.worker)
        executor.guard, executor.deadline = self.guard, self.deadline
        executor.parent_cancel = self.cancelled
        return executor

    def stop(self) -> None:
        self.cancelled.set()

    def _check(self) -> None:
        check_entry()
        if self.cancelled.is_set() or (self.parent_cancel is not None and self.parent_cancel.is_set()):
            raise SourceControlError("源批次已取消")
        if self.guard and self.control.client.get(self.guard[0]) != self.guard[1]:
            raise SourceControlError("业务任务锁已失效")

    def call(self, call: SourceCall) -> Any:
        with collection_entry(self.control.client, lane=call.entry_lane or self.lane, mode=self.mode) as acquired:
            if not acquired:
                raise SourceBusyError('其他采集任务正在运行')
            return self._call_locked(call)

    def _call_locked(self, call: SourceCall) -> Any:
        if call.mode != self.mode:
            raise ValueError("Provider 与源执行器采集模式不一致")
        if call.group not in GROUPS:
            raise ValueError("未知来源组")
        guard = (self.guard or entry_guard(call.entry_lane or self.lane)) if self.mode == "auto" else None
        started = time.monotonic()
        deadline = started + call.budget_seconds
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        request_deadline = deadline - CLEANUP_SECONDS
        token = uuid4().hex
        keys = None
        queue = process = None
        exitcode = None
        requested = self.context.Event()
        try:
            quota_wait_logged = False
            while keys is None:
                self._check()
                if time.monotonic() >= request_deadline:
                    raise SourceNotStartedError("等待源配额超过预算，尚未发起请求")
                for key in protected_keys(call.cooldown_keys, call.group, call.mode):
                    if self.control.client.exists(key):
                        raise SourceCoolingError(self.control.client.ttl(key))
                if self.mode == "manual":
                    break
                keys = self.control.acquire(call.group, token)
                if keys is None:
                    if not quota_wait_logged:
                        LOGGER.info("配额等待 function=%s group=%s", call.function, call.group)
                        quota_wait_logged = True
                    time.sleep(min(0.1, max(0, request_deadline-time.monotonic())))
            queue = self.context.Queue()
            process = self.context.Process(target=self.worker, args=(queue, self.redis_url, call, keys, token,
                guard, request_deadline, self.read_seconds, os.getpid(), requested), daemon=True)
            process.start()
            renewed = time.monotonic()
            while True:
                self._check()
                self.control.check(keys, token, guard)
                now = time.monotonic()
                if now >= request_deadline:
                    if not requested.is_set():
                        raise SourceNotStartedError("源预算结束，尚未发起请求")
                    raise TimeoutError("源调用超过预算")
                if keys is not None and now - renewed >= RENEW_SECONDS:
                    self.control.renew(keys, token)
                    renewed = now
                try:
                    status, result = queue.get(timeout=min(0.1, request_deadline-now))
                    break
                except Empty:
                    if not process.is_alive():
                        if time.monotonic() >= request_deadline:
                            if not requested.is_set():
                                raise SourceNotStartedError("源预算结束，尚未发起请求")
                            raise TimeoutError("源调用超过预算")
                        if process.exitcode == 76:
                            if not requested.is_set():
                                raise SourceNotStartedError("源预算结束，尚未发起请求")
                            raise TimeoutError("源调用超过预算")
                        if process.exitcode in {73,74,77}:
                            raise SourceControlError("源控制失效 exitcode="+str(process.exitcode))
                        raise SourceResourceError(exitcode=process.exitcode)
            self._check()
            self.control.check(keys, token, guard)
            if status == "throttled":
                raise SourceThrottledError(result)
            if status == "cooldown":
                raise SourceCoolingError(result)
            if status == "control":
                raise SourceControlError("源请求控制失效")
            if status != "ok":
                if result["category"] == "TIMEOUT" and not requested.is_set():
                    raise SourceNotStartedError("源预算结束，尚未发起请求")
                raise SourceCallError(result)
            return result
        except BaseException as exc:
            log = LOGGER.info if isinstance(exc, (SourceThrottledError, SourceNotStartedError, SourceCoolingError)) else LOGGER.warning
            log("源取消 function=%s reason=%s elapsed=%.2f exitcode=%s",call.function,getattr(exc,"reason",type(exc).__name__),time.monotonic()-started,process.exitcode if process else None)
            if process is not None:
                self._reap(process, deadline)
                exitcode = process.exitcode
                process.close()
                process = None
            raise
        finally:
            if process is not None:
                self._reap(process, deadline)
                exitcode = process.exitcode
                process.close()
            if queue is not None:
                queue.close()
            if call.function == "stock_fund_flow_individual" and call.parameters.get("symbol") == "即时" and self.mode == "auto":
                self.control.release_extra(call.group, token)
            if keys is not None:
                self.control.release(keys, token)
            LOGGER.info("源 %s 总耗时 %.2f 秒 exitcode=%s",call.function,time.monotonic()-started,exitcode)

    @staticmethod
    def _reap(process: Any, deadline: float) -> None:
        if getattr(process, "pid", None) is None:
            return
        deadline = min(deadline, time.monotonic() + CLEANUP_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join(timeout=max(0, deadline-time.monotonic()-1))
        if process.is_alive():
            process.kill()
            process.join(timeout=max(0, deadline-time.monotonic()))
        if process.is_alive():
            raise SourceControlError("源进程未完成回收，拒绝释放名额")
        process.join(timeout=0)
        LOGGER.info("源回收 exitcode=%s signal=%s",process.exitcode,-process.exitcode if process.exitcode is not None and process.exitcode < 0 else None)


@contextmanager
def source_batch(source: Any, guard: tuple[str, str] | None = None,
                 deadline: float | None = None) -> Iterator[None]:
    executor = getattr(source, "executor", None)
    if executor is None:
        yield
    else:
        with executor.batch(guard, deadline):
            yield


def completed(actions: list[tuple[str, str, Callable[..., Any]]], *, source: Any = None,
              max_workers: int = 1, ready: Callable[[str], float] | None = None,
              ready_deadline: float | None = None, on_skip: Callable[[str], None] | None = None,
              ) -> Iterator[tuple[str, Any, Exception | None, float]]:
    """最多三项在途；到期筛选和短等待在父流程进行，不占源配额。"""
    if max_workers == 1:
        for key, group, function in actions:
            executor = getattr(source, 'executor', None)
            try:
                if executor is not None:
                    executor._check()
                value = function()
            except (SourceControlError, redis.RedisError):
                raise
            except Exception as exc:
                yield key, None, exc, time.monotonic()
            else:
                yield key, value, None, time.monotonic()
        return
    if max_workers not in {2, 3}:
        raise ValueError("只支持一、二或三项在途")
    pending = {}
    waiting = list(actions)
    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='source-wait')
    wait_logged = False
    eligible = set()
    first_scan = True

    def submit_next():
        for index, (key, group, function) in enumerate(waiting):
            if ready is None or (key in eligible and ready(key) <= time.monotonic()):
                waiting.pop(index)
                break
        else:
            return False
        isolated = copy(source)
        parent = getattr(source, 'executor', None)
        executor = parent.fork() if parent is not None else None
        if executor is not None:
            isolated.executor = executor
        def invoke():
            try:
                if executor is not None:
                    executor._check()
                value = function(isolated)
            except (SourceControlError, redis.RedisError):
                raise
            except Exception as exc:
                return key, None, exc, time.monotonic()
            return key, value, None, time.monotonic()
        future = pool.submit(copy_context().run, invoke)
        pending[future] = executor
        return True

    try:
        while waiting or pending:
            check_entry()
            parent = getattr(source, 'executor', None)
            if parent is not None:
                parent._check()
            now = time.monotonic()
            if ready is not None:
                # 记录60秒内到期的项，即使三个并发位一直忙也不丢失其资格。
                for key, _, _ in waiting:
                    if first_scan or ready_deadline is None or now <= ready_deadline:
                        due_at = ready(key)
                        if ready_deadline is None or due_at <= ready_deadline or (first_scan and due_at <= now):
                            eligible.add(key)
                        else:
                            eligible.discard(key)
                if ready_deadline is not None and now > ready_deadline:
                    skipped = [item for item in waiting if item[0] not in eligible]
                    waiting = [item for item in waiting if item[0] in eligible]
                    for key, _, _ in skipped:
                        if on_skip is not None:
                            on_skip(key)
            first_scan = False
            while len(pending) < max_workers and submit_next():
                pass
            if pending:
                done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.pop(future)
                    yield future.result()
            elif waiting:
                if ready_deadline is None or time.monotonic() >= ready_deadline:
                    for key, _, _ in waiting:
                        if on_skip is not None:
                            on_skip(key)
                    break
                if not wait_logged:
                    LOGGER.info("父流程等待股票到期 remainingBudget=%.2f",ready_deadline-time.monotonic())
                    wait_logged = True
                time.sleep(min(0.1, max(0, ready_deadline-time.monotonic())))
    finally:
        for future, executor in pending.items():
            if executor is not None:
                executor.stop()
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
