"""可恢复的毕业去向核验队列。

关键保证：
* 领取使用带条件的原子 UPDATE，两个会话并发领取同一条记录时只有一个成功；
* 租约（owner + lease_token + lease_epoch + lease_until）到期后记录可被重新分配；
* 转交/续租/完成均采用 (owner, token, 未到期) 三重校验（fencing），
  旧负责人的迟到请求或旧令牌不能覆盖新负责人；
* 高风险按固定“时效加成”优先，但普通记录的等待时间会持续老化，
  老化分超过加成后即可反超，避免饿死；
* 所有动作写入 verification_events，保留操作者、原因与前后状态。
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Optional, Iterable

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.clock import clock
from app.models import (
    VerificationItem,
    VerificationEvent,
    QueueState,
    QueueAction,
    RiskLevel,
    MaterialStatus,
)

# 高风险记录相当于提前 10 分钟入队；普通记录等待超过该时长即可反超新入队的高风险记录
HIGH_RISK_BONUS_SECONDS = 600.0

DEFAULT_LEASE_SECONDS = 300
MAX_BATCH = 500


class QueueError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _now() -> datetime:
    return clock.now()


def _new_token() -> str:
    return uuid.uuid4().hex


def _record_event(
    db: Session,
    item: VerificationItem,
    action: str,
    actor: str,
    reason: Optional[str],
    from_state: Optional[QueueState],
    to_state: Optional[QueueState],
    detail: Optional[dict] = None,
) -> VerificationEvent:
    event = VerificationEvent(
        item_id=item.id,
        action=action,
        actor=actor,
        reason=reason,
        from_state=from_state.value if from_state else None,
        to_state=to_state.value if to_state else None,
        detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else None,
        created_at=_now(),
    )
    db.add(event)
    return event


def _is_claimable(item: VerificationItem, now: datetime) -> bool:
    if item.state == QueueState.QUEUED:
        return True
    # 租约到期的记录保留在 CLAIMED 状态（可审计），但允许被重新分配
    if item.state == QueueState.CLAIMED and item.lease_until is not None and item.lease_until <= now:
        return True
    return False


def _priority_score(item: VerificationItem, now: datetime) -> float:
    # 按整秒老化：同一秒内入队的记录交给风险/届次/材料等确定性次序，避免微秒差决定顺序
    wait = int((now - item.queued_since).total_seconds())
    if wait < 0:
        wait = 0
    bonus = HIGH_RISK_BONUS_SECONDS if item.risk_level == RiskLevel.HIGH else 0.0
    return float(wait) + bonus


def enqueue(
    db: Session,
    *,
    title: str,
    actor: str,
    reason: str,
    graduation_year: int,
    risk_level: RiskLevel = RiskLevel.NORMAL,
    material_status: MaterialStatus = MaterialStatus.INCOMPLETE,
    material_note: Optional[str] = None,
    graduate_id: Optional[int] = None,
    destination_desc: Optional[str] = None,
) -> VerificationItem:
    now = _now()
    item = VerificationItem(
        title=title,
        graduate_id=graduate_id,
        destination_desc=destination_desc,
        risk_level=risk_level,
        graduation_year=graduation_year,
        material_status=material_status,
        material_note=material_note,
        state=QueueState.QUEUED,
        queued_since=now,
        enqueued_at=now,
        owner=None,
        lease_until=None,
        lease_token=None,
        lease_epoch=0,
    )
    db.add(item)
    db.flush()
    _record_event(
        db, item, QueueAction.ENQUEUE.value, actor, reason, None, QueueState.QUEUED,
        {"risk_level": risk_level.value, "graduation_year": graduation_year,
         "material_status": material_status.value},
    )
    db.commit()
    db.refresh(item)
    return item


def _claimable_candidates(
    db: Session,
    *,
    risk_level: Optional[RiskLevel] = None,
    graduation_year: Optional[int] = None,
) -> list[VerificationItem]:
    now = _now()
    stmt = select(VerificationItem).where(
        or_(
            VerificationItem.state == QueueState.QUEUED,
            (VerificationItem.state == QueueState.CLAIMED)
            & (VerificationItem.lease_until.is_not(None))
            & (VerificationItem.lease_until <= now),
        )
    )
    if risk_level is not None:
        stmt = stmt.where(VerificationItem.risk_level == risk_level)
    if graduation_year is not None:
        stmt = stmt.where(VerificationItem.graduation_year == graduation_year)
    items = [row[0] for row in db.execute(stmt).all()]
    items = [item for item in items if _is_claimable(item, now)]
    # 高风险加成优先；同分时高风险优先，随后毕业届次更早、材料更齐全、入队更早者优先；
    # 普通记录等待时间持续累积，超过加成后自然反超，防止饿死
    items.sort(key=lambda item: (
        -_priority_score(item, now),
        0 if item.risk_level == RiskLevel.HIGH else 1,
        item.graduation_year,
        0 if item.material_status == MaterialStatus.COMPLETE else 1,
        item.id,
    ))
    return items


def _atomic_claim(db: Session, item_id: int, actor: str, until: datetime, token: str) -> bool:
    """条件更新：仅当记录待领取或租约已到期时才能抢占，返回是否成功。"""
    now = _now()
    stmt = (
        update(VerificationItem)
        .where(VerificationItem.id == item_id)
        .where(
            or_(
                VerificationItem.state == QueueState.QUEUED,
                (VerificationItem.state == QueueState.CLAIMED)
                & (VerificationItem.lease_until.is_not(None))
                & (VerificationItem.lease_until <= now),
            )
        )
        .values(
            state=QueueState.CLAIMED,
            owner=actor,
            lease_until=until,
            lease_token=token,
            lease_epoch=VerificationItem.lease_epoch + 1,
        )
    )
    result = db.execute(stmt)
    return result.rowcount == 1


def claim_next(
    db: Session,
    *,
    actor: str,
    reason: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    risk_level: Optional[RiskLevel] = None,
    graduation_year: Optional[int] = None,
    item_id: Optional[int] = None,
) -> Optional[VerificationItem]:
    now = _now()
    until = now + timedelta(seconds=lease_seconds)
    token = _new_token()

    if item_id is not None:
        item = db.get(VerificationItem, item_id)
        if item is None:
            raise QueueError(404, "核验事项不存在")
        if not _is_claimable(item, now):
            if item.state == QueueState.COMPLETED:
                raise QueueError(409, "事项已完成，不能领取")
            if item.state == QueueState.WAITING_MATERIAL:
                raise QueueError(409, "事项处于退回补件中，暂不能领取")
            raise QueueError(409, f"事项正由 {item.owner} 持有，租约未到期")
        target_ids = [item.id]
    else:
        target_ids = [candidate.id for candidate in _claimable_candidates(
            db, risk_level=risk_level, graduation_year=graduation_year
        )]
        if not target_ids:
            return None

    # 按优先级逐个尝试条件抢占；并发落败时继续尝试下一个候选
    claimed_item: Optional[VerificationItem] = None
    previous_owner = None
    for target_id in target_ids:
        candidate = db.get(VerificationItem, target_id)
        if candidate is None or not _is_claimable(candidate, _now()):
            continue
        previous_owner = candidate.owner
        if _atomic_claim(db, target_id, actor, until, token):
            claimed_item = candidate
            break
        db.rollback()

    if claimed_item is None:
        return None

    prior_state = claimed_item.state
    db.commit()
    item = db.get(VerificationItem, claimed_item.id)
    detail = {"lease_seconds": lease_seconds, "lease_until": until.isoformat(),
              "token": token, "priority_score": round(_priority_score(claimed_item, now), 1)}
    if previous_owner and previous_owner != actor:
        detail["previous_owner"] = previous_owner
        detail["reassigned_reason"] = "lease_expired"
    _record_event(
        db, item, QueueAction.CLAIM.value, actor, reason,
        prior_state, QueueState.CLAIMED, detail,
    )
    db.commit()
    db.refresh(item)
    return item


def _require_active_lease(
    item: VerificationItem, actor: str, token: str, *, action_name: str
) -> None:
    """校验操作者仍持有有效租约，任何一项不符都拒绝（fencing）。"""
    now = _now()
    if item.state == QueueState.COMPLETED:
        raise QueueError(409, "事项已完成，不能重复处理")
    if item.state != QueueState.CLAIMED:
        raise QueueError(409, f"事项当前为{item.state.value}状态，不能执行{action_name}")
    if item.owner != actor or item.lease_token != token:
        raise QueueError(409, "租约令牌无效：事项已转交或重新分配，迟到的请求被拒绝")
    if item.lease_until is None or item.lease_until <= now:
        raise QueueError(409, "租约已到期，请重新领取后再操作")


def renew_lease(
    db: Session, item_id: int, *, actor: str, token: str, reason: str, lease_seconds: int
) -> VerificationItem:
    item = db.get(VerificationItem, item_id)
    if item is None:
        raise QueueError(404, "核验事项不存在")
    _require_active_lease(item, actor, token, action_name="续租")
    until = _now() + timedelta(seconds=lease_seconds)
    item.lease_until = until
    _record_event(
        db, item, QueueAction.RENEW.value, actor, reason,
        QueueState.CLAIMED, QueueState.CLAIMED,
        {"lease_seconds": lease_seconds, "lease_until": until.isoformat()},
    )
    db.commit()
    db.refresh(item)
    return item


def transfer(
    db: Session, item_id: int, *, actor: str, to_teacher: str, token: str,
    reason: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> VerificationItem:
    item = db.get(VerificationItem, item_id)
    if item is None:
        raise QueueError(404, "核验事项不存在")
    if to_teacher == actor:
        raise QueueError(400, "转交对象不能是当前负责人自己")
    _require_active_lease(item, actor, token, action_name="转交")
    now = _now()
    until = now + timedelta(seconds=lease_seconds)
    new_token = _new_token()
    old_owner = item.owner
    item.owner = to_teacher
    item.lease_until = until
    item.lease_token = new_token
    item.lease_epoch += 1
    item.state = QueueState.CLAIMED
    _record_event(
        db, item, QueueAction.TRANSFER.value, actor, reason,
        QueueState.CLAIMED, QueueState.CLAIMED,
        {"from_teacher": old_owner, "to_teacher": to_teacher,
         "lease_until": until.isoformat(), "new_token": new_token},
    )
    db.commit()
    db.refresh(item)
    return item


def return_for_material(
    db: Session, item_id: int, *, actor: str, token: str, reason: str
) -> VerificationItem:
    item = db.get(VerificationItem, item_id)
    if item is None:
        raise QueueError(404, "核验事项不存在")
    _require_active_lease(item, actor, token, action_name="退回补件")
    item.state = QueueState.WAITING_MATERIAL
    item.material_status = MaterialStatus.INCOMPLETE
    # 补件期间挂起租约并作废旧令牌，补件到齐后重新入队
    item.owner = None
    item.lease_until = None
    item.lease_token = None
    item.lease_epoch += 1
    _record_event(
        db, item, QueueAction.RETURN.value, actor, reason,
        QueueState.CLAIMED, QueueState.WAITING_MATERIAL,
    )
    db.commit()
    db.refresh(item)
    return item


def batch_resubmit(
    db: Session, *, actor: str, item_ids: Iterable[int], reason: str,
    material_note: Optional[str] = None,
) -> dict:
    ids = list(item_ids)
    if not ids:
        raise QueueError(400, "item_ids 不能为空")
    if len(ids) > MAX_BATCH:
        raise QueueError(400, f"单次最多处理 {MAX_BATCH} 条")
    if len(ids) != len(set(ids)):
        raise QueueError(400, "item_ids 存在重复")

    reopened: list[int] = []
    skipped: list[int] = []
    missing: list[int] = []
    results: list[VerificationItem] = []
    now = _now()
    for item_id in ids:
        item = db.get(VerificationItem, item_id)
        if item is None:
            missing.append(item_id)
            continue
        if item.state != QueueState.WAITING_MATERIAL:
            # 幂等：非退回补件状态的记录直接跳过，不报错
            skipped.append(item_id)
            continue
        item.state = QueueState.QUEUED
        item.material_status = MaterialStatus.COMPLETE
        if material_note is not None:
            item.material_note = material_note
        item.queued_since = now
        item.owner = None
        item.lease_until = None
        item.lease_token = None
        item.lease_epoch += 1
        db.flush()
        _record_event(
            db, item, QueueAction.RESUBMIT.value, actor, reason,
            QueueState.WAITING_MATERIAL, QueueState.QUEUED,
            {"material_status": MaterialStatus.COMPLETE.value,
             **({"material_note": material_note} if material_note is not None else {})},
        )
        reopened.append(item_id)
        results.append(item)
    db.commit()
    for item in results:
        db.refresh(item)
    return {"reopened": reopened, "skipped": skipped, "missing": missing, "results": results}


def complete(
    db: Session, item_id: int, *, actor: str, token: str, reason: str
) -> VerificationItem:
    item = db.get(VerificationItem, item_id)
    if item is None:
        raise QueueError(404, "核验事项不存在")
    _require_active_lease(item, actor, token, action_name="完成")
    now = _now()
    item.state = QueueState.COMPLETED
    item.completed_by = actor
    item.completed_at = now
    item.lease_until = None
    item.lease_token = None
    item.lease_epoch += 1
    _record_event(
        db, item, QueueAction.COMPLETE.value, actor, reason,
        QueueState.CLAIMED, QueueState.COMPLETED,
        {"completed_at": now.isoformat()},
    )
    db.commit()
    db.refresh(item)
    return item


def list_events(db: Session, *, item_id: Optional[int] = None) -> list[VerificationEvent]:
    stmt = select(VerificationEvent).order_by(VerificationEvent.id)
    if item_id is not None:
        stmt = stmt.where(VerificationEvent.item_id == item_id)
    return [row[0] for row in db.execute(stmt).all()]
