"""读取Linux cgroup总内存，只报告可取得的计数，不把SIGKILL推断为OOM。"""

from pathlib import Path

MEMORY_LIMIT_BYTES = 800 * 1024 * 1024


def memory_state(root: Path = Path('/sys/fs/cgroup')) -> dict:
    def integer(path):
        try:
            value = path.read_text().strip()
            return int(value) if value.isdigit() else None
        except OSError:
            return None
    events = {}
    try:
        events = dict(line.split() for line in (root/'memory.events').read_text().splitlines())
    except (OSError, ValueError):
        pass
    current = integer(root/'memory.current')
    peak = integer(root/'memory.peak')
    if current is None:
        current = integer(root/'memory/memory.usage_in_bytes')
        peak = integer(root/'memory/memory.max_usage_in_bytes')
    return {'currentBytes':current, 'peakBytes':peak,
            'oomKill':int(events['oom_kill']) if events.get('oom_kill','').isdigit() else None}


class SourceResourceError(RuntimeError):
    """资源不足或进程异常退出，独立于源拒绝/限流冷却。"""
    category = 'RESOURCE'
    def __init__(self, reason='MEMORY_PRESSURE', *, state=None, exitcode=None):
        self.reason, self.state, self.exitcode = reason, state or {}, exitcode
        super().__init__('采集资源不足或源进程异常退出')


def check_memory(reader=None):
    state = (reader or memory_state)()
    if state.get('currentBytes') is not None and state['currentBytes'] >= MEMORY_LIMIT_BYTES:
        raise SourceResourceError(state=state)
    return state
