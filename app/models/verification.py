from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Enum, Text, Boolean, Index
from sqlalchemy.orm import relationship

from .base import Base, TimestampMixin
from .enums import QueueState, RiskLevel, MaterialStatus


class VerificationItem(Base, TimestampMixin):
    """待核验的毕业去向记录及其在核验队列中的状态与租约。"""

    __tablename__ = "verification_items"
    __table_args__ = (
        Index("ix_verification_queue", "state", "risk_level", "graduation_year"),
    )

    id = Column(Integer, primary_key=True, index=True)
    graduate_id = Column(Integer, ForeignKey("graduates.id"), nullable=True, index=True, comment="关联毕业生ID")
    title = Column(String(200), nullable=False, comment="核验事项标题")
    destination_desc = Column(Text, nullable=True, comment="毕业去向描述")

    risk_level = Column(Enum(RiskLevel), nullable=False, default=RiskLevel.NORMAL, comment="风险等级")
    graduation_year = Column(Integer, nullable=False, comment="毕业届次")
    material_status = Column(
        Enum(MaterialStatus), nullable=False, default=MaterialStatus.INCOMPLETE, comment="材料完整度"
    )
    material_note = Column(Text, nullable=True, comment="材料情况说明")

    state = Column(Enum(QueueState), nullable=False, default=QueueState.QUEUED, comment="队列状态")

    # 排队优先级依据：每次进入待领取队列时刷新，老化（防饿死）据此计算
    queued_since = Column(DateTime, nullable=False, comment="进入待领取队列的时间")
    enqueued_at = Column(DateTime, nullable=False, comment="首次入队时间")

    # 短暂租约：owner 为当前负责人；lease_token/lease_epoch 为每次授权的防滞令牌
    owner = Column(String(50), nullable=True, index=True, comment="当前负责人")
    lease_until = Column(DateTime, nullable=True, comment="租约到期时间")
    lease_token = Column(String(64), nullable=True, comment="本次租约令牌")
    lease_epoch = Column(Integer, nullable=False, default=0, comment="授权纪元，每次易主或重领递增")

    completed_by = Column(String(50), nullable=True, comment="完成人")
    completed_at = Column(DateTime, nullable=True, comment="完成时间")

    events = relationship(
        "VerificationEvent",
        back_populates="item",
        order_by="VerificationEvent.id",
        cascade="all, delete-orphan",
    )


class VerificationEvent(Base):
    """核验队列动作审计流水：所有动作均记录操作者与原因。"""

    __tablename__ = "verification_events"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("verification_items.id"), nullable=False, index=True)
    action = Column(String(20), nullable=False, comment="动作")
    actor = Column(String(50), nullable=False, comment="操作者")
    reason = Column(Text, nullable=True, comment="动作原因")
    from_state = Column(String(20), nullable=True, comment="动作前状态")
    to_state = Column(String(20), nullable=True, comment="动作后状态")
    detail = Column(Text, nullable=True, comment="附加信息(JSON)")
    created_at = Column(DateTime, nullable=False, comment="发生时间（虚拟时钟）")

    item = relationship("VerificationItem", back_populates="events")
