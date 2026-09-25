from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core import get_db
from app.schemas import ClockAdvanceRequest, ClockOut
from app.services.clock import advance_clock, get_now, reset_clock

router = APIRouter(prefix="/clock", tags=["虚拟时钟"])


@router.get("", response_model=ClockOut)
def read_clock(db: Session = Depends(get_db)):
    """读取当前虚拟时间。"""
    return ClockOut(current_time=get_now(db))


@router.post("/advance", response_model=ClockOut)
def advance(payload: ClockAdvanceRequest, db: Session = Depends(get_db)):
    """推进虚拟时间（随库持久化，重启后保持）。"""
    return ClockOut(current_time=advance_clock(db, payload.seconds))


@router.post("/reset", response_model=ClockOut)
def reset(db: Session = Depends(get_db)):
    """把虚拟时钟重置回真实时间。"""
    return ClockOut(current_time=reset_clock(db))
