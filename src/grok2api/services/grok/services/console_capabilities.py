"""Console Playground model capabilities and payload filtering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from grok2api.core.logger import logger
from grok2api.services.grok.utils.tool_call import build_tool_prompt
from grok2api.services.reverse.console_constants import (
    CONSOLE_SEARCH_TOOLS,
    CONSOLE_STRICT_PARAM_VALIDATION,
)



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
    supports_function_calling=False,
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
    return [{"type": t} for t in CONSOLE_SEARCH_TOOLS]


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


def partition_console_tools(
    tools: Optional[List[Dict[str, Any]]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    search_tools: List[Dict[str, Any]] = []
    function_tools: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type in CONSOLE_SEARCH_TOOLS:
            search_tools.append(tool)
        elif tool_type == "function":
            function_tools.append(tool)
    return search_tools, function_tools


def prepare_console_tooling(
    *,
    caps: ConsoleModelCapabilities,
    client_tools: Optional[List[Dict[str, Any]]],
    console_search: bool,
    tool_choice: Any,
    parallel_tool_calls: Optional[bool],
    instructions: Optional[str],
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str], Optional[List[Dict[str, Any]]], Any]:
    """Prepare upstream tools/instructions for console requests.

    Native FC models forward client tools (and optional search) upstream.

    Prompt FC models (e.g. multi-agent) always inject function tools into
    ``instructions``; ``console_search`` only adds web/x search tools to the
    upstream payload — function ``tool_choice`` is never forwarded upstream.
    """
    normalized = list(client_tools or [])

    if caps.supports_function_calling:
        merged = merge_tools(normalized, console_search=console_search) or None
        return merged, instructions, None, tool_choice

    upstream_search: List[Dict[str, Any]] = []
    if console_search:
        upstream_search = default_search_tools()
    else:
        for tool in normalized:
            if isinstance(tool, dict) and tool.get("type") in CONSOLE_SEARCH_TOOLS:
                upstream_search.append({"type": str(tool["type"])})

    upstream_tools = upstream_search or None
    prompt_tools: Optional[List[Dict[str, Any]]] = None
    merged_instructions = instructions

    if normalized and tool_choice != "none":
        tool_prompt = build_tool_prompt(normalized, tool_choice, parallel_tool_calls)
        if tool_prompt:
            prompt_tools = normalized
            merged_instructions = (
                f"{tool_prompt}\n\n{instructions}" if instructions else tool_prompt
            )

    return upstream_tools, merged_instructions, prompt_tools, None


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
    strict = CONSOLE_STRICT_PARAM_VALIDATION if strict is None else strict
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
                logger.debug(f"Dropped unsupported reasoning.effort={effort}")

    for field in ("frequency_penalty", "presence_penalty"):
        if field in result and not getattr(caps, f"supports_{field}"):
            if strict:
                raise ValueError(f"{field} not supported for model")
            result.pop(field, None)
            logger.debug(f"Dropped unsupported {field}")

    tools = result.get("tools")
    if isinstance(tools, list) and not caps.supports_function_calling:
        search_tools, _ = partition_console_tools(tools)
        if search_tools:
            result["tools"] = search_tools
        else:
            result.pop("tools", None)
            result.pop("tool_choice", None)

    return result


__all__ = [
    "ConsoleModelCapabilities",
    "CONSOLE_CAPABILITIES",
    "default_search_tools",
    "filter_payload",
    "get_console_capabilities",
    "merge_tools",
    "partition_console_tools",
    "prepare_console_tooling",
    "should_include_encrypted",
]
