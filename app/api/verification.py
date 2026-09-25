from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core import get_db
from app.models import RiskLevel, VerificationStatus, VerificationTask
from app.schemas import (
    BatchSupplementResult,
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    EnqueueRequest,
    RenewRequest,
    ReturnSupplementRequest,
    SupplementBatchRequest,
    SupplementReceivedRequest,
    TransferRequest,
    VerificationEventOut,
    VerificationTaskOut,
)
from app.services import verification_service
from app.services.clock import get_now
from app.services.verification_queue import effective_priority_score, is_claimable
from app.services.verification_service import QueueConflict

router = APIRouter(prefix="/verification-queue", tags=["核验队列"])


def _conflict(exc: QueueConflict) -> HTTPException:
    status_code = 404 if exc.code in {"task_not_found", "graduate_not_found"} else 409
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})


def _to_out(task: VerificationTask, now) -> VerificationTaskOut:
    return VerificationTaskOut(
        id=task.id,
        graduate_id=task.graduate_id,
        graduation_year=task.graduation_year,
        risk_level=task.risk_level,
        material_completeness=task.material_completeness,
        status=task.status,
        assignee=task.assignee,
        lease_expires_at=task.lease_expires_at,
        version=task.version,
        claim_count=task.claim_count,
        enqueued_at=task.enqueued_at,
        completed_at=task.completed_at,
        priority_score=effective_priority_score(task, now),
        claimable=is_claimable(task, now),
    )


@router.post("/enqueue", response_model=VerificationTaskOut)
def enqueue(payload: EnqueueRequest, db: Session = Depends(get_db)):
    """把待核验的毕业去向放入队列。"""
    try:
        task = verification_service.enqueue_task(
            db,
            graduate_id=payload.graduate_id,
            actor=payload.actor,
            reason=payload.reason,
            risk_level=payload.risk_level,
            material_completeness=payload.material_completeness,
        )
    except QueueConflict as exc:
        raise _conflict(exc)
    return _to_out(task, get_now(db))


@router.post("/claim", response_model=ClaimResponse)
def claim(payload: ClaimRequest, db: Session = Depends(get_db)):
    """按优先级领取下一条任务，返回带租约的任务。"""
    try:
        task = verification_service.claim_task(
            db,
            actor=payload.actor,
            reason=payload.reason,
            lease_seconds=payload.lease_seconds,
            graduation_year=payload.graduation_year,
            risk_level=payload.risk_level,
            min_material_completeness=payload.min_material_completeness,
        )
    except QueueConflict as exc:
        raise _conflict(exc)
    return ClaimResponse(
        task=_to_out(task, get_now(db)),
        lease_expires_at=task.lease_expires_at,
        message=f"领取成功，租约 {int((task.lease_expires_at - get_now(db)).total_seconds())} 秒后到期",
    )


@router.get("/tasks", response_model=List[VerificationTaskOut])
def list_tasks(
    status: Optional[VerificationStatus] = Query(None, description="队列状态"),
    risk_level: Optional[RiskLevel] = Query(None, description="风险等级"),
    graduation_year: Optional[int] = Query(None, description="毕业届次"),
    assignee: Optional[str] = Query(None, description="负责人"),
    claimable_only: bool = Query(False, description="只看当前可领取的任务"),
    db: Session = Depends(get_db),
):
    """按有效优先级（风险+老化）降序列出队列任务。"""
    now = get_now(db)
    query = db.query(VerificationTask)
    if status is not None:
        query = query.filter(VerificationTask.status == status)
    if risk_level is not None:
        query = query.filter(VerificationTask.risk_level == risk_level)
    if graduation_year is not None:
        query = query.filter(VerificationTask.graduation_year == graduation_year)
    if assignee is not None:
        query = query.filter(VerificationTask.assignee == assignee)

    tasks = query.all()
    if claimable_only:
        tasks = [task for task in tasks if is_claimable(task, now)]
    tasks.sort(key=lambda task: (-effective_priority_score(task, now), task.enqueued_at, task.id))
    return [_to_out(task, now) for task in tasks]


@router.get("/tasks/{task_id}", response_model=VerificationTaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(VerificationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "核验任务不存在"})
    return _to_out(task, get_now(db))


@router.post("/tasks/{task_id}/renew", response_model=VerificationTaskOut)
def renew(task_id: int, payload: RenewRequest, db: Session = Depends(get_db)):
    """当前负责人续租。"""
    try:
        task = verification_service.renew_lease(
            db, task_id=task_id, actor=payload.actor,
            reason=payload.reason, lease_seconds=payload.lease_seconds,
        )
    except QueueConflict as exc:
        raise _conflict(exc)
    return _to_out(task, get_now(db))


@router.post("/tasks/{task_id}/transfer", response_model=VerificationTaskOut)
def transfer(task_id: int, payload: TransferRequest, db: Session = Depends(get_db)):
    """当前负责人把任务转交给其他老师。"""
    try:
        task = verification_service.transfer_task(
            db, task_id=task_id, actor=payload.actor,
            to_assignee=payload.to_assignee, reason=payload.reason,
        )
    except QueueConflict as exc:
        raise _conflict(exc)
    return _to_out(task, get_now(db))


@router.post("/tasks/{task_id}/return-supplement", response_model=VerificationTaskOut)
def return_supplement(task_id: int, payload: ReturnSupplementRequest, db: Session = Depends(get_db)):
    """退回任务要求补件，任务退出领取池。"""
    try:
        task = verification_service.return_for_supplement(
            db, task_id=task_id, actor=payload.actor, reason=payload.reason,
            required_documents=payload.required_documents,
        )
    except QueueConflict as exc:
        raise _conflict(exc)
    return _to_out(task, get_now(db))


@router.post("/tasks/{task_id}/supplement-received", response_model=VerificationTaskOut)
def supplement_received(task_id: int, payload: SupplementReceivedRequest, db: Session = Depends(get_db)):
    """补件到位，任务重新入池参与优先级排序。"""
    try:
        task = verification_service.receive_supplement(
            db, task_id=task_id, actor=payload.actor, reason=payload.reason,
            material_completeness=payload.material_completeness,
        )
    except QueueConflict as exc:
        raise _conflict(exc)
    return _to_out(task, get_now(db))


@router.post("/supplement-received/batch", response_model=BatchSupplementResult)
def supplement_received_batch(payload: SupplementBatchRequest, db: Session = Depends(get_db)):
    """批量登记补件到位，单条失败不影响其他记录。"""
    return verification_service.receive_supplement_batch(
        db,
        actor=payload.actor,
        task_ids=payload.task_ids,
        graduation_year=payload.graduation_year,
        reason=payload.reason,
        material_completeness=payload.material_completeness,
    )


@router.post("/tasks/{task_id}/complete", response_model=VerificationTaskOut)
def complete(task_id: int, payload: CompleteRequest, db: Session = Depends(get_db)):
    """当前负责人在有效租约内完成核验。"""
    try:
        task = verification_service.complete_task(
            db, task_id=task_id, actor=payload.actor, reason=payload.reason,
        )
    except QueueConflict as exc:
        raise _conflict(exc)
    return _to_out(task, get_now(db))


@router.get("/tasks/{task_id}/events", response_model=List[VerificationEventOut])
def get_events(task_id: int, db: Session = Depends(get_db)):
    """任务的完整操作审计（操作者、原因、时间）。"""
    try:
        events = verification_service.list_events(db, task_id)
    except QueueConflict as exc:
        raise _conflict(exc)
    return events
