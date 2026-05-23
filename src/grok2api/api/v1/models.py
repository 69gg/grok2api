"""
Models API 路由
"""

from fastapi import APIRouter

from grok2api.services.grok.services.model import ModelService
from grok2api.services.token import get_token_manager
from grok2api.services.token.manager import SUPER_POOL_NAME


router = APIRouter(tags=["Models"])


def _has_super_tokens(manager) -> bool:
    pool = manager.pools.get(SUPER_POOL_NAME)
    return bool(pool and pool.count() > 0)


@router.get("/models")
async def list_models():
    """OpenAI 兼容 models 列表接口"""
    manager = await get_token_manager()
    models = ModelService.list(has_super_tokens=_has_super_tokens(manager))
    data = [
        {
            "id": m.model_id,
            "object": "model",
            "created": 0,
            "owned_by": m.owned_by,
        }
        for m in models
    ]
    return {"object": "list", "data": data}


__all__ = ["router"]
