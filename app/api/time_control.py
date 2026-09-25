from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from app.core.clock import clock
from app.schemas import ClockResponse

router = APIRouter(prefix="/dev/clock", tags=["时间控制（本地/测试）"])

# 仅允许本机测试客户端推进虚拟时间；TestClient 的 client.host 为 "testclient"
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _ensure_local(request: Request) -> None:
    # TestClient/ASGI 进程内调用没有网络对端（client 为 None）；真实 uvicorn 请求必然带地址
    host = request.client.host if request.client else "testclient"
    if host not in _LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="虚拟时钟仅允许本机推进")


class AdvanceRequest(BaseModel):
    seconds: float = Field(..., ge=0, description="向前推进的虚拟秒数")


@router.get("", response_model=ClockResponse)
def get_clock():
    return ClockResponse(now=clock.now(), offset_seconds=clock.offset_seconds)


@router.post("/advance", response_model=ClockResponse)
def advance_clock(payload: AdvanceRequest, request: Request):
    _ensure_local(request)
    now = clock.advance(payload.seconds)
    return ClockResponse(now=now, offset_seconds=clock.offset_seconds)


@router.post("/reset", response_model=ClockResponse)
def reset_clock(request: Request):
    _ensure_local(request)
    now = clock.reset()
    return ClockResponse(now=now, offset_seconds=0.0)
