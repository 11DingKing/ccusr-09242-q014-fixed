"""核验队列的纯业务规则：风险评估、优先级排序与租约判定。

优先级采用"风险基础分 + 等待老化分"：
高风险入队即获得 ``RISK_BASE_SCORE`` 的领先分，普通记录每等待一秒
获得 ``AGING_POINTS_PER_SECOND`` 分，等待足够久后普通记录的有效分
必然反超新入队的高风险记录，从而高风险优先但普通记录不会饿死。
"""

from datetime import datetime
from typing import Iterable, Optional

from app.models import (
    DestinationStatus,
    Graduate,
    RiskLevel,
    VerificationStatus,
    VerificationTask,
)

# 高风险基础分：相当于普通记录排队 2 小时的老化分
RISK_BASE_SCORE = {
    RiskLevel.HIGH: 7200.0,
    RiskLevel.NORMAL: 0.0,
}
AGING_POINTS_PER_SECOND = 1.0

DEFAULT_LEASE_SECONDS = 300
MIN_LEASE_SECONDS = 30
MAX_LEASE_SECONDS = 3600

# 材料完整度低于该值时入队自动评估为高风险
AUTO_RISK_MATERIAL_THRESHOLD = 50


def assess_risk_level(graduate: Graduate, material_completeness: int) -> RiskLevel:
    """未显式指定风险时，依据去向状态与材料完整度给出默认风险等级。"""

    if graduate.destination_status in (DestinationStatus.CHANGING, DestinationStatus.PENDING):
        return RiskLevel.HIGH
    if material_completeness < AUTO_RISK_MATERIAL_THRESHOLD:
        return RiskLevel.HIGH
    return RiskLevel.NORMAL


def clamp_lease_seconds(value: Optional[int]) -> int:
    """把请求租约限幅到允许区间，缺省返回默认租约。"""

    if value is None:
        return DEFAULT_LEASE_SECONDS
    return max(MIN_LEASE_SECONDS, min(MAX_LEASE_SECONDS, value))


def effective_priority_score(task: VerificationTask, now: datetime) -> float:
    """有效优先级 = 风险基础分 + 入队至今的老化分。"""

    wait_seconds = max(0.0, (now - task.enqueued_at).total_seconds())
    return RISK_BASE_SCORE[task.risk_level] + wait_seconds * AGING_POINTS_PER_SECOND


def is_claimable(task: VerificationTask, now: datetime) -> bool:
    """待领取，或已领取但租约到期（可被重新分配）。"""

    if task.status == VerificationStatus.QUEUED:
        return True
    return (
        task.status == VerificationStatus.CLAIMED
        and task.lease_expires_at is not None
        and task.lease_expires_at <= now
    )


def lease_active(task: VerificationTask, now: datetime) -> bool:
    """当前是否处于有效租约内。"""

    return (
        task.status == VerificationStatus.CLAIMED
        and task.lease_expires_at is not None
        and task.lease_expires_at > now
    )


def sort_claimable(tasks: Iterable[VerificationTask], now: datetime) -> list[VerificationTask]:
    """按有效优先级降序、入队时间升序、ID 升序排列可领取任务。"""

    claimable = [task for task in tasks if is_claimable(task, now)]
    return sorted(
        claimable,
        key=lambda task: (-effective_priority_score(task, now), task.enqueued_at, task.id),
    )


def select_next_task(
    tasks: Iterable[VerificationTask],
    now: datetime,
    *,
    graduation_year: Optional[int] = None,
    risk_level: Optional[RiskLevel] = None,
    min_material_completeness: Optional[int] = None,
) -> Optional[VerificationTask]:
    """在可选过滤条件下，按优先级选出下一个可领取任务。"""

    candidates = []
    for task in tasks:
        if graduation_year is not None and task.graduation_year != graduation_year:
            continue
        if risk_level is not None and task.risk_level != risk_level:
            continue
        if min_material_completeness is not None and task.material_completeness < min_material_completeness:
            continue
        candidates.append(task)
    ordered = sort_claimable(candidates, now)
    return ordered[0] if ordered else None
