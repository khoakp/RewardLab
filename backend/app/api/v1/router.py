from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.api.v1.workflows import router as workflows_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(health_router)
api_v1_router.include_router(workflows_router)
