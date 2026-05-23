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
    CONSOLE_VOICE = "console_voice"


OWNER_SOURCE = "grok2api@69gg"


def _owned_by(platform: str) -> str:
    return f"{platform}<{OWNER_SOURCE}>"


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
            return _owned_by("xai-console")
        if self.channel == Channel.CONSOLE_VOICE:
            return _owned_by("xai-console-voice")
        return _owned_by("grok-app")


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


def _console_voice_model(
    model_id: str,
    *,
    display_name: Optional[str] = None,
    description: str = "",
) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        grok_model=model_id,
        model_mode="CONSOLE_VOICE",
        tier=Tier.BASIC,
        cost=Cost.LOW,
        display_name=display_name or model_id.upper(),
        description=description,
        channel=Channel.CONSOLE_VOICE,
    )


_CONSOLE_VOICE_MODELS = [
    _console_voice_model(
        "grok-tts-1",
        display_name="Grok TTS",
        description="Console Voice Playground text-to-speech",
    ),
    _console_voice_model(
        "grok-stt-1",
        display_name="Grok STT",
        description="Console Voice Playground speech-to-text",
    ),
]

CONSOLE_VOICE_MODEL_IDS = {m.model_id for m in _CONSOLE_VOICE_MODELS}
CONSOLE_VOICE_TTS_MODEL_IDS = {"grok-tts-1"}
CONSOLE_VOICE_STT_MODEL_IDS = {"grok-stt-1"}


def _super_grok_model(
    model_id: str,
    *,
    model_mode: str,
    grok_model: Optional[str] = None,
    display_name: Optional[str] = None,
    description: str = "",
    is_image: bool = False,
    is_image_edit: bool = False,
    is_video: bool = False,
    cost: Cost = Cost.LOW,
) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        grok_model=grok_model or model_id,
        model_mode=model_mode,
        tier=Tier.SUPER,
        cost=cost,
        display_name=display_name or model_id.upper(),
        description=description,
        is_image=is_image,
        is_image_edit=is_image_edit,
        is_video=is_video,
    )


_SUPER_GROK_MODELS = [
    # Chat
    _super_grok_model("grok-4.20-0309", model_mode="MODEL_MODE_AUTO"),
    _super_grok_model("grok-4.20-0309-reasoning", model_mode="MODEL_MODE_EXPERT"),
    _super_grok_model("grok-4.20-0309-non-reasoning-super", model_mode="MODEL_MODE_FAST"),
    _super_grok_model("grok-4.20-0309-super", model_mode="MODEL_MODE_AUTO"),
    _super_grok_model("grok-4.20-0309-reasoning-super", model_mode="MODEL_MODE_EXPERT"),
    _super_grok_model(
        "grok-4.20-auto",
        grok_model="grok-420",
        model_mode="MODEL_MODE_AUTO",
        description="Prefer ssoSuper token pool",
    ),
    _super_grok_model(
        "grok-4.20-expert",
        grok_model="grok-420",
        model_mode="MODEL_MODE_EXPERT",
        description="Prefer ssoSuper token pool",
    ),
    _super_grok_model(
        "grok-4.3-beta",
        grok_model="grok-43",
        model_mode="grok-420-computer-use-sa",
    ),
    # Image
    _super_grok_model(
        "grok-imagine-image",
        model_mode="MODEL_MODE_AUTO",
        cost=Cost.HIGH,
        is_image=True,
        display_name="Grok Imagine Image",
    ),
    _super_grok_model(
        "grok-imagine-image-pro",
        model_mode="MODEL_MODE_AUTO",
        cost=Cost.HIGH,
        is_image=True,
        display_name="Grok Imagine Image Pro",
        description="Imagine waterfall image generation model for chat completions",
    ),
    # Image edit
    _super_grok_model(
        "grok-imagine-image-edit",
        model_mode="MODEL_MODE_AUTO",
        cost=Cost.HIGH,
        is_image_edit=True,
        display_name="Grok Imagine Image Edit",
    ),
    # Video (legacy route; super pool only)
    _super_grok_model(
        "grok-imagine-1.0-video",
        grok_model="grok-43",
        model_mode="MODEL_MODE_AUTO",
        cost=Cost.HIGH,
        is_video=True,
        display_name="Grok Video",
        description="Video generation model",
    ),
]

SUPER_GROK_MODEL_IDS = {m.model_id for m in _SUPER_GROK_MODELS}


class ModelService:
    """模型管理服务"""

    BASIC_MODEL_IDS = {
        "grok-4.3-fast",
        *CONSOLE_MODEL_IDS,
        *CONSOLE_VOICE_MODEL_IDS,
    }

    MODELS = [
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
        *_SUPER_GROK_MODELS,
        *_CONSOLE_MODELS,
        *_CONSOLE_VOICE_MODELS,
    ]

    _map = {m.model_id: m for m in MODELS}

    @classmethod
    def get(cls, model_id: str) -> Optional[ModelInfo]:
        """获取模型信息"""
        return cls._map.get(model_id)

    @classmethod
    def list(cls, *, has_super_tokens: bool = True) -> list[ModelInfo]:
        """获取模型列表；无 ssoSuper token 时不返回仅 super 池可用的模型。"""
        models = list(cls._map.values())
        if has_super_tokens:
            return models
        return [m for m in models if not cls.is_super_pool_only(m.model_id)]

    @classmethod
    def is_super_pool_only(cls, model_id: str) -> bool:
        """模型是否仅能通过 ssoSuper 池调用。"""
        return cls.pool_candidates_for_model(model_id) == ["ssoSuper"]

    @classmethod
    def is_console(cls, model_id: str) -> bool:
        model = cls.get(model_id)
        return bool(model and model.channel in {Channel.CONSOLE, Channel.CONSOLE_VOICE})

    @classmethod
    def is_console_voice(cls, model_id: str) -> bool:
        model = cls.get(model_id)
        return bool(model and model.channel == Channel.CONSOLE_VOICE)

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
        if model and model.channel in {Channel.CONSOLE, Channel.CONSOLE_VOICE}:
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
        if (
            model.channel in {Channel.CONSOLE, Channel.CONSOLE_VOICE}
            or model.model_id in cls.BASIC_MODEL_IDS
        ):
            return ["ssoBasic", "ssoSuper"]
        if model.tier == Tier.SUPER or model.model_id not in cls.BASIC_MODEL_IDS:
            return ["ssoSuper"]
        return ["ssoSuper"]


__all__ = [
    "Channel",
    "CONSOLE_MODEL_IDS",
    "CONSOLE_VOICE_MODEL_IDS",
    "CONSOLE_VOICE_STT_MODEL_IDS",
    "CONSOLE_VOICE_TTS_MODEL_IDS",
    "SUPER_GROK_MODEL_IDS",
    "OWNER_SOURCE",
    "ModelInfo",
    "ModelService",
]
