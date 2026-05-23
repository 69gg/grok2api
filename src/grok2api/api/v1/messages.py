"""
Anthropic Messages API compatibility layer.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncGenerator, AsyncIterable, Dict, List, Optional, Tuple

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from grok2api.core.auth import get_admin_api_keys, is_valid_admin_api_key
from grok2api.core.config import config
from grok2api.core.exceptions import (
    AppException,
    AuthenticationException,
    ErrorType,
    ValidationException,
)
from grok2api.core.logger import logger
from grok2api.services.grok.services.chat import ChatService
from grok2api.services.grok.services.console_channel import ConsoleChannelService
from grok2api.services.grok.services.model import Channel, ModelService
from grok2api.services.grok.utils import process as proc_base


router = APIRouter(tags=["Anthropic Messages"])


_SUPPORTED_MESSAGE_ROLES = {"user", "assistant"}
_SUPPORTED_SYSTEM_BLOCK_TYPES = {"text"}
_SUPPORTED_ASSISTANT_BLOCK_TYPES = {"text", "tool_use", "thinking"}
_SUPPORTED_USER_BLOCK_TYPES = {"text", "tool_result"}
_THINKING_ENABLED_TYPES = {"enabled", "on", "true"}
_THINKING_DISABLED_TYPES = {"disabled", "off", "false"}
class AnthropicMessageInput(BaseModel):
    role: str
    content: Any

    model_config = ConfigDict(extra="allow")


class AnthropicMessagesRequest(BaseModel):
    model: str = Field(..., description="Model name")
    max_tokens: int = Field(..., description="Maximum output tokens")
    messages: List[AnthropicMessageInput] = Field(..., description="Anthropic messages")
    system: Optional[Any] = Field(None, description="Anthropic system prompt")
    stream: Optional[bool] = Field(False, description="Whether to stream")
    temperature: Optional[float] = Field(None, description="Sampling temperature")
    top_p: Optional[float] = Field(None, description="Nucleus sampling")
    tools: Optional[List[Dict[str, Any]]] = Field(None, description="Anthropic tools")
    tool_choice: Optional[Any] = Field(None, description="Anthropic tool choice")
    stop_sequences: Optional[List[str]] = Field(None, description="Stop sequences")
    thinking: Optional[Any] = Field(None, description="Anthropic thinking config")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadata")

    model_config = ConfigDict(extra="allow")


def _anthropic_error_payload(
    message: str,
    *,
    error_type: str = "invalid_request_error",
) -> Dict[str, Any]:
    return {
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        },
    }


def _anthropic_json_error(
    message: str,
    *,
    status_code: int = 400,
    error_type: str = "invalid_request_error",
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_anthropic_error_payload(message, error_type=error_type),
    )


def _anthropic_stream_error(message: str, *, error_type: str = "invalid_request_error") -> StreamingResponse:
    async def _gen() -> AsyncGenerator[str, None]:
        payload = _anthropic_error_payload(message, error_type=error_type)
        yield f"event: error\ndata: {orjson.dumps(payload).decode()}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


def _extract_bearer_token(request: Request) -> Optional[str]:
    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


async def _authenticate_anthropic_request(request: Request) -> Optional[str]:
    await config.ensure_loaded()

    version = (request.headers.get("anthropic-version") or "").strip()
    if not version:
        raise ValidationException(
            message="anthropic-version header is required",
            param="headers.anthropic-version",
            code="missing_anthropic_version",
        )

    api_keys = get_admin_api_keys()
    if not api_keys:
        return None

    token = (request.headers.get("x-api-key") or "").strip() or _extract_bearer_token(request)
    if not token:
        raise AuthenticationException("Missing API key")
    if not is_valid_admin_api_key(token):
        raise AuthenticationException("Invalid API key")
    return token


def _coerce_text_blocks(
    content: Any,
    *,
    param_prefix: str,
    allowed_types: set[str],
) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise ValidationException(
            message="content must be a string or an array of content blocks",
            param=param_prefix,
            code="invalid_content",
        )

    blocks: List[Dict[str, Any]] = []
    for idx, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type not in allowed_types:
            continue
        blocks.append(block)
    return blocks


def _extract_text_from_text_block(block: Dict[str, Any], *, param_prefix: str) -> str:
    text = block.get("text")
    if not isinstance(text, str):
        raise ValidationException(
            message="text blocks must include a string text field",
            param=f"{param_prefix}.text",
            code="invalid_content_block",
        )
    return text


def _normalize_system_messages(system: Any) -> List[Dict[str, Any]]:
    if system is None:
        return []
    if isinstance(system, str):
        return [{"role": "system", "content": system}]
    blocks = _coerce_text_blocks(
        system,
        param_prefix="system",
        allowed_types=_SUPPORTED_SYSTEM_BLOCK_TYPES,
    )
    texts: List[str] = []
    for idx, block in enumerate(blocks):
        text = _extract_text_from_text_block(block, param_prefix=f"system.{idx}")
        if text.strip():
            texts.append(text)
    if not texts:
        return []
    return [{"role": "system", "content": "\n\n".join(texts)}]


def _normalize_tool_result_content(content: Any, *, param_prefix: str) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: List[str] = []
        for idx, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = _extract_text_from_text_block(block, param_prefix=f"{param_prefix}.{idx}")
            if text:
                texts.append(text)
        return "\n".join(texts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _anthropic_tool_to_chat_tool(tool: Dict[str, Any], *, index: int) -> Optional[Dict[str, Any]]:
    if not isinstance(tool, dict):
        return None
    if tool.get("type") not in {None, "custom", "function"}:
        return None
    name = tool.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    input_schema = tool.get("input_schema")
    if input_schema is None:
        input_schema = {"type": "object", "properties": {}, "additionalProperties": True}
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}, "additionalProperties": True}
    return {
        "type": "function",
        "function": {
            "name": name.strip(),
            "description": str(tool.get("description") or ""),
            "parameters": input_schema,
        },
    }


def _normalize_anthropic_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for idx, tool in enumerate(tools or []):
        item = _anthropic_tool_to_chat_tool(tool, index=idx)
        if item:
            normalized.append(item)
    return normalized


def _normalize_anthropic_tool_choice(
    tool_choice: Any,
    *,
    tools_present: bool,
) -> Tuple[Optional[Any], bool]:
    parallel_tool_calls = True
    if tool_choice is None:
        return None, parallel_tool_calls
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return "auto", parallel_tool_calls
        if tool_choice == "none":
            return "none", parallel_tool_calls
        if tool_choice == "any":
            return ("required" if tools_present else None), parallel_tool_calls
        return None, parallel_tool_calls
    if not isinstance(tool_choice, dict):
        return None, parallel_tool_calls
    if tool_choice.get("disable_parallel_tool_use") is True:
        parallel_tool_calls = False
    choice_type = tool_choice.get("type")
    if choice_type in {None, "auto"}:
        return "auto", parallel_tool_calls
    if choice_type == "none":
        return "none", parallel_tool_calls
    if choice_type == "any":
        return ("required" if tools_present else None), parallel_tool_calls
    if choice_type == "tool":
        name = tool_choice.get("name")
        if not isinstance(name, str) or not name.strip() or not tools_present:
            return None, parallel_tool_calls
        return {"type": "function", "function": {"name": name.strip()}}, parallel_tool_calls
    return None, parallel_tool_calls


def _thinking_enabled(thinking: Any) -> bool:
    if thinking is None:
        return False
    if isinstance(thinking, bool):
        return thinking
    if isinstance(thinking, str):
        lowered = thinking.strip().lower()
        if lowered in _THINKING_ENABLED_TYPES:
            return True
        if lowered in _THINKING_DISABLED_TYPES:
            return False
    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").strip().lower()
        if thinking_type in _THINKING_ENABLED_TYPES:
            return True
        if thinking_type in _THINKING_DISABLED_TYPES:
            return False
    return False


def _normalize_anthropic_messages(messages: List[AnthropicMessageInput]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    pending_tool_use_ids: List[str] = []

    for msg_index, message in enumerate(messages):
        role = message.role
        if role not in _SUPPORTED_MESSAGE_ROLES:
            raise ValidationException(
                message=f"unsupported role: {role}",
                param=f"messages.{msg_index}.role",
                code="invalid_role",
            )

        if pending_tool_use_ids and role != "user":
            raise ValidationException(
                message="assistant tool_use blocks must be followed by a user message containing tool_result",
                param=f"messages.{msg_index}.role",
                code="invalid_message_sequence",
            )

        if role == "assistant":
            blocks = _coerce_text_blocks(
                message.content,
                param_prefix=f"messages.{msg_index}.content",
                allowed_types=_SUPPORTED_ASSISTANT_BLOCK_TYPES,
            )
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []

            for block_index, block in enumerate(blocks):
                block_type = block.get("type")
                if block_type == "thinking":
                    continue
                if block_type == "text":
                    text = _extract_text_from_text_block(
                        block,
                        param_prefix=f"messages.{msg_index}.content.{block_index}",
                    )
                    if text:
                        text_parts.append(text)
                    continue
                if block_type == "tool_use":
                    tool_id = block.get("id") or f"call_{uuid.uuid4().hex[:24]}"
                    name = block.get("name")
                    if not isinstance(name, str) or not name.strip():
                        raise ValidationException(
                            message="tool_use blocks require a name",
                            param=f"messages.{msg_index}.content.{block_index}.name",
                            code="invalid_content_block",
                        )
                    tool_input = block.get("input")
                    if tool_input is None:
                        tool_input = {}
                    if not isinstance(tool_input, dict):
                        raise ValidationException(
                            message="tool_use.input must be an object",
                            param=f"messages.{msg_index}.content.{block_index}.input",
                            code="invalid_content_block",
                        )
                    tool_calls.append(
                        {
                            "id": str(tool_id),
                            "type": "function",
                            "function": {
                                "name": name.strip(),
                                "arguments": orjson.dumps(tool_input).decode(),
                            },
                        }
                    )
                    continue

            if text_parts or tool_calls:
                internal_message: Dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n\n".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    internal_message["tool_calls"] = tool_calls
                    pending_tool_use_ids = [tool_call["id"] for tool_call in tool_calls]
                else:
                    pending_tool_use_ids = []
                normalized.append(internal_message)
            continue

        blocks = _coerce_text_blocks(
            message.content,
            param_prefix=f"messages.{msg_index}.content",
            allowed_types=_SUPPORTED_USER_BLOCK_TYPES,
        )
        tool_result_ids: List[str] = []
        user_text_parts: List[str] = []
        seen_text = False
        for block_index, block in enumerate(blocks):
            block_type = block.get("type")
            if block_type == "text":
                seen_text = True
                text = _extract_text_from_text_block(
                    block,
                    param_prefix=f"messages.{msg_index}.content.{block_index}",
                )
                if text:
                    user_text_parts.append(text)
                continue
            if seen_text:
                raise ValidationException(
                    message="tool_result blocks must appear before user text in the same message",
                    param=f"messages.{msg_index}.content.{block_index}.type",
                    code="invalid_message_sequence",
                )
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id.strip():
                raise ValidationException(
                    message="tool_result blocks require tool_use_id",
                    param=f"messages.{msg_index}.content.{block_index}.tool_use_id",
                    code="invalid_content_block",
                )
            tool_result_ids.append(tool_use_id.strip())
            normalized.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_use_id.strip(),
                    "content": _normalize_tool_result_content(
                        block.get("content"),
                        param_prefix=f"messages.{msg_index}.content.{block_index}.content",
                    ),
                }
            )

        if pending_tool_use_ids:
            if not tool_result_ids:
                raise ValidationException(
                    message="assistant tool_use blocks must be followed by tool_result blocks in the next user message",
                    param=f"messages.{msg_index}.content",
                    code="invalid_message_sequence",
                )
            missing_ids = [tool_id for tool_id in pending_tool_use_ids if tool_id not in tool_result_ids]
            if missing_ids:
                raise ValidationException(
                    message="user tool_result blocks must cover all previous assistant tool_use ids",
                    param=f"messages.{msg_index}.content",
                    code="invalid_message_sequence",
                )
            pending_tool_use_ids = []
        elif tool_result_ids:
            raise ValidationException(
                message="tool_result blocks require a preceding assistant tool_use block",
                param=f"messages.{msg_index}.content",
                code="invalid_message_sequence",
            )

        if user_text_parts:
            normalized.append({"role": "user", "content": "\n\n".join(user_text_parts)})

    return normalized


def _to_anthropic_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
    }


def _pick_stop_sequence(text: Optional[str], stop_sequences: Optional[List[str]]) -> Optional[str]:
    if not isinstance(text, str) or not text or not stop_sequences:
        return None
    for sequence in stop_sequences:
        if isinstance(sequence, str) and sequence and text.endswith(sequence):
            return sequence
    return None


def _map_stop_reason(
    finish_reason: Optional[str],
    *,
    has_tool_calls: bool,
    text_content: Optional[str],
    stop_sequences: Optional[List[str]],
) -> Tuple[str, Optional[str]]:
    if has_tool_calls:
        return "tool_use", None
    matched_sequence = _pick_stop_sequence(text_content, stop_sequences)
    if matched_sequence:
        return "stop_sequence", matched_sequence
    if finish_reason in {"length", "max_tokens"}:
        return "max_tokens", None
    return "end_turn", None


def _format_thinking_text(text: str) -> str:
    if not text:
        return text
    result = text
    result = re.sub(r"([a-z])([A-Z][a-z])", r"\1\n\2", result)
    result = re.sub(r"([^\n])(-\s)", r"\1\n\2", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _build_anthropic_content_blocks(
    *,
    content: Optional[str],
    tool_calls: Optional[List[Dict[str, Any]]],
    reasoning_text: Optional[str],
    include_thinking: bool,
) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    if include_thinking and reasoning_text:
        blocks.append(
            {
                "type": "thinking",
                "thinking": _format_thinking_text(reasoning_text),
                "signature": "",
            }
        )
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    for tool_call in tool_calls or []:
        function_obj = tool_call.get("function") or {}
        arguments_text = function_obj.get("arguments") or "{}"
        try:
            tool_input = orjson.loads(arguments_text)
        except orjson.JSONDecodeError:
            tool_input = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "name": function_obj.get("name"),
                "input": tool_input if isinstance(tool_input, dict) else {},
            }
        )
    return blocks


def _build_anthropic_message_response(
    *,
    model: str,
    content: Optional[str],
    tool_calls: Optional[List[Dict[str, Any]]],
    reasoning_text: Optional[str],
    finish_reason: Optional[str],
    usage: Optional[Dict[str, Any]],
    stop_sequences: Optional[List[str]],
    include_thinking: bool,
    message_id: Optional[str] = None,
) -> Dict[str, Any]:
    stop_reason, stop_sequence = _map_stop_reason(
        finish_reason,
        has_tool_calls=bool(tool_calls),
        text_content=content,
        stop_sequences=stop_sequences,
    )
    return {
        "id": message_id or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": _build_anthropic_content_blocks(
            content=content,
            tool_calls=tool_calls,
            reasoning_text=reasoning_text,
            include_thinking=include_thinking,
        ),
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "usage": _to_anthropic_usage(usage),
    }


class AnthropicStreamAdapter:
    def __init__(
        self,
        *,
        model: str,
        stop_sequences: Optional[List[str]],
        include_thinking: bool,
    ):
        self.model = model
        self.stop_sequences = stop_sequences
        self.include_thinking = include_thinking
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.message_started = False
        self.output_blocks: List[Dict[str, Any]] = []
        self.current_block_type: Optional[str] = None
        self.current_block_index: Optional[int] = None
        self.current_text = ""
        self.usage_input_tokens = 0
        self.usage_output_tokens = 0
        self.final_stop_reason = "end_turn"
        self.final_stop_sequence: Optional[str] = None
        self.final_content_text = ""
        self.has_tool_calls = False
        self.reasoning_chars = 0
        self.thinking_blocks_emitted = 0
        self.tool_blocks_emitted = 0

    def _event(self, event_type: str, payload: Dict[str, Any]) -> str:
        return f"event: {event_type}\ndata: {orjson.dumps(payload).decode()}\n\n"

    def _message_snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.message_id,
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": self.output_blocks,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": self.usage_input_tokens,
                "output_tokens": self.usage_output_tokens,
            },
        }

    def ensure_message_started(self) -> List[str]:
        if self.message_started:
            return []
        self.message_started = True
        return [
            self._event(
                "message_start",
                {
                    "type": "message_start",
                    "message": self._message_snapshot(),
                },
            )
        ]

    def _append_block(self, block: Dict[str, Any]) -> int:
        self.output_blocks.append(block)
        return len(self.output_blocks) - 1

    def _close_current_block(self) -> List[str]:
        if self.current_block_index is None:
            return []
        index = self.current_block_index
        self.current_block_index = None
        self.current_block_type = None
        self.current_text = ""
        return [
            self._event(
                "content_block_stop",
                {
                    "type": "content_block_stop",
                    "index": index,
                },
            )
        ]

    def _ensure_text_block(self, block_type: str) -> List[str]:
        if self.current_block_type == block_type and self.current_block_index is not None:
            return []
        events = self._close_current_block()
        block: Dict[str, Any]
        if block_type == "thinking":
            block = {"type": "thinking", "thinking": "", "signature": ""}
        else:
            block = {"type": "text", "text": ""}
        index = self._append_block(block)
        self.current_block_type = block_type
        self.current_block_index = index
        if block_type == "thinking":
            self.thinking_blocks_emitted += 1
        events.append(
            self._event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": block,
                },
            )
        )
        return events

    def ingest_reasoning(self, delta: str) -> List[str]:
        if not self.include_thinking or not delta:
            return []
        events = self._ensure_text_block("thinking")
        self.current_text += delta
        self.reasoning_chars += len(delta)
        self.output_blocks[self.current_block_index]["thinking"] = _format_thinking_text(self.current_text)
        events.append(
            self._event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.current_block_index,
                    "delta": {"type": "thinking_delta", "thinking": delta},
                },
            )
        )
        return events

    def ingest_text(self, delta: str) -> List[str]:
        if not delta:
            return []
        events = self._ensure_text_block("text")
        self.current_text += delta
        self.final_content_text += delta
        self.output_blocks[self.current_block_index]["text"] = self.current_text
        events.append(
            self._event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.current_block_index,
                    "delta": {"type": "text_delta", "text": delta},
                },
            )
        )
        return events

    def ingest_tool_call(self, tool_call: Dict[str, Any]) -> List[str]:
        events = self._close_current_block()
        function_obj = tool_call.get("function") or {}
        arguments_text = function_obj.get("arguments") or "{}"
        try:
            parsed_input = orjson.loads(arguments_text)
        except orjson.JSONDecodeError:
            parsed_input = {}
        block = {
            "type": "tool_use",
            "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
            "name": function_obj.get("name"),
            "input": parsed_input if isinstance(parsed_input, dict) else {},
        }
        index = self._append_block(block)
        self.has_tool_calls = True
        self.tool_blocks_emitted += 1
        events.append(
            self._event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                },
            )
        )
        events.append(
            self._event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": arguments_text},
                },
            )
        )
        events.append(
            self._event(
                "content_block_stop",
                {
                    "type": "content_block_stop",
                    "index": index,
                },
            )
        )
        return events

    def finalize(self, usage: Optional[Dict[str, Any]], finish_reason: Optional[str]) -> List[str]:
        events = self._close_current_block()
        anthropic_usage = _to_anthropic_usage(usage)
        self.usage_input_tokens = anthropic_usage["input_tokens"]
        self.usage_output_tokens = anthropic_usage["output_tokens"]
        self.final_stop_reason, self.final_stop_sequence = _map_stop_reason(
            finish_reason,
            has_tool_calls=self.has_tool_calls,
            text_content=self.final_content_text,
            stop_sequences=self.stop_sequences,
        )
        logger.info(
            "Anthropic stream summary: model={}, include_thinking={}, reasoning_chars={}, thinking_blocks_emitted={}, tool_blocks_emitted={}, stop_reason={}",
            self.model,
            self.include_thinking,
            self.reasoning_chars,
            self.thinking_blocks_emitted,
            self.tool_blocks_emitted,
            self.final_stop_reason,
        )
        events.append(
            self._event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": self.final_stop_reason,
                        "stop_sequence": self.final_stop_sequence,
                    },
                    "usage": {
                        "input_tokens": self.usage_input_tokens,
                        "output_tokens": self.usage_output_tokens,
                    },
                },
            )
        )
        events.append(self._event("message_stop", {"type": "message_stop"}))
        return events


async def _anthropic_stream_from_chat(
    chat_stream: AsyncIterable[str],
    *,
    model: str,
    stop_sequences: Optional[List[str]],
    include_thinking: bool,
) -> AsyncGenerator[str, None]:
    adapter = AnthropicStreamAdapter(
        model=model,
        stop_sequences=stop_sequences,
        include_thinking=include_thinking,
    )
    final_usage: Optional[Dict[str, Any]] = None
    final_finish_reason: Optional[str] = None

    try:
        async for chunk in chat_stream:
            line = proc_base._normalize_line(chunk)
            if not line:
                continue
            try:
                data = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue

            if data.get("object") != "chat.completion.chunk":
                continue

            for event in adapter.ensure_message_started():
                yield event

            choice = (data.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if isinstance(delta.get("_grok2api_reasoning"), str):
                for event in adapter.ingest_reasoning(delta["_grok2api_reasoning"]):
                    yield event
            if isinstance(delta.get("content"), str) and delta["content"]:
                for event in adapter.ingest_text(delta["content"]):
                    yield event
            for tool_call in delta.get("tool_calls") or []:
                if isinstance(tool_call, dict):
                    for event in adapter.ingest_tool_call(tool_call):
                        yield event
            if data.get("usage"):
                final_usage = data["usage"]
            if choice.get("finish_reason") is not None:
                final_finish_reason = choice.get("finish_reason")

        for event in adapter.finalize(final_usage, final_finish_reason):
            yield event
    except Exception as exc:
        payload = _anthropic_error_payload(str(exc) or "stream_error", error_type="api_error")
        yield f"event: error\ndata: {orjson.dumps(payload).decode()}\n\n"


@router.post("/messages")
async def create_message(request: Request, payload: AnthropicMessagesRequest):
    try:
        await _authenticate_anthropic_request(request)
        if not ModelService.valid(payload.model):
            return _anthropic_json_error(
                f"The model `{payload.model}` does not exist or you do not have access to it.",
                status_code=400,
            )
        if payload.max_tokens <= 0:
            return _anthropic_json_error("max_tokens must be greater than 0")
        if not payload.messages:
            return _anthropic_json_error("messages is required")

        tools = _normalize_anthropic_tools(payload.tools)
        tool_choice, parallel_tool_calls = _normalize_anthropic_tool_choice(
            payload.tool_choice,
            tools_present=bool(tools),
        )
        explicit_thinking = _thinking_enabled(payload.thinking)
        include_thinking = explicit_thinking
        thinking_source = "request" if explicit_thinking else "disabled"
        logger.info(
            "Anthropic messages request: model={}, stream={}, thinking_enabled={}, thinking_source={}, tools_count={}, tool_choice={}",
            payload.model,
            bool(payload.stream),
            include_thinking,
            thinking_source,
            len(tools),
            str(payload.tool_choice),
        )

        internal_messages = _normalize_system_messages(payload.system)
        internal_messages.extend(_normalize_anthropic_messages(payload.messages))
        if not internal_messages:
            return _anthropic_json_error("messages cannot be empty")

        if ModelService.is_console(payload.model):
            raw_messages = [msg.model_dump() for msg in payload.messages]
            thinking_cfg: Optional[Dict[str, Any]] = None
            if payload.thinking is not None:
                if isinstance(payload.thinking, dict):
                    thinking_cfg = payload.thinking
                elif hasattr(payload.thinking, "model_dump"):
                    thinking_cfg = payload.thinking.model_dump()
            result = await ConsoleChannelService.messages(
                model=payload.model,
                messages=raw_messages,
                system=payload.system,
                stream=bool(payload.stream),
                max_tokens=payload.max_tokens,
                temperature=payload.temperature,
                top_p=payload.top_p,
                tools=tools or None,
                tool_choice=tool_choice,
                thinking=thinking_cfg,
            )
            if payload.stream:
                return StreamingResponse(
                    result,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )
            return JSONResponse(content=result)

        result = await ChatService.completions(
            model=payload.model,
            messages=internal_messages,
            stream=bool(payload.stream),
            temperature=payload.temperature if payload.temperature is not None else 0.8,
            top_p=payload.top_p if payload.top_p is not None else 0.95,
            tools=tools or None,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            include_internal_reasoning=include_thinking,
        )

        if payload.stream:
            return StreamingResponse(
                _anthropic_stream_from_chat(
                    result,
                    model=payload.model,
                    stop_sequences=payload.stop_sequences,
                    include_thinking=include_thinking,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        if not isinstance(result, dict):
            return _anthropic_json_error(
                "Unexpected stream response for non-stream request",
                status_code=500,
                error_type=ErrorType.SERVER.value,
            )

        choice = (result.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        response = _build_anthropic_message_response(
            model=payload.model,
            content=message.get("content"),
            tool_calls=message.get("tool_calls"),
            reasoning_text=message.get("_grok2api_reasoning_text"),
            finish_reason=choice.get("finish_reason"),
            usage=result.get("usage"),
            stop_sequences=payload.stop_sequences,
            include_thinking=include_thinking,
        )
        return JSONResponse(content=response)

    except AuthenticationException as exc:
        return _anthropic_json_error(
            exc.message,
            status_code=401,
            error_type="authentication_error",
        )
    except ValidationException as exc:
        return _anthropic_json_error(exc.message)
    except AppException as exc:
        error_type = "authentication_error" if exc.error_type == ErrorType.AUTHENTICATION.value else "api_error"
        return _anthropic_json_error(
            exc.message,
            status_code=exc.status_code,
            error_type=error_type,
        )
    except Exception as exc:
        return _anthropic_json_error(
            str(exc) or "internal server error",
            status_code=500,
            error_type="api_error",
        )


__all__ = ["router"]
