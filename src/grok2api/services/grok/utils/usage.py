"""
OpenAI 兼容 usage 估算与格式转换工具。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional

import orjson


_TOKEN_SEGMENT_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_PROMPT_OVERHEAD_TOKENS = 4


def _compact_json(value: Any) -> str:
    return orjson.dumps(
        value,
        option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS,
    ).decode("utf-8")


def estimate_tokens(value: Any) -> int:
    """
    对任意 payload 做轻量 token 估算。
    """
    if value is None:
        return 0

    if isinstance(value, (bytes, bytearray)):
        if not value:
            return 0
        return max(1, math.ceil(len(value) / 4))

    if not isinstance(value, str):
        try:
            value = _compact_json(value)
        except Exception:
            value = str(value)

    text = value.strip()
    if not text:
        return 0

    byte_estimate = math.ceil(len(text.encode("utf-8")) / 4)
    segment_estimate = math.ceil(len(_TOKEN_SEGMENT_RE.findall(text)) * 0.75)
    return max(1, byte_estimate, segment_estimate)


def estimate_prompt_tokens(value: Any) -> int:
    if value is None:
        return 0
    prompt_tokens = estimate_tokens(value)
    if prompt_tokens <= 0:
        return 0
    return prompt_tokens + _PROMPT_OVERHEAD_TOKENS


def estimate_completion_tokens(
    *,
    content: Optional[str] = None,
    tool_calls: Optional[list[dict[str, Any]]] = None,
    reasoning_content: Optional[str] = None,
) -> int:
    completion_tokens = estimate_tokens(content)
    if tool_calls:
        completion_tokens += estimate_tokens(tool_calls)
    if reasoning_content:
        completion_tokens += estimate_tokens(reasoning_content)
    return completion_tokens


def build_chat_usage(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    reasoning_tokens: int = 0,
) -> Dict[str, Any]:
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    reasoning_tokens = max(0, int(reasoning_tokens or 0))
    total_tokens = prompt_tokens + completion_tokens
    text_tokens = max(0, completion_tokens - reasoning_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "prompt_tokens_details": {
            "cached_tokens": 0,
            "text_tokens": prompt_tokens,
            "audio_tokens": 0,
            "image_tokens": 0,
        },
        "completion_tokens_details": {
            "text_tokens": text_tokens,
            "audio_tokens": 0,
            "reasoning_tokens": reasoning_tokens,
        },
    }


def estimate_chat_usage(
    *,
    prompt_tokens: int,
    content: Optional[str] = None,
    tool_calls: Optional[list[dict[str, Any]]] = None,
    reasoning_content: Optional[str] = None,
) -> Dict[str, Any]:
    completion_tokens = estimate_completion_tokens(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )
    return build_chat_usage(
        prompt_tokens,
        completion_tokens,
        reasoning_tokens=estimate_tokens(reasoning_content),
    )


def normalize_chat_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not usage:
        return build_chat_usage(0, 0)

    prompt_tokens = usage.get("prompt_tokens")
    if prompt_tokens is None:
        prompt_tokens = usage.get("input_tokens", 0)

    completion_tokens = usage.get("completion_tokens")
    if completion_tokens is None:
        completion_tokens = usage.get("output_tokens", 0)

    reasoning_tokens = int(
        (
            usage.get("completion_tokens_details")
            or usage.get("output_tokens_details")
            or {}
        ).get("reasoning_tokens")
        or 0
    )
    return build_chat_usage(
        prompt_tokens,
        completion_tokens,
        reasoning_tokens=reasoning_tokens,
    )


def to_responses_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    chat_usage = normalize_chat_usage(usage)
    prompt_tokens = chat_usage["prompt_tokens"]
    completion_tokens = chat_usage["completion_tokens"]
    total_tokens = chat_usage["total_tokens"]
    prompt_details = chat_usage.get("prompt_tokens_details") or {}

    return {
        "input_tokens": prompt_tokens,
        "input_tokens_details": {
            "cached_tokens": int(prompt_details.get("cached_tokens") or 0),
            "text_tokens": int(prompt_details.get("text_tokens") or prompt_tokens),
            "image_tokens": int(prompt_details.get("image_tokens") or 0),
        },
        "output_tokens": completion_tokens,
        "output_tokens_details": {
            "text_tokens": int(
                (
                    chat_usage.get("completion_tokens_details") or {}
                ).get("text_tokens")
                or completion_tokens
            ),
            "reasoning_tokens": int(
                (
                    chat_usage.get("completion_tokens_details") or {}
                ).get("reasoning_tokens")
                or 0
            ),
        },
        "total_tokens": total_tokens,
    }


__all__ = [
    "build_chat_usage",
    "estimate_chat_usage",
    "estimate_completion_tokens",
    "estimate_prompt_tokens",
    "estimate_tokens",
    "normalize_chat_usage",
    "to_responses_usage",
]
