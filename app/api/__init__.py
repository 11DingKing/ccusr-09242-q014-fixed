from fastapi import APIRouter
from .graduates import router as graduates_router
from .colleges import router as colleges_router
from .micro_majors import router as micro_majors_router
from .statistics import router as statistics_router
from .employer_follow_ups import router as follow_ups_router
from .warnings import router as warnings_router
from .attributions import router as attributions_router
from .reference_lines import router as reference_lines_router
from .verification import router as verification_router
from .time_control import router as time_control_router

api_router = APIRouter()

api_router.include_router(graduates_router)
api_router.include_router(colleges_router)
api_router.include_router(micro_majors_router)
api_router.include_router(statistics_router)
api_router.include_router(follow_ups_router)
api_router.include_router(warnings_router)
api_router.include_router(attributions_router)
api_router.include_router(reference_lines_router)
api_router.include_router(verification_router)
api_router.include_router(time_control_router)

__all__ = ["api_router"]
