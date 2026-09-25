from sqlalchemy import Column, Integer, String, ForeignKey, Boolean, Enum
from sqlalchemy.orm import relationship
from .base import Base, TimestampMixin
from .enums import DestinationStatus, DestinationType, SalaryRange


class Graduate(Base, TimestampMixin):
    __tablename__ = "graduates"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(String(20), unique=True, nullable=False, comment="学号")
    name = Column(String(50), nullable=False, comment="姓名")
    gender = Column(String(10), comment="性别")
    major = Column(String(100), nullable=False, comment="主修专业")
    graduation_year = Column(Integer, nullable=False, index=True, comment="毕业届次")
    college_id = Column(Integer, ForeignKey("colleges.id"), comment="学院ID")

    has_micro_major = Column(Boolean, default=False, nullable=False, comment="是否修读微专业")
    micro_major_id = Column(Integer, ForeignKey("micro_majors.id"), nullable=True, comment="修读的微专业ID")

    destination_status = Column(
        Enum(DestinationStatus),
        default=DestinationStatus.PENDING,
        nullable=False,
        comment="去向状态"
    )
    destination_type = Column(
        Enum(DestinationType),
        default=DestinationType.UNDECIDED,
        nullable=False,
        comment="去向类型"
    )
    unit_industry = Column(String(50), comment="单位行业")
    salary_range = Column(Enum(SalaryRange), comment="起薪区间")
    is_aligned = Column(Boolean, default=False, comment="是否对口就业")

    college = relationship("College", back_populates="graduates")
    micro_major = relationship("MicroMajor", back_populates="graduates")
    status_logs = relationship("StatusChangeLog", back_populates="graduate", order_by="StatusChangeLog.changed_at.desc()")
    follow_ups = relationship("EmployerFollowUp", back_populates="graduate", order_by="EmployerFollowUp.follow_up_date.desc()")
    verification_task = relationship("VerificationTask", back_populates="graduate", uselist=False)
