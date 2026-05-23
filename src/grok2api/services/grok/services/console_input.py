"""Build console.x.ai Responses API input[] from Chat/Anthropic/Responses requests."""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import orjson

from grok2api.services.grok.services.console_capabilities import (
    ConsoleModelCapabilities,
    should_include_encrypted,
)
from grok2api.services.grok.utils.tool_call import normalize_function_tool
from grok2api.services.reverse.console_constants import CONSOLE_ALLOWED_INCLUDE

_ENCRYPTED_RE = re.compile(r"^[A-Za-z0-9+/=_-]{80,}$")

# Server-only fields safe to drop when replaying output[] back to console.x.ai.
_REPLAY_STRIP_KEYS = frozenset({"status"})


def _new_reasoning_id() -> str:
    return f"rs_{uuid.uuid4().hex[:24]}"


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def is_encrypted_reasoning(text: str) -> bool:
    if not text:
        return False
    stripped = text.replace("\n", "").replace(" ", "").replace("\r", "")
    if len(stripped) < 80:
        return False
    sample = stripped[:512]
    return bool(_ENCRYPTED_RE.match(sample))


def _text_content(text: str) -> List[Dict[str, Any]]:
    return [{"type": "input_text", "text": text}]


def _output_text_content(text: str) -> List[Dict[str, Any]]:
    return [{"type": "output_text", "text": text}]


def _normalize_image_block(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    block_type = str(block.get("type") or "").strip().lower()
    if block_type == "input_image":
        image_url = block.get("image_url")
        if isinstance(image_url, str) and image_url.strip():
            return {
                "type": "input_image",
                "image_url": image_url.strip(),
                "detail": str(block.get("detail") or "auto"),
            }
        if isinstance(image_url, dict) and image_url.get("url"):
            return {
                "type": "input_image",
                "image_url": str(image_url.get("url")),
                "detail": str(image_url.get("detail") or block.get("detail") or "auto"),
            }
        return None
    if block_type == "image_url":
        image_url = block.get("image_url")
        url = ""
        detail = "auto"
        if isinstance(image_url, dict):
            url = str(image_url.get("url") or "").strip()
            detail = str(image_url.get("detail") or "auto")
        elif isinstance(image_url, str):
            url = image_url.strip()
        if not url:
            return None
        return {"type": "input_image", "image_url": url, "detail": detail}
    if block_type.endswith("_url"):
        payload = block.get(block_type) or {}
        if isinstance(payload, dict) and payload.get("url"):
            return {
                "type": "input_image",
                "image_url": str(payload.get("url")),
                "detail": str(payload.get("detail") or "auto"),
            }
    return block


def _normalize_user_content_blocks(content: List[Any]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            if block:
                blocks.append({"type": "input_text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type == "text":
            blocks.append({"type": "input_text", "text": block.get("text") or ""})
            continue
        if block_type in {"input_text", "output_text"}:
            blocks.append(block)
            continue
        normalized = _normalize_image_block(block)
        if normalized is not None:
            blocks.append(normalized)
    return blocks


def _normalize_function_call_item(item: Dict[str, Any]) -> Dict[str, Any]:
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        arguments = orjson.dumps(arguments or {}).decode()
    call_id = item.get("call_id") or item.get("id") or _new_call_id()
    item_id = str(item.get("id") or "").strip()
    if item_id.startswith("call_") and not item.get("call_id"):
        call_id = item_id
    normalized: Dict[str, Any] = {
        "type": "function_call",
        "call_id": call_id,
        "name": item.get("name"),
        "arguments": arguments,
    }
    if item_id.startswith("fc_"):
        normalized["id"] = item_id
    return normalized


def _passthrough_replay_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Replay upstream output items without modifying encrypted blobs or server ids."""
    cloned = copy.deepcopy(item)
    for key in _REPLAY_STRIP_KEYS:
        cloned.pop(key, None)
    return cloned


def _normalize_reasoning_item(item: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {
        "type": "reasoning",
        "id": item.get("id") or _new_reasoning_id(),
    }
    status = item.get("status")
    if status:
        normalized["status"] = status
    summary = item.get("summary")
    if isinstance(summary, list) and summary:
        normalized["summary"] = summary
    content = item.get("content")
    if isinstance(content, list) and content:
        normalized["content"] = content
    enc = item.get("encrypted_content")
    if enc:
        normalized["encrypted_content"] = enc
    return normalized


def _normalize_replay_message_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    role = str(item.get("role") or "").strip().lower()
    content = item.get("content")
    if role == "user":
        if isinstance(content, str):
            return {"role": "user", "content": _text_content(content)}
        if isinstance(content, list):
            blocks = _normalize_user_content_blocks(content)
            if blocks:
                return {"role": "user", "content": blocks}
        return None
    if role == "assistant":
        if isinstance(content, str):
            return {
                "type": "message",
                "role": "assistant",
                "content": _output_text_content(content),
            }
        if isinstance(content, list):
            parts: List[Dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "").strip().lower()
                if part_type in {"output_text", "text"}:
                    text = part.get("text") or part.get("content") or ""
                    if text:
                        parts.append({"type": "output_text", "text": text})
                elif part_type == "refusal":
                    parts.append(part)
            if parts:
                msg: Dict[str, Any] = {
                    "type": "message",
                    "role": "assistant",
                    "content": parts,
                }
                if item.get("id"):
                    msg["id"] = item.get("id")
                if item.get("status"):
                    msg["status"] = item.get("status")
                phase = item.get("phase")
                if phase is not None:
                    msg["phase"] = str(phase)
                return msg
    return None


class ConsoleInputBuilder:
    """Convert heterogeneous client history into Responses input[]."""

    @staticmethod
    def from_responses_input(
        input_value: Any,
        *,
        instructions: Optional[str] = None,
    ) -> Tuple[Optional[str], List[Dict[str, Any]], bool]:
        instructions_parts: List[str] = []
        if instructions:
            instructions_parts.append(instructions)
        items: List[Dict[str, Any]] = []
        history_encrypted = False

        if input_value is None:
            return ("\n\n".join(instructions_parts) or None), items, history_encrypted

        raw_items = input_value if isinstance(input_value, list) else [input_value]
        for item in raw_items:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    items.append({"role": "user", "content": _text_content(item)})
                continue

            item_type = item.get("type")
            role = item.get("role")

            if role in {"system", "developer"}:
                content = item.get("content")
                if isinstance(content, str) and content.strip():
                    instructions_parts.append(content.strip())
                continue

            if (
                item_type in {"reasoning", "compaction"}
                or (item_type is None and item.get("encrypted_content"))
            ):
                if item.get("encrypted_content"):
                    history_encrypted = True
                    items.append(_passthrough_replay_item(item))
                else:
                    items.append(_normalize_reasoning_item(item))
                continue

            if item_type == "function_call":
                if item.get("call_id"):
                    items.append(_passthrough_replay_item(item))
                else:
                    items.append(_normalize_function_call_item(item))
                continue

            if item_type in {"function_call_output", "tool_call_output", "tool_output"}:
                if item.get("call_id") or item.get("tool_call_id"):
                    items.append(_passthrough_replay_item(item))
                else:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": item.get("id"),
                            "output": item.get("output") or item.get("content") or "",
                        }
                    )
                continue

            if item_type == "message" or role in {"user", "assistant"}:
                if item_type == "message" and item.get("id"):
                    items.append(_passthrough_replay_item(item))
                    continue
                replay_item = _normalize_replay_message_item(item)
                if replay_item:
                    items.append(replay_item)
                continue

            if role == "user":
                content = item.get("content")
                if isinstance(content, str):
                    items.append({"role": "user", "content": _text_content(content)})
                elif isinstance(content, list):
                    blocks = _normalize_user_content_blocks(content)
                    if blocks:
                        items.append({"role": "user", "content": blocks})

        merged_instructions = "\n\n".join(instructions_parts) if instructions_parts else None
        return merged_instructions, items, history_encrypted

    @staticmethod
    def from_chat_messages(
        messages: List[Dict[str, Any]],
    ) -> Tuple[Optional[str], List[Dict[str, Any]], bool]:
        instructions_parts: List[str] = []
        items: List[Dict[str, Any]] = []
        history_encrypted = False

        for message in messages:
            role = message.get("role")
            if role in {"system", "developer"}:
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    instructions_parts.append(content.strip())
                continue

            if role == "user":
                content = message.get("content")
                if isinstance(content, str):
                    items.append({"role": "user", "content": _text_content(content)})
                elif isinstance(content, list):
                    blocks = _normalize_user_content_blocks(content)
                    items.append({"role": "user", "content": blocks or _text_content("")})
                continue

            if role == "assistant":
                reasoning = message.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning.strip():
                    if is_encrypted_reasoning(reasoning):
                        history_encrypted = True
                        reasoning_id = message.get("reasoning_id") or message.get(
                            "reasoning_item_id"
                        )
                        replay_item: Dict[str, Any] = {
                            "type": "reasoning",
                            "encrypted_content": reasoning,
                        }
                        if isinstance(reasoning_id, str) and reasoning_id.strip():
                            replay_item["id"] = reasoning_id.strip()
                        items.append(replay_item)
                    # plaintext summary stays in visible content path for grok-4.3

                tool_calls = message.get("tool_calls") or []
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    fn = tool_call.get("function") or {}
                    items.append(
                        _normalize_function_call_item(
                            {
                                "call_id": tool_call.get("id") or _new_call_id(),
                                "name": fn.get("name"),
                                "arguments": fn.get("arguments") or "{}",
                            }
                        )
                    )

                content = message.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text") or "")
                    text = "\n".join(p for p in parts if p)
                if isinstance(reasoning, str) and reasoning.strip() and not is_encrypted_reasoning(reasoning):
                    prefix = reasoning.strip()
                    text = f"{prefix}\n\n{text}" if text else prefix
                if text or not tool_calls:
                    items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": _output_text_content(text or ""),
                        }
                    )
                continue

            if role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.get("tool_call_id") or _new_call_id(),
                        "output": message.get("content") or "",
                    }
                )

        return ("\n\n".join(instructions_parts) or None), items, history_encrypted

    @staticmethod
    def from_anthropic_raw_messages(
        messages: List[Dict[str, Any]],
    ) -> Tuple[Optional[str], List[Dict[str, Any]], bool]:
        chat_messages: List[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                content = message.get("content")
                assistant: Dict[str, Any] = {"role": "assistant"}
                text_parts: List[str] = []
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "thinking":
                            thinking = block.get("thinking") or ""
                            if thinking:
                                assistant["reasoning_content"] = thinking
                        elif block_type == "text":
                            text = block.get("text") or ""
                            if text:
                                text_parts.append(text)
                        elif block_type == "tool_use":
                            if "tool_calls" not in assistant:
                                assistant["tool_calls"] = []
                            assistant["tool_calls"].append(
                                {
                                    "id": block.get("id") or _new_call_id(),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name"),
                                        "arguments": orjson.dumps(block.get("input") or {}).decode(),
                                    },
                                }
                            )
                if text_parts:
                    assistant["content"] = "\n\n".join(text_parts)
                if len(assistant) > 1:
                    chat_messages.append(assistant)
                continue
            if role == "user":
                content = message.get("content")
                if isinstance(content, str):
                    chat_messages.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    tool_results: List[Dict[str, Any]] = []
                    user_text: List[str] = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            tool_results.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": block.get("tool_use_id") or _new_call_id(),
                                    "content": block.get("content") or "",
                                }
                            )
                        elif block.get("type") == "text":
                            user_text.append(block.get("text") or "")
                    if user_text:
                        chat_messages.append({"role": "user", "content": "\n".join(user_text)})
                    chat_messages.extend(tool_results)
        return ConsoleInputBuilder.from_chat_messages(chat_messages)

    @staticmethod
    def normalize_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        if not tools:
            return []
        normalized: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_type = tool.get("type")
            if tool_type in {"web_search", "web_search_preview", "x_search"}:
                normalized.append({"type": "web_search" if tool_type == "web_search_preview" else tool_type})
                continue
            if tool_type == "function":
                flat = normalize_function_tool(tool)
                if flat:
                    normalized.append(
                        {
                            "type": "function",
                            "name": flat.get("name"),
                            "description": flat.get("description") or "",
                            "parameters": flat.get("parameters") or {"type": "object", "properties": {}},
                        }
                    )
        return normalized

    @staticmethod
    def build_payload(
        *,
        console_model: str,
        caps: ConsoleModelCapabilities,
        input_items: List[Dict[str, Any]],
        instructions: Optional[str] = None,
        stream: bool = True,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        reasoning_config: Optional[Dict[str, Any]] = None,
        text_format: Optional[Dict[str, Any]] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        parallel_tool_calls: Optional[bool] = True,
        request_include: Optional[List[str]] = None,
        history_has_encrypted: bool = False,
        thinking_enabled: bool = False,
        store: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": console_model,
            "input": input_items,
            "stream": True if stream is None else bool(stream),
            "store": store,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = "auto" if tool_choice == "required" else tool_choice
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if text_format:
            payload["text"] = {"format": text_format}

        reasoning: Dict[str, Any] = {}
        if isinstance(reasoning_config, dict):
            for key, value in reasoning_config.items():
                if value is not None:
                    reasoning[key] = value
        effort = reasoning.get("effort") or reasoning_effort
        if caps.supports_reasoning_effort and effort is not None and "effort" not in reasoning:
            reasoning["effort"] = effort
        if caps.supports_reasoning_summary and "summary" not in reasoning:
            effort_text = str(reasoning.get("effort") or reasoning_effort or "").lower()
            if effort_text not in ("none", ""):
                reasoning["summary"] = "auto"
        if reasoning:
            payload["reasoning"] = reasoning

        include_values: List[str] = []
        if request_include:
            include_values.extend(
                item for item in request_include if item in CONSOLE_ALLOWED_INCLUDE
            )
        include_encrypted = should_include_encrypted(
            caps,
            reasoning_effort=str(reasoning.get("effort") or reasoning_effort or ""),
            thinking_enabled=thinking_enabled,
            request_include=request_include,
            history_has_encrypted=history_has_encrypted,
        )
        if include_encrypted and caps.supports_encrypted_reasoning:
            if "reasoning.encrypted_content" not in include_values:
                include_values.append("reasoning.encrypted_content")
        if include_values:
            payload["include"] = include_values

        return payload


__all__ = ["ConsoleInputBuilder", "is_encrypted_reasoning"]
