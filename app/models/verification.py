from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Enum, JSON
from sqlalchemy.orm import relationship
from .base import Base
from .enums import RiskLevel, VerificationAction, VerificationStatus


class VerificationTask(Base):
    """待核验毕业去向的队列任务，全部时间戳取自可持久化的虚拟时钟。"""

    __tablename__ = "verification_tasks"

    id = Column(Integer, primary_key=True, index=True)
    graduate_id = Column(
        Integer, ForeignKey("graduates.id"), unique=True, nullable=False,
        comment="毕业生ID（同一毕业生仅允许一条未完成的核验任务）",
    )
    graduation_year = Column(Integer, nullable=False, index=True, comment="毕业届次（冗余自毕业生档案）")
    risk_level = Column(Enum(RiskLevel), default=RiskLevel.NORMAL, nullable=False, comment="风险等级")
    material_completeness = Column(Integer, default=0, nullable=False, comment="材料完整度（0-100）")

    status = Column(
        Enum(VerificationStatus), default=VerificationStatus.QUEUED,
        nullable=False, index=True, comment="队列状态",
    )
    assignee = Column(String(50), nullable=True, comment="当前负责人")
    lease_expires_at = Column(DateTime, nullable=True, comment="租约到期时间（虚拟时钟）")
    version = Column(Integer, default=1, nullable=False, comment="乐观锁版本号")
    claim_count = Column(Integer, default=0, nullable=False, comment="累计领取次数")

    enqueued_at = Column(DateTime, nullable=False, comment="入队时间（虚拟时钟，用于老化排序）")
    completed_at = Column(DateTime, nullable=True, comment="完成时间（虚拟时钟）")

    graduate = relationship("Graduate", back_populates="verification_task")
    events = relationship(
        "VerificationEvent", back_populates="task",
        order_by="VerificationEvent.id", cascade="all, delete-orphan",
    )


class VerificationEvent(Base):
    """队列动作审计：每次状态变化都记录操作者与原因。"""

    __tablename__ = "verification_events"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(
        Integer, ForeignKey("verification_tasks.id"), nullable=False,
        index=True, comment="核验任务ID",
    )
    action = Column(Enum(VerificationAction), nullable=False, comment="动作类型")
    actor = Column(String(50), nullable=False, comment="操作者")
    reason = Column(String(500), nullable=True, comment="操作原因")
    detail = Column(JSON, nullable=True, comment="动作附加信息（租约、前后负责人等）")
    created_at = Column(DateTime, nullable=False, comment="发生时间（虚拟时钟）")

    task = relationship("VerificationTask", back_populates="events")


class ClockState(Base):
    """单行虚拟时钟，随库持久化，服务重启后队列时间语义不丢失。"""

    __tablename__ = "clock_state"

    id = Column(Integer, primary_key=True)
    current_time = Column(DateTime, nullable=False, comment="当前虚拟时间")
