"""序列化市场资金点与雪球报价的个股状态版本提交。"""

import time
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import uuid4

EVENT_LOCK_KEY = "stock:monitor:v1:event:lock"
EVENT_LOCK_SECONDS = 15
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@contextmanager
def monitor_event_lock(client: Any, *, key: str = EVENT_LOCK_KEY) -> Iterator[None]:
    """短临界区只覆盖状态 ID 读取与 Redis 事务提交。"""
    token = uuid4().hex
    for _ in range(40):
        if client.set(key, token, nx=True, ex=EVENT_LOCK_SECONDS):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("个股状态版本锁忙碌")
    try:
        yield
    finally:
        client.eval(RELEASE_SCRIPT, 1, key, token)
