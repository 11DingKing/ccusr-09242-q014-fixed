"""可通过本地接口推进的虚拟时钟。

默认返回真实 UTC 时间；测试时通过 ``advance`` 叠加偏移量，
使租约到期与排队老化逻辑无需真实等待即可验证。
偏移量仅保存在进程内存中，服务重启后自动归零（与真实时钟一致）。
"""

from datetime import datetime, timedelta


class VirtualClock:
    def __init__(self) -> None:
        self._offset = timedelta()

    def now(self) -> datetime:
        return datetime.utcnow() + self._offset

    def advance(self, seconds: float) -> datetime:
        self._offset += timedelta(seconds=float(seconds))
        return self.now()

    def reset(self) -> datetime:
        self._offset = timedelta()
        return self.now()

    @property
    def offset_seconds(self) -> float:
        return self._offset.total_seconds()


clock = VirtualClock()
