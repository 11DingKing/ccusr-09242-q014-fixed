from datetime import datetime
from typing import Any, List, Optional

from pydantic import Field

from .common import BaseSchema
from app.models import RiskLevel, VerificationAction, VerificationStatus


class EnqueueRequest(BaseSchema):
    graduate_id: int = Field(..., description="毕业生ID")
    actor: str = Field(..., min_length=1, max_length=50, description="操作者")
    reason: Optional[str] = Field(None, max_length=500, description="入队原因")
    risk_level: Optional[RiskLevel] = Field(None, description="风险等级，缺省按去向状态与材料完整度自动评估")
    material_completeness: int = Field(0, ge=0, le=100, description="材料完整度（0-100）")


class ClaimRequest(BaseSchema):
    actor: str = Field(..., min_length=1, max_length=50, description="领取人")
    reason: Optional[str] = Field(None, max_length=500, description="领取原因")
    lease_seconds: Optional[int] = Field(None, ge=30, le=3600, description="租约秒数，缺省300秒")
    graduation_year: Optional[int] = Field(None, description="只领取该届次的任务")
    risk_level: Optional[RiskLevel] = Field(None, description="只领取该风险等级的任务")
    min_material_completeness: Optional[int] = Field(None, ge=0, le=100, description="只领取材料完整度不低于该值的任务")


class RenewRequest(BaseSchema):
    actor: str = Field(..., min_length=1, max_length=50, description="操作者（须为当前负责人）")
    reason: Optional[str] = Field(None, max_length=500, description="续租原因")
    lease_seconds: Optional[int] = Field(None, ge=30, le=3600, description="租约秒数，缺省300秒")


class TransferRequest(BaseSchema):
    actor: str = Field(..., min_length=1, max_length=50, description="操作者（须为当前负责人）")
    to_assignee: str = Field(..., min_length=1, max_length=50, description="接收老师")
    reason: str = Field(..., min_length=1, max_length=500, description="转交原因")


class ReturnSupplementRequest(BaseSchema):
    actor: str = Field(..., min_length=1, max_length=50, description="操作者（须为当前负责人）")
    reason: str = Field(..., min_length=1, max_length=500, description="退回原因")
    required_documents: Optional[List[str]] = Field(None, description="需要补交的材料清单")


class SupplementReceivedRequest(BaseSchema):
    actor: str = Field(..., min_length=1, max_length=50, description="登记人")
    reason: Optional[str] = Field(None, max_length=500, description="登记说明")
    material_completeness: Optional[int] = Field(None, ge=0, le=100, description="补件后的材料完整度")


class SupplementBatchRequest(BaseSchema):
    actor: str = Field(..., min_length=1, max_length=50, description="登记人")
    task_ids: Optional[List[int]] = Field(None, description="待登记任务ID；缺省处理全部待补件任务")
    graduation_year: Optional[int] = Field(None, description="与缺省模式配合，只处理该届次")
    reason: Optional[str] = Field(None, max_length=500, description="登记说明")
    material_completeness: Optional[int] = Field(None, ge=0, le=100, description="补件后的材料完整度")


class CompleteRequest(BaseSchema):
    actor: str = Field(..., min_length=1, max_length=50, description="操作者（须为当前负责人）")
    reason: Optional[str] = Field(None, max_length=500, description="完成说明")


class VerificationEventOut(BaseSchema):
    id: int
    task_id: int
    action: VerificationAction
    actor: str
    reason: Optional[str] = None
    detail: Optional[Any] = None
    created_at: datetime


class VerificationTaskOut(BaseSchema):
    id: int
    graduate_id: int
    graduation_year: int
    risk_level: RiskLevel
    material_completeness: int
    status: VerificationStatus
    assignee: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    version: int
    claim_count: int
    enqueued_at: datetime
    completed_at: Optional[datetime] = None
    priority_score: float = Field(..., description="当前有效优先级分（风险基础分+老化分）")
    claimable: bool = Field(..., description="当前是否可被领取")


class ClaimResponse(BaseSchema):
    task: VerificationTaskOut
    lease_expires_at: datetime
    message: str


class BatchSupplementResult(BaseSchema):
    succeeded: List[int]
    failed: List[dict]
    total: int


class ClockAdvanceRequest(BaseSchema):
    seconds: float = Field(..., gt=0, description="推进秒数")


class ClockOut(BaseSchema):
    current_time: datetime
