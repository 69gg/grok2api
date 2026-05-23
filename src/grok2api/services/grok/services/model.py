"""
Grok 模型管理服务
"""

from enum import Enum
from typing import Optional, Tuple, List
from pydantic import BaseModel, Field, ConfigDict

from grok2api.core.exceptions import ValidationException
from grok2api.services.grok.services.console_capabilities import (
    ConsoleModelCapabilities,
    CAP_BUILD,
    CAP_GROK_43,
    CAP_420_NON_REASONING,
    CAP_420_REASONING,
    CAP_MULTI_AGENT,
)


class Tier(str, Enum):
    """模型档位"""

    BASIC = "basic"
    SUPER = "super"


class Cost(str, Enum):
    """计费类型"""

    LOW = "low"
    HIGH = "high"


class Channel(str, Enum):
    GROK = "grok"
    CONSOLE = "console"


class ModelInfo(BaseModel):
    """模型信息"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_id: str
    grok_model: str
    model_mode: str
    tier: Tier = Field(default=Tier.BASIC)
    cost: Cost = Field(default=Cost.LOW)
    display_name: str
    description: str = ""
    is_image: bool = False
    is_image_edit: bool = False
    is_video: bool = False
    channel: Channel = Channel.GROK
    console_model: Optional[str] = None
    console_search: bool = False
    capabilities: Optional[ConsoleModelCapabilities] = None

    @property
    def owned_by(self) -> str:
        if self.channel == Channel.CONSOLE:
            return "xai-console"
        return "grok2api@chenyme"


def _console_model(
    model_id: str,
    console_model: str,
    *,
    console_search: bool = False,
    capabilities: ConsoleModelCapabilities,
    display_name: Optional[str] = None,
) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        grok_model=console_model,
        model_mode="CONSOLE",
        tier=Tier.BASIC,
        cost=Cost.LOW,
        display_name=display_name or model_id.upper(),
        description="Console Chat Playground model",
        channel=Channel.CONSOLE,
        console_model=console_model,
        console_search=console_search,
        capabilities=capabilities,
    )


_CONSOLE_MODELS = [
    _console_model("grok-4.3", "grok-4.3", capabilities=CAP_GROK_43),
    _console_model("grok-4.3-search", "grok-4.3", console_search=True, capabilities=CAP_GROK_43),
    _console_model("grok-build-0.1", "grok-build-0.1", capabilities=CAP_BUILD),
    _console_model("grok-build-0.1-search", "grok-build-0.1", console_search=True, capabilities=CAP_BUILD),
    _console_model("grok-4.20-0309-non-reasoning", "grok-4.20-0309-non-reasoning", capabilities=CAP_420_NON_REASONING),
    _console_model(
        "grok-4.20-0309-non-reasoning-search",
        "grok-4.20-0309-non-reasoning",
        console_search=True,
        capabilities=CAP_420_NON_REASONING,
    ),
    _console_model("grok-4.20-0309-reasoning", "grok-4.20-0309-reasoning", capabilities=CAP_420_REASONING),
    _console_model(
        "grok-4.20-0309-reasoning-search",
        "grok-4.20-0309-reasoning",
        console_search=True,
        capabilities=CAP_420_REASONING,
    ),
    _console_model("grok-4.20-multi-agent-0309", "grok-4.20-multi-agent-0309", capabilities=CAP_MULTI_AGENT),
    _console_model(
        "grok-4.20-multi-agent-0309-search",
        "grok-4.20-multi-agent-0309",
        console_search=True,
        capabilities=CAP_MULTI_AGENT,
    ),
]

CONSOLE_MODEL_IDS = {m.model_id for m in _CONSOLE_MODELS}


class ModelService:
    """模型管理服务"""

    BASIC_MODEL_IDS = {
        "grok-4.3-fast",
        "grok-imagine-1.0",
        "grok-imagine-1.0-edit",
        *CONSOLE_MODEL_IDS,
    }

    MODELS = [
        ModelInfo(
            model_id="grok-3",
            grok_model="grok-3",
            model_mode="MODEL_MODE_GROK_3",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-3",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-3-mini",
            grok_model="grok-3",
            model_mode="MODEL_MODE_GROK_3_MINI_THINKING",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-3-MINI",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-3-thinking",
            grok_model="grok-3",
            model_mode="MODEL_MODE_GROK_3_THINKING",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-3-THINKING",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4",
            grok_model="grok-4",
            model_mode="MODEL_MODE_GROK_4",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-4",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4-thinking",
            grok_model="grok-4",
            model_mode="MODEL_MODE_GROK_4_THINKING",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-4-THINKING",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4-heavy",
            grok_model="grok-4",
            model_mode="MODEL_MODE_HEAVY",
            tier=Tier.SUPER,
            cost=Cost.HIGH,
            display_name="GROK-4-HEAVY",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4.1-mini",
            grok_model="grok-4-1-thinking-1129",
            model_mode="MODEL_MODE_GROK_4_1_MINI_THINKING",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-4.1-MINI",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4.1-fast",
            grok_model="grok-4-1-thinking-1129",
            model_mode="MODEL_MODE_FAST",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-4.1-FAST",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4.1-expert",
            grok_model="grok-4-1-thinking-1129",
            model_mode="MODEL_MODE_EXPERT",
            tier=Tier.BASIC,
            cost=Cost.HIGH,
            display_name="GROK-4.1-EXPERT",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4.1-thinking",
            grok_model="grok-4-1-thinking-1129",
            model_mode="MODEL_MODE_GROK_4_1_THINKING",
            tier=Tier.BASIC,
            cost=Cost.HIGH,
            display_name="GROK-4.1-THINKING",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4.20-beta",
            grok_model="grok-420",
            model_mode="MODEL_MODE_GROK_420",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-4.20-BETA",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4.20-fast",
            grok_model="grok-420",
            model_mode="MODEL_MODE_FAST",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-4.20-FAST",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-4.3-fast",
            grok_model="grok-43",
            model_mode="MODEL_MODE_FAST",
            tier=Tier.BASIC,
            cost=Cost.LOW,
            display_name="GROK-4.3-FAST",
            description="Fast Grok 4.3 chat model",
            is_image=False,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-imagine-1.0-fast",
            grok_model="grok-4.3",
            model_mode="MODEL_MODE_FAST",
            tier=Tier.SUPER,
            cost=Cost.HIGH,
            display_name="Grok Image Fast",
            description="Imagine waterfall image generation model for chat completions",
            is_image=True,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-imagine-1.0",
            grok_model="grok-4.3",
            model_mode="MODEL_MODE_FAST",
            tier=Tier.BASIC,
            cost=Cost.HIGH,
            display_name="Grok Image",
            description="Image generation model",
            is_image=True,
            is_image_edit=False,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-imagine-1.0-edit",
            grok_model="grok-4.3",
            model_mode="MODEL_MODE_FAST",
            tier=Tier.BASIC,
            cost=Cost.HIGH,
            display_name="Grok Image Edit",
            description="Image edit model",
            is_image=False,
            is_image_edit=True,
            is_video=False,
        ),
        ModelInfo(
            model_id="grok-imagine-1.0-video",
            grok_model="grok-4.3",
            model_mode="MODEL_MODE_FAST",
            tier=Tier.SUPER,
            cost=Cost.HIGH,
            display_name="Grok Video",
            description="Video generation model",
            is_image=False,
            is_image_edit=False,
            is_video=True,
        ),
        *_CONSOLE_MODELS,
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
    def is_console(cls, model_id: str) -> bool:
        model = cls.get(model_id)
        return bool(model and model.channel == Channel.CONSOLE)

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
    def pool_for_model(cls, model_id: str) -> str:
        """根据模型选择 Token 池"""
        model = cls.get(model_id)
        if model and model.channel == Channel.CONSOLE:
            return "ssoBasic"
        if model and model.model_id not in cls.BASIC_MODEL_IDS:
            return "ssoSuper"
        return "ssoBasic"

    @classmethod
    def pool_candidates_for_model(cls, model_id: str) -> List[str]:
        """按优先级返回可用 Token 池列表"""
        model = cls.get(model_id)
        if not model:
            return ["ssoSuper"]
        if model.channel == Channel.CONSOLE or model.model_id in cls.BASIC_MODEL_IDS:
            return ["ssoBasic", "ssoSuper"]
        if model.tier == Tier.SUPER or model.model_id not in cls.BASIC_MODEL_IDS:
            return ["ssoSuper"]
        return ["ssoSuper"]


__all__ = ["Channel", "CONSOLE_MODEL_IDS", "ModelInfo", "ModelService"]
