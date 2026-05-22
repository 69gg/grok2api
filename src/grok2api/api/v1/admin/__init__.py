"""Admin API router (app_key protected)."""

from fastapi import APIRouter

from grok2api.api.v1.admin.cache import router as cache_router
from grok2api.api.v1.admin.config import router as config_router
from grok2api.api.v1.admin.imagine import router as imagine_router
from grok2api.api.v1.admin.token import router as tokens_router

router = APIRouter()

router.include_router(config_router)
router.include_router(tokens_router)
router.include_router(cache_router)
router.include_router(imagine_router)

__all__ = ["router"]
