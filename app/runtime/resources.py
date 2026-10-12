"""源进程异常退出分类，不将信号退出推断为具体系统原因。"""


class SourceResourceError(RuntimeError):
    """进程异常退出独立于数据源失败，不触发源冷却；保留RESOURCE响应分类。"""

    category = "RESOURCE"

    def __init__(self, reason="PROCESS_EXIT", *, exitcode=None):
        self.reason, self.exitcode = reason, exitcode
        super().__init__("采集源进程异常退出")
