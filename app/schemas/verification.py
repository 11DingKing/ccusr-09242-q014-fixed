from datetime import datetime
from typing import Optional, List

from pydantic import Field

from .common import BaseSchema, TimestampSchema
from app.models import RiskLevel, MaterialStatus, QueueState


class EnqueueRequest(BaseSchema):
    title: str = Field(..., min_length=1, description="核验事项标题")
    graduate_id: Optional[int] = None
    destination_desc: Optional[str] = None
    risk_level: RiskLevel = RiskLevel.NORMAL
    graduation_year: int = Field(..., ge=1990, le=2100)
    material_status: MaterialStatus = MaterialStatus.INCOMPLETE
    material_note: Optional[str] = None
    actor: str = Field(..., min_length=1, description="操作者")
    reason: Optional[str] = Field(None, description="入队原因")


class ClaimRequest(BaseSchema):
    actor: str = Field(..., min_length=1, description="领取教师")
    reason: Optional[str] = Field(None, description="领取原因/备注")
    lease_seconds: int = Field(300, ge=1, le=86400, description="租约时长（虚拟秒）")
    risk_level: Optional[RiskLevel] = Field(None, description="只领取指定风险等级")
    graduation_year: Optional[int] = None
    item_id: Optional[int] = Field(None, description="领取指定事项；缺省按优先级领取下一条")


class LeaseActionRequest(BaseSchema):
    actor: str = Field(..., min_length=1)
    token: str = Field(..., description="领取/转交时获得的租约令牌")
    reason: str = Field(..., min_length=1, description="动作原因")
    lease_seconds: Optional[int] = Field(None, ge=1, le=86400, description="续租时长（仅续租/转交使用）")


class TransferRequest(BaseSchema):
    actor: str = Field(..., min_length=1, description="转交人（当前负责人或管理员）")
    to_teacher: str = Field(..., min_length=1, description="接收教师")
    token: str = Field(..., description="转交人持有的租约令牌")
    reason: str = Field(..., min_length=1, description="转交原因")
    lease_seconds: int = Field(300, ge=1, le=86400)


class CompleteRequest(BaseSchema):
    actor: str = Field(..., min_length=1)
    token: str = Field(..., description="领取/转交时获得的租约令牌")
    reason: str = Field(..., min_length=1, description="完成结论/原因")


class BatchResubmitRequest(BaseSchema):
    actor: str = Field(..., min_length=1, description="补件操作者")
    item_ids: List[int] = Field(..., min_length=1, description="补件到齐的事项ID列表")
    reason: str = Field(..., min_length=1, description="补件说明")
    material_note: Optional[str] = None


class VerificationItemSchema(TimestampSchema):
    id: int
    graduate_id: Optional[int] = None
    title: str
    destination_desc: Optional[str] = None
    risk_level: RiskLevel
    graduation_year: int
    material_status: MaterialStatus
    material_note: Optional[str] = None
    state: QueueState
    queued_since: datetime
    enqueued_at: datetime
    owner: Optional[str] = None
    lease_until: Optional[datetime] = None
    lease_token: Optional[str] = None
    lease_epoch: int
    completed_by: Optional[str] = None
    completed_at: Optional[datetime] = None


class ClaimResponse(BaseSchema):
    item: Optional[VerificationItemSchema] = None
    claimed: bool
    message: str


class BatchResubmitResponse(BaseSchema):
    reopened: List[int]
    skipped: List[int]
    missing: List[int]
    results: List[VerificationItemSchema]


class VerificationEventSchema(BaseSchema):
    id: int
    item_id: int
    action: str
    actor: str
    reason: Optional[str] = None
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    detail: Optional[str] = None
    created_at: datetime


class ClockResponse(BaseSchema):
    now: datetime
    offset_seconds: float
