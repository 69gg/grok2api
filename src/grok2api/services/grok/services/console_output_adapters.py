"""Adapt console upstream events to Chat / Responses / Anthropic output formats."""

from __future__ import annotations

import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

import orjson

from grok2api.services.grok.services.console_stream_parser import (
    ConsoleEvent,
    ConsoleEventType,
)
from grok2api.services.grok.utils.response import make_response_id
from grok2api.services.grok.utils.tool_call import ToolCallStreamParser, parse_tool_calls
from grok2api.services.grok.utils.usage import from_upstream_responses_usage, to_responses_usage


def _sse_chat_chunk(
    response_id: str,
    model: str,
    *,
    delta: Optional[Dict[str, Any]] = None,
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> str:
    choice: Dict[str, Any] = {"index": 0, "delta": delta or {}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    payload: Dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {orjson.dumps(payload).decode()}\n\n"


class ConsoleChatStreamAdapter:
    def __init__(
        self,
        model: str,
        *,
        prompt_tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = None,
        emit_plaintext_reasoning: bool = True,
    ) -> None:
        self.model = model
        self.response_id = make_response_id()
        self.started = False
        self.emit_plaintext_reasoning = emit_plaintext_reasoning
        self.reasoning_parts: List[str] = []
        self._encrypted_reasoning: Optional[str] = None
        self.content_parts: List[str] = []
        self.tool_calls: Dict[int, Dict[str, Any]] = {}
        self._tool_index = 0
        self.finish_reason: Optional[str] = None
        self.usage: Optional[Dict[str, Any]] = None
        self._prompt_tools = prompt_tools
        self._tool_stream_enabled = bool(prompt_tools) and tool_choice != "none"
        self._prompt_parser = (
            ToolCallStreamParser(prompt_tools) if self._tool_stream_enabled else None
        )
        self._prompt_tool_calls_seen = False
        self._prompt_content_emitted = False

    def _handle_prompt_tool_stream(self, chunk: str) -> List[tuple[str, Any]]:
        if not chunk:
            return []
        if not self._tool_stream_enabled or not self._prompt_parser:
            return [("text", chunk)]

        events: List[tuple[str, Any]] = []
        allow_calls = not self._prompt_content_emitted and not self._prompt_tool_calls_seen
        for kind, payload in self._prompt_parser.feed(chunk, allow_calls=allow_calls):
            if kind == "tool":
                events.append(("tool", payload))
                self._prompt_tool_calls_seen = True
            else:
                if self._prompt_tool_calls_seen and isinstance(payload, str) and payload.strip():
                    continue
                if isinstance(payload, str) and payload.strip():
                    self._prompt_content_emitted = True
                events.append((kind, payload))
        return events

    def _emit_prompt_tool_call(self, tool_call: Dict[str, Any]) -> List[str]:
        index = self._tool_index
        self._tool_index += 1
        indexed = {
            "index": index,
            "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": dict(tool_call.get("function") or {}),
        }
        self.tool_calls[index] = indexed
        self.finish_reason = "tool_calls"
        return [
            _sse_chat_chunk(
                self.response_id,
                self.model,
                delta={"tool_calls": [indexed]},
            )
        ]

    def _ensure_role(self) -> List[str]:
        if self.started:
            return []
        self.started = True
        return [
            _sse_chat_chunk(
                self.response_id,
                self.model,
                delta={"role": "assistant", "content": ""},
            )
        ]

    def _remember_encrypted_reasoning(self, enc: str) -> bool:
        if not isinstance(enc, str) or not enc:
            return False
        if self._encrypted_reasoning is None:
            self._encrypted_reasoning = enc
            return True
        return False

    def ingest(self, event: ConsoleEvent) -> List[str]:
        out: List[str] = []
        out.extend(self._ensure_role())

        if event.type == ConsoleEventType.REASONING_SUMMARY_DELTA:
            if (
                self.emit_plaintext_reasoning
                and self._encrypted_reasoning is None
            ):
                delta = event.data.get("delta") or ""
                self.reasoning_parts.append(delta)
                out.append(
                    _sse_chat_chunk(
                        self.response_id,
                        self.model,
                        delta={"reasoning_content": delta},
                    )
                )
        elif event.type == ConsoleEventType.REASONING_DONE:
            enc = event.data.get("encrypted_content") or ""
            if enc and self._remember_encrypted_reasoning(enc):
                out.append(
                    _sse_chat_chunk(
                        self.response_id,
                        self.model,
                        delta={"reasoning_content": enc},
                    )
                )
        elif event.type == ConsoleEventType.TEXT_DELTA:
            delta = event.data.get("delta") or ""
            if self._prompt_parser:
                for kind, payload in self._handle_prompt_tool_stream(delta):
                    if kind == "tool":
                        out.extend(self._emit_prompt_tool_call(payload))
                    elif kind == "text" and payload:
                        self.content_parts.append(payload)
                        out.append(
                            _sse_chat_chunk(
                                self.response_id,
                                self.model,
                                delta={"content": payload},
                            )
                        )
            else:
                self.content_parts.append(delta)
                out.append(
                    _sse_chat_chunk(
                        self.response_id,
                        self.model,
                        delta={"content": delta},
                    )
                )
        elif event.type == ConsoleEventType.TOOL_CALL_START:
            index = int(event.data.get("index") or 0)
            self.tool_calls[index] = {
                "index": index,
                "id": event.data.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": event.data.get("name") or "", "arguments": ""},
            }
            out.append(
                _sse_chat_chunk(
                    self.response_id,
                    self.model,
                    delta={"tool_calls": [self.tool_calls[index]]},
                )
            )
        elif event.type == ConsoleEventType.TOOL_CALL_ARGS_DELTA:
            index = int(event.data.get("index") or 0)
            tool = self.tool_calls.setdefault(
                index,
                {
                    "index": index,
                    "id": f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            tool["function"]["arguments"] += event.data.get("delta") or ""
            out.append(
                _sse_chat_chunk(
                    self.response_id,
                    self.model,
                    delta={
                        "tool_calls": [
                            {
                                "index": index,
                                "function": {"arguments": event.data.get("delta") or ""},
                            }
                        ]
                    },
                )
            )
        elif event.type == ConsoleEventType.TOOL_CALL_DONE:
            index = int(event.data.get("index") or 0)
            if index in self.tool_calls:
                args = event.data.get("arguments")
                if args:
                    self.tool_calls[index]["function"]["arguments"] = args
        elif event.type == ConsoleEventType.COMPLETED:
            usage_raw = event.data.get("usage")
            if usage_raw:
                self.usage = from_upstream_responses_usage(usage_raw)
            self.finish_reason = event.data.get("finish_reason") or "stop"
            response = event.data.get("response") or {}
            for item in response.get("output") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "reasoning":
                    encrypted = item.get("encrypted_content")
                    if encrypted:
                        self._remember_encrypted_reasoning(encrypted)
                    elif self.emit_plaintext_reasoning:
                        summary = item.get("summary")
                        if isinstance(summary, list):
                            for part in summary:
                                if isinstance(part, dict) and part.get("text"):
                                    self.reasoning_parts.append(str(part["text"]))
                elif item.get("type") == "message":
                    for block in item.get("content") or []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "output_text":
                            text = block.get("text") or ""
                            if text:
                                self.content_parts.append(text)
                elif item.get("type") == "function_call":
                    index = self._tool_index
                    self._tool_index += 1
                    self.tool_calls[index] = {
                        "id": item.get("call_id") or item.get("id") or f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": item.get("name") or "",
                            "arguments": item.get("arguments") or "{}",
                        },
                    }
                    self.finish_reason = "tool_calls"

        return out

    def finalize(self) -> List[str]:
        out: List[str] = []
        if self._prompt_parser:
            for kind, payload in self._prompt_parser.flush():
                if kind == "tool":
                    out.extend(self._emit_prompt_tool_call(payload))
                elif kind == "text" and payload:
                    self.content_parts.append(payload)
                    out.append(
                        _sse_chat_chunk(
                            self.response_id,
                            self.model,
                            delta={"content": payload},
                        )
                    )
        if self.tool_calls and self.finish_reason != "tool_calls":
            self.finish_reason = "tool_calls"
        out.append(
            _sse_chat_chunk(
                self.response_id,
                self.model,
                finish_reason=self.finish_reason or "stop",
                usage=self.usage,
            )
        )
        out.append("data: [DONE]\n\n")
        return out

    def build_non_stream_response(self) -> Dict[str, Any]:
        if self._prompt_tools and not self.tool_calls:
            content = "".join(self.content_parts) or None
            if content:
                text, parsed = parse_tool_calls(content, self._prompt_tools)
                if parsed:
                    self.content_parts = [text] if text else []
                    for i, tool_call in enumerate(parsed):
                        self.tool_calls[i] = {
                            "index": i,
                            "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": dict(tool_call.get("function") or {}),
                        }
                    self.finish_reason = "tool_calls"
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self.content_parts) or None,
        }
        if self._encrypted_reasoning is not None:
            message["reasoning_content"] = self._encrypted_reasoning
        elif self.reasoning_parts:
            message["reasoning_content"] = "".join(self.reasoning_parts)
        tool_list = [self.tool_calls[i] for i in sorted(self.tool_calls)]
        if tool_list:
            message["tool_calls"] = tool_list
        return {
            "id": self.response_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": self.finish_reason or "stop",
                }
            ],
            "usage": self.usage or from_upstream_responses_usage({}),
        }


class ConsoleResponsesPassthroughAdapter:
    """Forward upstream Responses SSE/JSON; only rewrite the exposed model alias."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.completed_response: Optional[Dict[str, Any]] = None

    def _apply_model_alias(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(data)
        response = data.get("response")
        if isinstance(response, dict):
            data["response"] = dict(response)
            data["response"]["model"] = self.model_id
        elif data.get("object") == "response":
            data["model"] = self.model_id
        return data

    def _finalize(self, data: Dict[str, Any]) -> str:
        if data.get("type") == "response.completed":
            self.completed_response = data
        return f"data: {orjson.dumps(data).decode()}\n\n"

    def ingest_raw_line(self, line: str | bytes) -> Optional[str]:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                data = orjson.loads(stripped)
            except orjson.JSONDecodeError:
                return line if line.endswith("\n") else line + "\n"
            if isinstance(data, dict) and data.get("object") == "response":
                data = {"type": "response.completed", "response": data}
            if isinstance(data, dict):
                return self._finalize(self._apply_model_alias(data))
        if not stripped.startswith("data:"):
            return line if line.endswith("\n") else line + "\n"
        payload_text = stripped[5:].strip()
        if not payload_text or payload_text == "[DONE]":
            return line if line.endswith("\n") else line + "\n"
        try:
            data = orjson.loads(payload_text)
        except orjson.JSONDecodeError:
            return line if line.endswith("\n") else line + "\n"
        if isinstance(data, dict):
            return self._finalize(self._apply_model_alias(data))
        return line if line.endswith("\n") else line + "\n"


# Back-compat alias
ConsoleResponsesStreamAdapter = ConsoleResponsesPassthroughAdapter


def anthropic_usage_from_chat(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    normalized = usage or from_upstream_responses_usage({})
    return {
        "input_tokens": int(normalized.get("prompt_tokens") or 0),
        "output_tokens": int(normalized.get("completion_tokens") or 0),
    }


__all__ = [
    "ConsoleChatStreamAdapter",
    "ConsoleResponsesPassthroughAdapter",
    "ConsoleResponsesStreamAdapter",
    "anthropic_usage_from_chat",
    "to_responses_usage",
]
