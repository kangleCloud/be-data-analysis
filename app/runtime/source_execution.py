"""独立源进程、跨入口配额与 HTTP 启动间隔；业务写入始终由调用父进程完成。"""

import logging
import multiprocessing as mp
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from queue import Empty
from typing import Any, Callable, Iterator
from uuid import uuid4
from urllib.parse import urlparse

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.runtime.gates import collection_entry, check_entry
from app.runtime.resources import SourceResourceError, check_memory, memory_state
from app.providers.ths_paging import IndividualPaging
from app.providers.http import bounded_timeout, error_metadata, quiet_progress

LOGGER = logging.getLogger(__name__)
PREFIX = "stock:source-control:v1:"
GLOBAL_LIMIT = 2
SOURCE_LIMIT = 2
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
if redis.call('get', KEYS[1]) ~= ARGV[1] or redis.call('get', KEYS[2]) ~= ARGV[1] then return {-1,0} end
if ARGV[4] ~= '' and redis.call('get', KEYS[5]) ~= ARGV[4] then return {-1,0} end
for i=7,#KEYS do if redis.call('exists', KEYS[i]) == 1 then return {-2,redis.call('ttl', KEYS[i])} end end
local interval = tonumber(ARGV[5])
local owner = interval > 0 and redis.call('get', KEYS[6]) or false
if owner and owner ~= ARGV[1] then return {-3,redis.call('ttl', KEYS[6])} end
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
local a=tonumber(redis.call('get', KEYS[3]) or '0')
local b=tonumber(redis.call('get', KEYS[4]) or '0')
local remaining=math.max(a,b)-now
if remaining > 0 then return {0,remaining} end
if interval > 0 and not owner then redis.call('set', KEYS[6], ARGV[1], 'EX', interval) end
redis.call('set', KEYS[3], now+tonumber(ARGV[2]), 'PX', 30000)
redis.call('set', KEYS[4], now+tonumber(ARGV[3]), 'PX', 30000)
return {1,0}
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

    @property
    def lane(self):
        return "funds" if self.function == "stock_fund_flow_individual" and self.parameters.get("symbol") == "即时" else "quotes"


class SourceControl:
    def __init__(self, client: Any) -> None:
        self.client = client

    def acquire(self, group: str, token: str) -> tuple[str, str] | None:
        keys = [f"{PREFIX}slot:global:{i}" for i in range(GLOBAL_LIMIT)] + [
            f"{PREFIX}slot:{group}:{i}" for i in range(SOURCE_LIMIT)]
        result = self.client.eval(ACQUIRE_SCRIPT, len(keys), *keys, token, LEASE_SECONDS, GLOBAL_LIMIT)
        return (keys[result[0]-1], keys[result[1]-1]) if result else None

    def renew(self, keys: tuple[str, str], token: str) -> None:
        if not self.client.eval(RENEW_SCRIPT, 2, *keys, token, LEASE_SECONDS):
            raise SourceControlError("源调用配额已失效")

    def release(self, keys: tuple[str, str], token: str) -> None:
        self.client.eval(RELEASE_SCRIPT, 2, *keys, token)

    def check(self, keys: tuple[str, str], token: str,
              guard: tuple[str, str] | None = None) -> None:
        if any(self.client.get(key) != token for key in keys):
            raise SourceControlError("源调用配额已失效")
        if guard is not None and self.client.get(guard[0]) != guard[1]:
            raise SourceControlError("业务任务锁已失效")

    def request_turn(self, call: SourceCall, keys: tuple[str, str], token: str,
                     guard: tuple[str, str] | None, deadline: float) -> None:
        interval = 0.2 if call.group == "sina" else 1
        rate_keys = [f"{PREFIX}rate:{call.group}",
                     f"{PREFIX}rate:ths:fund" if call.fund_profile else f"{PREFIX}rate:{call.group}"]
        all_keys = [*keys, *rate_keys, guard[0] if guard else f"{PREFIX}no-guard",
                    call.interval_key or f"{PREFIX}no-interval", *call.cooldown_keys]
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("源调用预算已耗尽")
            status, value = self.client.eval(RATE_SCRIPT, len(all_keys), *all_keys, token,
                                             int(interval * 1000), 2000 if call.fund_profile else int(interval*1000),
                                             guard[1] if guard else "", call.interval_seconds if call.interval_key else 0)
            if status == -1:
                raise SourceControlError("源配额或业务锁已失效")
            if status == -2:
                raise SourceCoolingError(value)
            if status == -3:
                raise SourceThrottledError(value)
            if status == 1:
                return
            time.sleep(min(0.1, value / 1000, max(0, deadline-time.monotonic())))

    def cool(self, call: SourceCall, metadata: dict[str, Any]) -> None:
        status = metadata["http_status"]
        seconds = None
        key = call.cooldown_keys[0] if call.cooldown_keys else None
        if call.cooldown_policy == "market":
            shared, seconds = market_cooldown(metadata)
            index = 0 if shared else 1
            key = call.cooldown_keys[index] if len(call.cooldown_keys) > index else None
        elif call.cooldown_policy == "etf":
            seconds = 7200 if status in {403, 429} else 300
        elif call.cooldown_policy == "stock" and (status in {401, 403, 429}
                or metadata["exception_type"] == "RateLimitError" or metadata["category"] == "AUTH_REJECTED"):
            seconds = 7200
        if seconds and key:
            self.client.set(key, "1", ex=seconds, nx=True)


def market_cooldown(metadata: dict[str, Any]) -> tuple[bool, int]:
    """只有明确风控拒绝才暂停共享源；普通失败仅暂停当前市场模块。"""
    shared = (metadata["http_status"] in {403, 429}
              or metadata["category"] == "SOURCE_REJECTED"
              or metadata["exception_type"] == "RateLimitError")
    return shared, 7200 if shared else 300


def _client(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=1,
                                socket_connect_timeout=1, retry=Retry(NoBackoff(), 0))


@contextmanager
def controlled_http(control: SourceControl, call: SourceCall, keys: tuple[str, str], token: str,
                    guard: tuple[str, str] | None, deadline: float, read_seconds: float,
                    started_event: Any = None) -> Iterator[None]:
    """Session.send 覆盖 get/post、雪球会话、重定向，每次物理 HTTP 都先取共享速率许可。"""
    import requests
    original = requests.Session.send
    paging = IndividualPaging(call)
    def send(session: Any, request: Any, **kwargs: Any) -> Any:
        if urlparse(request.url).hostname not in call.domains:
            raise SourceControlError("源请求域名不在审计清单中")
        page = paging.prepare(request)
        check_memory()
        control.request_turn(call, keys, token, guard, deadline)
        remaining = max(0.001, deadline - time.monotonic())
        connect, read = bounded_timeout(kwargs.get("timeout"), read_seconds)
        kwargs["timeout"] = (min(connect, remaining), min(read, remaining))
        if call.fund_profile:
            kwargs["allow_redirects"] = False
        try:
            if started_event is not None:
                started_event.set()
            response = original(session, request, **kwargs)
            response.raise_for_status()
            paging.response(page,response)
            return response
        except SourceResourceError:
            raise
        except Exception as exc:
            control.cool(call, error_metadata(exc))
            raise
    requests.Session.send = send
    try:
        yield paging
    finally:
        requests.Session.send = original


def _source_worker(queue: Any, url: str, call: SourceCall, keys: tuple[str, str], token: str,
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
                check_memory()
                control.check(keys, token, guard)
            except SourceResourceError:
                os._exit(72)
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
        check_memory()
        import akshare
        LOGGER.info("源初始化耗时 %.2f 秒 memory=%s", time.monotonic()-initialized, memory_state())
        os.environ["TZ"] = "Asia/Shanghai"
        time.tzset()
        with controlled_http(control, call, keys, token, guard, deadline, read_seconds, started_event) as paging, quiet_progress():
            function = getattr(akshare, call.function)
            clear = getattr(function, "cache_clear", None)
            if clear:
                clear()
            fetched = time.monotonic()
            frame = function(**call.parameters)
            LOGGER.info("源请求耗时 %.2f 秒 memory=%s", time.monotonic()-fetched, memory_state())
            serialized = time.monotonic()
            rows = frame.to_dict("records")
            paging.finish(rows)
            del frame
            LOGGER.info("源转换耗时 %.2f 秒 rows=%d memory=%s",time.monotonic()-serialized,len(rows),memory_state())
        queue.put(("ok", rows))
    except SourceResourceError as exc:
        queue.put(("resource", {"reason":exc.reason,"state":exc.state}))
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
                 context: Any = None, lane: str = "quotes", worker: Callable[..., None] = _source_worker) -> None:
        self.redis_url, self.read_seconds = redis_url, read_seconds
        self.lane = lane
        self.control = SourceControl(client if client is not None else _client(redis_url))
        self.context = context or mp.get_context("spawn")
        self.worker = worker
        self.cancelled = threading.Event()
        self.guard: tuple[str, str] | None = None
        self.deadline: float | None = None

    @classmethod
    def configured(cls, read_seconds: float | None = None) -> "SourceExecutor":
        from app.core.config import get_settings
        settings = get_settings()
        return cls(settings.redis_url.get_secret_value(),
                   settings.source_timeout_seconds if read_seconds is None else read_seconds)

    @contextmanager
    def batch(self, guard: tuple[str, str] | None = None,
              deadline: float | None = None) -> Iterator[None]:
        with collection_entry(self.control.client, lane=self.lane) as acquired:
            if not acquired:
                raise SourceBusyError('其他采集任务正在运行')
            with self._batch_locked(guard,deadline):
                yield

    @contextmanager
    def _batch_locked(self, guard: tuple[str, str] | None = None,
              deadline: float | None = None) -> Iterator[None]:
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

    def stop(self) -> None:
        self.cancelled.set()

    def _check(self) -> None:
        check_entry()
        check_memory()
        if self.cancelled.is_set():
            raise SourceControlError("源批次已取消")
        if self.guard and self.control.client.get(self.guard[0]) != self.guard[1]:
            raise SourceControlError("业务任务锁已失效")

    def call(self, call: SourceCall) -> Any:
        with collection_entry(self.control.client, lane=call.lane) as acquired:
            if not acquired:
                raise SourceBusyError('其他采集任务正在运行')
            return self._call_locked(call)

    def _call_locked(self, call: SourceCall) -> Any:
        if call.group not in GROUPS:
            raise ValueError("未知来源组")
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
            while keys is None:
                self._check()
                if time.monotonic() >= request_deadline:
                    raise SourceNotStartedError("等待源配额超过预算，尚未发起请求")
                for key in call.cooldown_keys:
                    if self.control.client.exists(key):
                        raise SourceCoolingError(self.control.client.ttl(key))
                keys = self.control.acquire(call.group, token)
                if keys is None:
                    time.sleep(min(0.1, max(0, request_deadline-time.monotonic())))
            queue = self.context.Queue()
            process = self.context.Process(target=self.worker, args=(queue, self.redis_url, call, keys, token,
                self.guard, request_deadline, self.read_seconds, os.getpid(), requested), daemon=True)
            process.start()
            renewed = time.monotonic()
            while True:
                self._check()
                self.control.check(keys, token, self.guard)
                now = time.monotonic()
                if now >= request_deadline:
                    if not requested.is_set():
                        raise SourceNotStartedError("源预算结束，尚未发起请求")
                    raise TimeoutError("源调用超过预算")
                if now - renewed >= RENEW_SECONDS:
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
                        raise SourceResourceError("MEMORY_PRESSURE" if process.exitcode == 72 else "PROCESS_EXIT",state=memory_state(),exitcode=process.exitcode)
            self._check()
            self.control.check(keys, token, self.guard)
            if status == "resource":
                raise SourceResourceError(result["reason"],state=result["state"])
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
            LOGGER.warning("源取消 function=%s reason=%s elapsed=%.2f memory=%s exitcode=%s",call.function,getattr(exc,"reason",type(exc).__name__),time.monotonic()-started,memory_state(),process.exitcode if process else None)
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
            if keys is not None:
                self.control.release(keys, token)
            LOGGER.info("源 %s 总耗时 %.2f 秒 memory=%s exitcode=%s",call.function,time.monotonic()-started,memory_state(),exitcode)

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
        LOGGER.info("源回收 exitcode=%s signal=%s memory=%s",process.exitcode,-process.exitcode if process.exitcode is not None and process.exitcode < 0 else None,memory_state())


@contextmanager
def source_batch(source: Any, guard: tuple[str, str] | None = None,
                 deadline: float | None = None) -> Iterator[None]:
    executor = getattr(source, "executor", None)
    if executor is None:
        yield
    else:
        with executor.batch(guard, deadline):
            yield


def completed(actions: list[tuple[str, str, Callable[[], Any]]], *, source: Any = None,
              ) -> Iterator[tuple[str, Any, Exception | None, float]]:
    """单父进程串行准入，完成立即交付，不为源调用再创建等待线程。"""
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
