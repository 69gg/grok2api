"""
Grok 模型管理服务
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple
from pydantic import BaseModel, Field

from grok2api.core.exceptions import ValidationException


class Tier(str, Enum):
    """模型档位"""
    BASIC = "basic"
    SUPER = "super"


class Cost(str, Enum):
    """计费类型"""
    LOW = "low"
    HIGH = "high"


class ModelInfo(BaseModel):
    """模型信息"""
    model_id: str
    grok_model: str
    rate_limit_model: str
    model_mode: str
    tier: Tier = Field(default=Tier.BASIC)
    cost: Cost = Field(default=Cost.LOW)
    display_name: str
    description: str = ""
    is_video: bool = False
    is_image: bool = False


def _super_model(
    model_id: str,
    *,
    model_mode: str,
    grok_model: Optional[str] = None,
    display_name: Optional[str] = None,
    is_image: bool = False,
    cost: Cost = Cost.LOW,
) -> ModelInfo:
    upstream = grok_model or model_id
    return ModelInfo(
        model_id=model_id,
        grok_model=upstream,
        rate_limit_model=upstream,
        model_mode=model_mode,
        tier=Tier.SUPER,
        cost=cost,
        display_name=display_name or model_id,
        is_image=is_image,
    )


class ModelService:
    """模型管理服务"""

    BASIC_MODEL_IDS = {
        "grok-4.3-fast",
        "grok-imagine-image",
    }

    MODELS = [
        ModelInfo(
            model_id="grok-4.3-fast",
            grok_model="grok-43",
            rate_limit_model="grok-43",
            model_mode="MODEL_MODE_FAST",
            cost=Cost.LOW,
            display_name="Grok 4.3 Fast",
            description="Fast Grok 4.3 chat model",
        ),
        _super_model("grok-4.20-0309", model_mode="MODEL_MODE_AUTO"),
        _super_model("grok-4.20-0309-reasoning", model_mode="MODEL_MODE_EXPERT"),
        _super_model("grok-4.20-0309-non-reasoning-super", model_mode="MODEL_MODE_FAST"),
        _super_model("grok-4.20-0309-super", model_mode="MODEL_MODE_AUTO"),
        _super_model("grok-4.20-0309-reasoning-super", model_mode="MODEL_MODE_EXPERT"),
        _super_model("grok-4.20-auto", grok_model="grok-420", model_mode="MODEL_MODE_AUTO"),
        _super_model("grok-4.20-expert", grok_model="grok-420", model_mode="MODEL_MODE_EXPERT"),
        _super_model(
            "grok-4.3-beta",
            grok_model="grok-43",
            model_mode="grok-420-computer-use-sa",
        ),
        _super_model(
            "grok-imagine-image",
            model_mode="MODEL_MODE_AUTO",
            cost=Cost.HIGH,
            is_image=True,
            display_name="Grok Imagine Image",
        ),
        _super_model(
            "grok-imagine-image-pro",
            model_mode="MODEL_MODE_AUTO",
            cost=Cost.HIGH,
            is_image=True,
            display_name="Grok Imagine Image Pro",
        ),
        _super_model(
            "grok-imagine-image-edit",
            grok_model="grok-imagine-image",
            model_mode="MODEL_MODE_AUTO",
            cost=Cost.HIGH,
            display_name="Grok Imagine Image Edit",
        ),
        _super_model(
            "grok-imagine-1.0-video",
            grok_model="grok-43",
            model_mode="MODEL_MODE_AUTO",
            cost=Cost.HIGH,
            display_name="Grok Video",
        ),
    ]

    _map = {m.model_id: m for m in MODELS}

    @classmethod
    def get(cls, model_id: str) -> Optional[ModelInfo]:
        """获取模型信息"""
        return cls._map.get(model_id)

    @classmethod
    def list(cls) -> list[ModelInfo]:
        """获取所有模型"""
        return list(cls._map.values())

    @classmethod
    def valid(cls, model_id: str) -> bool:
        """模型是否有效"""
        return model_id in cls._map

    @classmethod
    def to_grok(cls, model_id: str) -> Tuple[str, str]:
        """转换为 Grok 参数"""
        model = cls.get(model_id)
        if not model:
            raise ValidationException(f"Invalid model ID: {model_id}")
        return model.grok_model, model.model_mode

    @classmethod
    def rate_limit_model_for(cls, model_id: str) -> str:
        """用于 /rest/rate-limits 的 modelName 映射。"""
        model = cls.get(model_id)
        return model.rate_limit_model if model else model_id

    @classmethod
    def is_heavy_bucket_model(cls, model_id: str) -> bool:
        """是否使用 heavy 配额桶（目前仅 grok-4-heavy）。"""
        return model_id == "grok-4-heavy"

    @classmethod
    def pool_for_model(cls, model_id: str) -> str:
        """根据模型选择 Token 池"""
        model = cls.get(model_id)
        if model and model.model_id not in cls.BASIC_MODEL_IDS:
            return "ssoSuper"
        return "ssoBasic"

    @classmethod
    def pool_candidates_for_model(cls, model_id: str) -> list[str]:
        """按优先级返回可用 Token 池列表。"""
        model = cls.get(model_id)
        if not model:
            return ["ssoSuper"]
        if model.model_id in cls.BASIC_MODEL_IDS:
            return ["ssoBasic", "ssoSuper"]
        if model.tier == Tier.SUPER or model.model_id not in cls.BASIC_MODEL_IDS:
            return ["ssoSuper"]
        return ["ssoSuper"]


__all__ = ["ModelService"]
