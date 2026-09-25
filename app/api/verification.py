from typing import Optional, List

from fastapi import APIRouter, Depends, Query
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import get_db
from app.models import VerificationItem, QueueState, RiskLevel
from app.schemas import (
    EnqueueRequest,
    ClaimRequest,
    LeaseActionRequest,
    TransferRequest,
    CompleteRequest,
    BatchResubmitRequest,
    VerificationItemSchema,
    ClaimResponse,
    BatchResubmitResponse,
    VerificationEventSchema,
)
from app.services import verification_queue as vq

router = APIRouter(prefix="/verification", tags=["核验队列"])


def _raise(exc: vq.QueueError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/items", response_model=VerificationItemSchema)
def enqueue_item(payload: EnqueueRequest, db: Session = Depends(get_db)):
    try:
        return vq.enqueue(
            db,
            title=payload.title,
            actor=payload.actor,
            reason=payload.reason or "入队核验",
            graduation_year=payload.graduation_year,
            risk_level=payload.risk_level,
            material_status=payload.material_status,
            material_note=payload.material_note,
            graduate_id=payload.graduate_id,
            destination_desc=payload.destination_desc,
        )
    except vq.QueueError as exc:
        _raise(exc)


@router.get("/items", response_model=List[VerificationItemSchema])
def list_items(
    state: Optional[QueueState] = Query(None),
    risk_level: Optional[RiskLevel] = Query(None),
    owner: Optional[str] = Query(None),
    graduation_year: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    stmt = select(VerificationItem)
    if state is not None:
        stmt = stmt.where(VerificationItem.state == state)
    if risk_level is not None:
        stmt = stmt.where(VerificationItem.risk_level == risk_level)
    if owner is not None:
        stmt = stmt.where(VerificationItem.owner == owner)
    if graduation_year is not None:
        stmt = stmt.where(VerificationItem.graduation_year == graduation_year)
    stmt = stmt.order_by(VerificationItem.id)
    return [row[0] for row in db.execute(stmt).all()]


@router.get("/items/{item_id}", response_model=VerificationItemSchema)
def get_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(VerificationItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="核验事项不存在")
    return item


@router.post("/claim", response_model=ClaimResponse)
def claim_next(payload: ClaimRequest, db: Session = Depends(get_db)):
    """领取下一条（按优先级）或指定事项；返回短暂租约令牌。"""
    try:
        item = vq.claim_next(
            db,
            actor=payload.actor,
            reason=payload.reason or "教师领取核验",
            lease_seconds=payload.lease_seconds,
            risk_level=payload.risk_level,
            graduation_year=payload.graduation_year,
            item_id=payload.item_id,
        )
    except vq.QueueError as exc:
        _raise(exc)
    if item is None:
        return ClaimResponse(item=None, claimed=False, message="当前没有可领取的事项")
    return ClaimResponse(item=item, claimed=True, message="领取成功")


@router.post("/items/{item_id}/renew", response_model=VerificationItemSchema)
def renew_item(item_id: int, payload: LeaseActionRequest, db: Session = Depends(get_db)):
    try:
        return vq.renew_lease(
            db, item_id, actor=payload.actor, token=payload.token,
            reason=payload.reason, lease_seconds=payload.lease_seconds or 300,
        )
    except vq.QueueError as exc:
        _raise(exc)


@router.post("/items/{item_id}/transfer", response_model=VerificationItemSchema)
def transfer_item(item_id: int, payload: TransferRequest, db: Session = Depends(get_db)):
    try:
        return vq.transfer(
            db, item_id, actor=payload.actor, to_teacher=payload.to_teacher,
            token=payload.token, reason=payload.reason,
            lease_seconds=payload.lease_seconds,
        )
    except vq.QueueError as exc:
        _raise(exc)


@router.post("/items/{item_id}/return", response_model=VerificationItemSchema)
def return_item(item_id: int, payload: LeaseActionRequest, db: Session = Depends(get_db)):
    try:
        return vq.return_for_material(
            db, item_id, actor=payload.actor, token=payload.token, reason=payload.reason
        )
    except vq.QueueError as exc:
        _raise(exc)


@router.post("/items/{item_id}/complete", response_model=VerificationItemSchema)
def complete_item(item_id: int, payload: CompleteRequest, db: Session = Depends(get_db)):
    try:
        return vq.complete(
            db, item_id, actor=payload.actor, token=payload.token, reason=payload.reason
        )
    except vq.QueueError as exc:
        _raise(exc)


@router.post("/batch-resubmit", response_model=BatchResubmitResponse)
def batch_resubmit(payload: BatchResubmitRequest, db: Session = Depends(get_db)):
    """批量补件到齐：退回补件中的事项重新入队并标记材料齐全。"""
    try:
        result = vq.batch_resubmit(
            db, actor=payload.actor, item_ids=payload.item_ids,
            reason=payload.reason, material_note=payload.material_note,
        )
    except vq.QueueError as exc:
        _raise(exc)
    return BatchResubmitResponse(**result)


@router.get("/events/list", response_model=List[VerificationEventSchema])
def list_all_events(db: Session = Depends(get_db)):
    return vq.list_events(db)


@router.get("/items/{item_id}/events", response_model=List[VerificationEventSchema])
def list_item_events(item_id: int, db: Session = Depends(get_db)):
    item = db.get(VerificationItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="核验事项不存在")
    return vq.list_events(db, item_id=item_id)
