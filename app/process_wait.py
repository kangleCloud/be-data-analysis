"""调度取消时短时回收一次性业务进程，源进程通过父进程看门狗退出。"""

import asyncio


async def wait_worker(process) -> int:
    try:
        return await process.wait()
    except asyncio.CancelledError:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), timeout=1)
        raise
