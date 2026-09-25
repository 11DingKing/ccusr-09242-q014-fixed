"""核验队列服务：入队、领取、转交、退回补件、完成等动作。

所有状态变更都通过带版本号/负责人条件的 UPDATE 完成：两个老师同时
领取同一条记录时只有一个 UPDATE 能命中；租约到期后原负责人迟到的
完成请求因其 WHERE 条件（负责人仍是自己且租约未到期）不再命中而被
拒绝，因此不会覆盖新负责人。全部动作写入审计事件并随事务提交，
进程在任一步骤崩溃后重启，状态与租约都能从数据库恢复。
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Graduate,
    RiskLevel,
    VerificationAction,
    VerificationEvent,
    VerificationStatus,
    VerificationTask,
)
from app.services.clock import get_now
from app.services.verification_queue import (
    assess_risk_level,
    clamp_lease_seconds,
    select_next_task,
)


class QueueConflict(Exception):
    """动作与队列当前状态冲突（409）。"""

    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(message)
        self.message = message
        self.code = code


def _get_task(db: Session, task_id: int) -> VerificationTask:
    task = db.get(VerificationTask, task_id)
    if task is None:
        raise QueueConflict("核验任务不存在", "task_not_found")
    return task


def _get_graduate(db: Session, graduate_id: int) -> Graduate:
    graduate = db.get(Graduate, graduate_id)
    if graduate is None:
        raise QueueConflict("毕业生不存在", "graduate_not_found")
    return graduate


def _record_event(
    db: Session, task: VerificationTask, action: VerificationAction,
    actor: str, now: datetime, *, reason: Optional[str] = None,
    detail: Optional[dict] = None,
) -> VerificationEvent:
    event = VerificationEvent(
        task_id=task.id, action=action, actor=actor,
        reason=reason, detail=detail, created_at=now,
    )
    db.add(event)
    return event


def enqueue_task(
    db: Session, *, graduate_id: int, actor: str,
    reason: Optional[str] = None,
    risk_level: Optional[RiskLevel] = None,
    material_completeness: int = 0,
) -> VerificationTask:
    """把一条毕业去向放入核验队列。已完成任务可重新入队。"""

    now = get_now(db)
    graduate = _get_graduate(db, graduate_id)
    if not 0 <= material_completeness <= 100:
        raise QueueConflict("材料完整度必须在 0-100 之间", "invalid_material_completeness")

    risk = risk_level or assess_risk_level(graduate, material_completeness)
    task = (
        db.query(VerificationTask)
        .filter(VerificationTask.graduate_id == graduate_id)
        .first()
    )

    if task is not None and task.status != VerificationStatus.COMPLETED:
        raise QueueConflict("该毕业生已有进行中的核验任务，不能重复入队", "already_queued")

    detail = {
        "risk_level": risk.value,
        "material_completeness": material_completeness,
        "graduation_year": graduate.graduation_year,
    }
    if task is None:
        task = VerificationTask(
            graduate_id=graduate_id,
            graduation_year=graduate.graduation_year,
            risk_level=risk,
            material_completeness=material_completeness,
            status=VerificationStatus.QUEUED,
            version=1,
            claim_count=0,
            enqueued_at=now,
        )
        db.add(task)
        db.flush()
    else:
        # 已完成的任务重新入队：老化重新计时
        detail["reenqueue"] = True
        task.risk_level = risk
        task.material_completeness = material_completeness
        task.status = VerificationStatus.QUEUED
        task.assignee = None
        task.lease_expires_at = None
        task.completed_at = None
        task.enqueued_at = now
        task.version += 1

    _record_event(db, task, VerificationAction.ENQUEUE, actor, now, reason=reason, detail=detail)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise QueueConflict("该毕业生已有进行中的核验任务，不能重复入队", "already_queued")
    db.refresh(task)
    return task


def claim_task(
    db: Session, *, actor: str, reason: Optional[str] = None,
    lease_seconds: Optional[int] = None,
    graduation_year: Optional[int] = None,
    risk_level: Optional[RiskLevel] = None,
    min_material_completeness: Optional[int] = None,
) -> VerificationTask:
    """按优先级领取下一条可领取任务，返回带租约的任务。

    可领取 = 待领取，或领取者租约已到期。并发领取通过版本号条件
    UPDATE 串行化，失败方重新选择下一条任务。
    """

    now = get_now(db)
    lease = clamp_lease_seconds(lease_seconds)
    lease_until = now + timedelta(seconds=lease)

    for _ in range(5):
        db.expire_all()
        tasks = db.query(VerificationTask).all()
        task = select_next_task(
            tasks, now,
            graduation_year=graduation_year,
            risk_level=risk_level,
            min_material_completeness=min_material_completeness,
        )
        if task is None:
            raise QueueConflict("暂无可领取的核验任务", "no_claimable_task")

        expected_version = task.version
        updated = (
            db.query(VerificationTask)
            .filter(
                VerificationTask.id == task.id,
                VerificationTask.version == expected_version,
            )
            .update(
                {
                    VerificationTask.status: VerificationStatus.CLAIMED,
                    VerificationTask.assignee: actor,
                    VerificationTask.lease_expires_at: lease_until,
                    VerificationTask.version: expected_version + 1,
                    VerificationTask.claim_count: VerificationTask.claim_count + 1,
                },
                synchronize_session=False,
            )
        )
        if updated == 1:
            detail = {
                "lease_seconds": lease,
                "lease_expires_at": lease_until.isoformat(),
                "claim_no": task.claim_count + 1,
            }
            db.expire_all()
            task = _get_task(db, task.id)
            _record_event(
                db, task, VerificationAction.CLAIM, actor, now,
                reason=reason, detail=detail,
            )
            db.commit()
            db.refresh(task)
            return task
        # 版本号已变（并发领取/续租/转交抢先），重新选择
    raise QueueConflict("领取冲突，请重试", "claim_conflict")


def _ensure_claimed(task: VerificationTask) -> None:
    if task.status != VerificationStatus.CLAIMED:
        raise QueueConflict(
            f"任务当前状态为 {task.status.value}，该动作要求任务处于核验中",
            "invalid_state",
        )


def renew_lease(
    db: Session, *, task_id: int, actor: str,
    reason: Optional[str] = None, lease_seconds: Optional[int] = None,
) -> VerificationTask:
    """负责人续租。仅当前负责人可续租。"""

    now = get_now(db)
    lease = clamp_lease_seconds(lease_seconds)
    lease_until = now + timedelta(seconds=lease)
    task = _get_task(db, task_id)
    _ensure_claimed(task)
    expected_version = task.version

    updated = (
        db.query(VerificationTask)
        .filter(
            VerificationTask.id == task_id,
            VerificationTask.version == expected_version,
            VerificationTask.assignee == actor,
            VerificationTask.lease_expires_at > now,
        )
        .update(
            {
                VerificationTask.lease_expires_at: lease_until,
                VerificationTask.version: expected_version + 1,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        if task.assignee != actor:
            raise QueueConflict("任务已由其他老师持有，不能续租", "not_owner")
        raise QueueConflict("租约已到期，不能续租，请重新领取", "lease_expired")

    _record_event(
        db, task, VerificationAction.RENEW, actor, now,
        reason=reason, detail={"lease_seconds": lease, "lease_expires_at": lease_until.isoformat()},
    )
    db.commit()
    db.refresh(task)
    return task


def transfer_task(
    db: Session, *, task_id: int, actor: str, to_assignee: str, reason: str,
) -> VerificationTask:
    """当前负责人把任务转交给其他老师，并重置完整租约。"""

    if not to_assignee.strip():
        raise QueueConflict("转交目标不能为空", "invalid_assignee")
    if to_assignee == actor:
        raise QueueConflict("不能转交给自己", "invalid_assignee")

    now = get_now(db)
    task = _get_task(db, task_id)
    if task.status not in (VerificationStatus.CLAIMED, VerificationStatus.WAITING_SUPPLEMENT):
        raise QueueConflict("只有核验中或待补件的任务可以转交", "invalid_state")
    if task.assignee != actor:
        raise QueueConflict("任务由其他老师持有，不能转交", "not_owner")

    expected_version = task.version
    lease_until: Optional[str] = None
    values = {
        VerificationTask.assignee: to_assignee,
        VerificationTask.version: expected_version + 1,
    }
    if task.status == VerificationStatus.CLAIMED:
        lease = clamp_lease_seconds(None)
        expires = now + timedelta(seconds=lease)
        values[VerificationTask.lease_expires_at] = expires
        lease_until = expires.isoformat()

    updated = (
        db.query(VerificationTask)
        .filter(
            VerificationTask.id == task_id,
            VerificationTask.version == expected_version,
            VerificationTask.assignee == actor,
            # 核验中转交要求租约仍有效；待补件转交不受租约约束
            (
                (VerificationTask.status == VerificationStatus.WAITING_SUPPLEMENT)
                | (VerificationTask.lease_expires_at > now)
            ),
        )
        .update(values, synchronize_session=False)
    )
    if not updated:
        if task.assignee != actor:
            raise QueueConflict("任务已被重新分配，转交失败", "not_owner")
        raise QueueConflict("租约已到期，转交前请重新领取", "lease_expired")

    _record_event(
        db, task, VerificationAction.TRANSFER, actor, now, reason=reason,
        detail={"from_assignee": actor, "to_assignee": to_assignee, "lease_expires_at": lease_until},
    )
    db.commit()
    db.refresh(task)
    return task


def return_for_supplement(
    db: Session, *, task_id: int, actor: str, reason: str,
    required_documents: Optional[list[str]] = None,
) -> VerificationTask:
    """负责人退回任务要求补件，任务退出领取池直到材料补齐。"""

    now = get_now(db)
    task = _get_task(db, task_id)
    _ensure_claimed(task)
    if task.assignee != actor:
        raise QueueConflict("任务由其他老师持有，不能退回补件", "not_owner")

    expected_version = task.version
    updated = (
        db.query(VerificationTask)
        .filter(
            VerificationTask.id == task_id,
            VerificationTask.version == expected_version,
            VerificationTask.assignee == actor,
            VerificationTask.lease_expires_at > now,
        )
        .update(
            {
                VerificationTask.status: VerificationStatus.WAITING_SUPPLEMENT,
                VerificationTask.lease_expires_at: None,
                VerificationTask.version: expected_version + 1,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        if task.assignee != actor:
            raise QueueConflict("任务已被重新分配，退回失败", "not_owner")
        raise QueueConflict("租约已到期，退回补件前请重新领取", "lease_expired")

    _record_event(
        db, task, VerificationAction.RETURN_SUPPLEMENT, actor, now, reason=reason,
        detail={"required_documents": required_documents or []},
    )
    db.commit()
    db.refresh(task)
    return task


def receive_supplement(
    db: Session, *, task_id: int, actor: str,
    reason: Optional[str] = None, material_completeness: Optional[int] = None,
) -> VerificationTask:
    """补件到位：任务重新入池参与优先级排序。"""

    now = get_now(db)
    task = _get_task(db, task_id)
    if task.status != VerificationStatus.WAITING_SUPPLEMENT:
        raise QueueConflict("只有待补件任务可以登记补件到位", "invalid_state")
    if material_completeness is not None and not 0 <= material_completeness <= 100:
        raise QueueConflict("材料完整度必须在 0-100 之间", "invalid_material_completeness")

    expected_version = task.version
    values = {
        VerificationTask.status: VerificationStatus.QUEUED,
        VerificationTask.assignee: None,
        VerificationTask.version: expected_version + 1,
        VerificationTask.enqueued_at: now,
    }
    if material_completeness is not None:
        values[VerificationTask.material_completeness] = material_completeness

    updated = (
        db.query(VerificationTask)
        .filter(
            VerificationTask.id == task_id,
            VerificationTask.version == expected_version,
            VerificationTask.status == VerificationStatus.WAITING_SUPPLEMENT,
        )
        .update(values, synchronize_session=False)
    )
    if not updated:
        raise QueueConflict("任务状态已变化，补件登记失败", "state_changed")

    _record_event(
        db, task, VerificationAction.SUPPLEMENT_RECEIVED, actor, now,
        reason=reason,
        detail={"material_completeness": material_completeness if material_completeness is not None else task.material_completeness},
    )
    db.commit()
    db.refresh(task)
    return task


def receive_supplement_batch(
    db: Session, *, actor: str, task_ids: Optional[list[int]] = None,
    graduation_year: Optional[int] = None,
    reason: Optional[str] = None, material_completeness: Optional[int] = None,
) -> dict:
    """批量登记补件到位。单条失败不影响其他记录，逐条返回结果。"""

    if task_ids is None:
        query = db.query(VerificationTask).filter(
            VerificationTask.status == VerificationStatus.WAITING_SUPPLEMENT
        )
        if graduation_year is not None:
            query = query.filter(VerificationTask.graduation_year == graduation_year)
        task_ids = [task.id for task in query.all()]

    succeeded: list[int] = []
    failed: list[dict] = []
    for task_id in task_ids:
        try:
            receive_supplement(
                db, task_id=task_id, actor=actor, reason=reason,
                material_completeness=material_completeness,
            )
            succeeded.append(task_id)
        except QueueConflict as exc:
            db.rollback()
            failed.append({"task_id": task_id, "code": exc.code, "message": exc.message})
    return {"succeeded": succeeded, "failed": failed, "total": len(task_ids)}


def complete_task(
    db: Session, *, task_id: int, actor: str, reason: Optional[str] = None,
) -> VerificationTask:
    """当前负责人在有效租约内完成核验。

    租约到期后、或任务已被别人领取时，迟到的完成请求一律被条件
    UPDATE 拒绝，不会覆盖新负责人。
    """

    now = get_now(db)
    task = _get_task(db, task_id)
    if task.status == VerificationStatus.COMPLETED:
        raise QueueConflict("任务已经完成", "already_completed")
    _ensure_claimed(task)

    expected_version = task.version
    updated = (
        db.query(VerificationTask)
        .filter(
            VerificationTask.id == task_id,
            VerificationTask.version == expected_version,
            VerificationTask.status == VerificationStatus.CLAIMED,
            VerificationTask.assignee == actor,
            VerificationTask.lease_expires_at > now,
        )
        .update(
            {
                VerificationTask.status: VerificationStatus.COMPLETED,
                VerificationTask.lease_expires_at: None,
                VerificationTask.completed_at: now,
                VerificationTask.version: expected_version + 1,
            },
            synchronize_session=False,
        )
    )
    if not updated:
        if task.assignee != actor:
            raise QueueConflict("任务已重新分配给其他老师，迟到的完成请求被拒绝", "not_owner")
        raise QueueConflict("租约已到期，不能完成，请续租或重新领取", "lease_expired")

    _record_event(
        db, task, VerificationAction.COMPLETE, actor, now, reason=reason,
        detail={"completed_at": now.isoformat()},
    )
    db.commit()
    db.refresh(task)
    return task


def list_events(db: Session, task_id: int) -> list[VerificationEvent]:
    _get_task(db, task_id)
    return (
        db.query(VerificationEvent)
        .filter(VerificationEvent.task_id == task_id)
        .order_by(VerificationEvent.id)
        .all()
    )
