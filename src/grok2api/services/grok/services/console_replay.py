"""Responses API stateless replay helpers for Console channel."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from grok2api.core.exceptions import ValidationException

_INCREMENTAL_FALLBACK_MESSAGE = (
    "no tool call found for function call output with call_id {call_id}"
)


def first_function_call_output_id(input_items: List[Dict[str, Any]]) -> Optional[str]:
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"function_call_output", "tool_call_output", "tool_output"}:
            call_id = item.get("call_id") or item.get("tool_call_id") or item.get("id")
            if call_id:
                return str(call_id)
    return None


def is_incremental_responses_input(input_items: List[Dict[str, Any]]) -> bool:
    """True when input[] looks like a previous_response_id continuation slice."""
    if not input_items:
        return True

    typed = [item for item in input_items if isinstance(item, dict)]
    if not typed:
        return True

    has_output = any(
        item.get("type") in {"function_call_output", "tool_call_output", "tool_output"}
        for item in typed
    )
    has_call = any(item.get("type") == "function_call" for item in typed)
    has_reasoning = any(item.get("type") == "reasoning" for item in typed)
    has_user_turn = any(
        item.get("role") == "user"
        or (item.get("type") == "message" and item.get("role") == "user")
        for item in typed
    )

    if has_output and not has_call and not has_reasoning:
        return True
    if has_output and not has_call and has_user_turn and len(typed) <= 2:
        return True
    return False


def reject_incremental_previous_response(
    *,
    had_previous_response_id: bool,
    input_items: List[Dict[str, Any]],
) -> None:
    """Reject incremental continuations that require previous_response_id on upstream."""
    if not had_previous_response_id:
        return
    if not is_incremental_responses_input(input_items):
        return
    call_id = first_function_call_output_id(input_items) or "unknown"
    raise ValidationException(
        message=_INCREMENTAL_FALLBACK_MESSAGE.format(call_id=call_id),
        param="input",
        code="invalid_request_error",
    )


__all__ = [
    "first_function_call_output_id",
    "is_incremental_responses_input",
    "reject_incremental_previous_response",
]
