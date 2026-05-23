"""Sanitize Console client requests before forwarding to console.x.ai upstream."""

from __future__ import annotations

from typing import Any, Dict, Mapping, MutableMapping

from grok2api.core.logger import logger

# Top-level fields accepted by console.x.ai /v1/responses.
CONSOLE_UPSTREAM_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "input",
        "instructions",
        "stream",
        "store",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "temperature",
        "top_p",
        "max_output_tokens",
        "reasoning",
        "include",
        "text",
        "frequency_penalty",
        "presence_penalty",
        "previous_response_id",
    }
)

# Client-only fields that must never be forwarded upstream.
CONSOLE_STRIP_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "stream_options",
        "prompt_cache_key",
        "prompt_cache_retention",
        "thinking",
        "output_config",
        "metadata",
        "user",
        "previous_response_id",
        "service_tier",
        "safety_identifier",
        "truncation",
        "max_tool_calls",
        "background",
        "context_management",
        "conversation",
        "prompt",
        "reasoning_effort",
        "verbosity",
        "web_search_options",
        "modalities",
        "audio",
        "prediction",
        "logprobs",
        "top_logprobs",
        "seed",
        "n",
        "stop",
        "logit_bias",
    }
)


def strip_console_client_extra(extra: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Drop client-only request fields (Undefined / OpenAI SDK extras)."""
    if not extra:
        return {}
    cleaned: Dict[str, Any] = {}
    for key, value in extra.items():
        if key in CONSOLE_STRIP_REQUEST_FIELDS:
            logger.debug(f"Console: dropped client field {key!r}")
            continue
        if value is not None:
            cleaned[key] = value
    return cleaned


def sanitize_console_upstream_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep only upstream-known top-level payload keys."""
    result: Dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in payload.items():
        if value is None:
            continue
        if key in CONSOLE_UPSTREAM_PAYLOAD_KEYS:
            result[key] = value
        else:
            dropped.append(key)
    if dropped:
        logger.debug(f"Console: dropped upstream payload fields: {', '.join(dropped)}")
    return result


def merge_console_payload(
    payload: MutableMapping[str, Any],
    extra: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    """Merge stripped client extras then whitelist for upstream."""
    merged = dict(payload)
    merged.update(strip_console_client_extra(extra))
    return sanitize_console_upstream_payload(merged)


__all__ = [
    "CONSOLE_STRIP_REQUEST_FIELDS",
    "CONSOLE_UPSTREAM_PAYLOAD_KEYS",
    "merge_console_payload",
    "sanitize_console_upstream_payload",
    "strip_console_client_extra",
]
