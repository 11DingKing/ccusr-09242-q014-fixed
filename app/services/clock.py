"""随库持久化的虚拟时钟。

队列的租约、老化排序与事件时间全部取自该时钟；时钟状态保存在
``clock_state`` 单行表中，服务重启后虚拟时间语义不丢失，测试可通过
本地接口推进时间。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import ClockState

_CLOCK_ROW_ID = 1


def _real_now() -> datetime:
    """真实当前时间（naive UTC，与 SQLite DateTime 存储一致）。"""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_now(db: Session) -> datetime:
    """读取当前虚拟时间；首次访问时以真实时间初始化。"""

    state = db.get(ClockState, _CLOCK_ROW_ID)
    if state is None:
        state = ClockState(id=_CLOCK_ROW_ID, current_time=_real_now())
        db.add(state)
        db.commit()
    return state.current_time


def advance_clock(db: Session, seconds: float) -> datetime:
    """推进虚拟时间并持久化，返回推进后的时间。"""

    if seconds <= 0:
        raise ValueError("推进秒数必须为正数")
    state = db.get(ClockState, _CLOCK_ROW_ID)
    if state is None:
        state = ClockState(id=_CLOCK_ROW_ID, current_time=_real_now())
        db.add(state)
    state.current_time = state.current_time + timedelta(seconds=seconds)
    db.commit()
    return state.current_time


def reset_clock(db: Session) -> datetime:
    """把虚拟时钟重置回真实时间。"""

    state = db.get(ClockState, _CLOCK_ROW_ID)
    if state is None:
        state = ClockState(id=_CLOCK_ROW_ID)
        db.add(state)
    state.current_time = _real_now()
    db.commit()
    return state.current_time
