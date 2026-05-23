"""Console Playground model capabilities and payload filtering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from grok2api.core.config import get_config
from grok2api.core.logger import logger


SEARCH_TOOL_TYPES = ("web_search_preview", "x_search")


@dataclass(frozen=True)
class ConsoleModelCapabilities:
    supports_image_input: bool = True
    supports_structured_output: bool = True
    supports_reasoning_output: bool = False
    supports_function_calling: bool = True
    supports_reasoning_effort: bool = False
    supports_frequency_penalty: bool = False
    supports_presence_penalty: bool = False
    supports_reasoning_summary: bool = False
    supports_encrypted_reasoning: bool = False
    default_reasoning_mode: str = "none"  # summary | encrypted | none
    max_output_tokens: int = 1_000_000
    reasoning_effort_options: tuple[str, ...] = ("none", "low", "medium", "high")


CAP_GROK_43 = ConsoleModelCapabilities(
    supports_reasoning_output=True,
    supports_reasoning_effort=True,
    supports_reasoning_summary=True,
    supports_encrypted_reasoning=True,
    default_reasoning_mode="summary",
    max_output_tokens=1_000_000,
    reasoning_effort_options=("none", "low", "medium", "high"),
)

CAP_BUILD = ConsoleModelCapabilities(
    supports_reasoning_output=True,
    supports_encrypted_reasoning=True,
    default_reasoning_mode="encrypted",
    max_output_tokens=256_000,
)

CAP_420_NON_REASONING = ConsoleModelCapabilities(
    supports_reasoning_output=False,
    supports_frequency_penalty=True,
    supports_presence_penalty=True,
    default_reasoning_mode="none",
    max_output_tokens=1_000_000,
)

CAP_420_REASONING = ConsoleModelCapabilities(
    supports_reasoning_output=True,
    supports_encrypted_reasoning=True,
    default_reasoning_mode="encrypted",
    max_output_tokens=1_000_000,
)

CAP_MULTI_AGENT = ConsoleModelCapabilities(
    supports_reasoning_output=True,
    supports_encrypted_reasoning=True,
    default_reasoning_mode="encrypted",
    max_output_tokens=1_000_000,
)

CONSOLE_CAPABILITIES: Dict[str, ConsoleModelCapabilities] = {
    "grok-4.3": CAP_GROK_43,
    "grok-build-0.1": CAP_BUILD,
    "grok-4.20-0309-non-reasoning": CAP_420_NON_REASONING,
    "grok-4.20-0309-reasoning": CAP_420_REASONING,
    "grok-4.20-multi-agent-0309": CAP_MULTI_AGENT,
}


def get_console_capabilities(console_model: str) -> ConsoleModelCapabilities:
    return CONSOLE_CAPABILITIES.get(console_model, CAP_GROK_43)


def default_search_tools() -> List[Dict[str, str]]:
    configured = get_config("console.search_tools")
    if isinstance(configured, list) and configured:
        return [{"type": str(t)} for t in configured if t]
    return [{"type": t} for t in SEARCH_TOOL_TYPES]


def merge_tools(
    user_tools: Optional[List[Dict[str, Any]]],
    *,
    console_search: bool,
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    if user_tools:
        merged.extend(user_tools)
    if console_search:
        existing = {t.get("type") for t in merged if isinstance(t, dict)}
        for tool in default_search_tools():
            if tool["type"] not in existing:
                merged.append(dict(tool))
    return merged


def should_include_encrypted(
    caps: ConsoleModelCapabilities,
    *,
    reasoning_effort: Optional[str],
    thinking_enabled: bool,
    request_include: Optional[List[str]],
    history_has_encrypted: bool,
) -> bool:
    if request_include and "reasoning.encrypted_content" in request_include:
        return True
    if history_has_encrypted:
        return caps.supports_encrypted_reasoning
    if not caps.supports_reasoning_output:
        return False
    if caps.default_reasoning_mode == "encrypted":
        return True
    if caps.supports_reasoning_summary and caps.default_reasoning_mode == "summary":
        effort = (reasoning_effort or "low").lower()
        if effort in ("none", ""):
            return False
        if thinking_enabled:
            return False
        return False
    return caps.supports_encrypted_reasoning and (thinking_enabled or bool(reasoning_effort))


def filter_payload(
    caps: ConsoleModelCapabilities,
    payload: Dict[str, Any],
    *,
    strict: Optional[bool] = None,
) -> Dict[str, Any]:
    strict = (
        bool(get_config("console.strict_param_validation"))
        if strict is None
        else strict
    )
    result = dict(payload)

    max_tokens = result.get("max_output_tokens")
    if max_tokens is not None:
        try:
            result["max_output_tokens"] = min(int(max_tokens), caps.max_output_tokens)
        except (TypeError, ValueError):
            result.pop("max_output_tokens", None)

    reasoning = result.get("reasoning")
    if isinstance(reasoning, dict):
        reasoning = dict(reasoning)
        if not caps.supports_reasoning_effort:
            reasoning.pop("effort", None)
            if not reasoning:
                result.pop("reasoning", None)
            else:
                result["reasoning"] = reasoning
        elif reasoning.get("effort") is not None:
            effort = str(reasoning.get("effort")).lower()
            allowed = set(caps.reasoning_effort_options)
            if effort not in allowed:
                if strict:
                    raise ValueError(f"reasoning.effort '{effort}' not supported for model")
                reasoning.pop("effort", None)
                logger.debug("Dropped unsupported reasoning.effort=%s", effort)

    for field in ("frequency_penalty", "presence_penalty"):
        if field in result and not getattr(caps, f"supports_{field}"):
            if strict:
                raise ValueError(f"{field} not supported for model")
            result.pop(field, None)
            logger.debug("Dropped unsupported %s", field)

    return result


__all__ = [
    "ConsoleModelCapabilities",
    "CONSOLE_CAPABILITIES",
    "default_search_tools",
    "filter_payload",
    "get_console_capabilities",
    "merge_tools",
    "should_include_encrypted",
]
