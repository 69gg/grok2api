"""Adapt console upstream events to Chat / Responses / Anthropic output formats."""

from __future__ import annotations

import copy
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
    ) -> None:
        self.model = model
        self.response_id = make_response_id()
        self.started = False
        self.reasoning_parts: List[str] = []
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

    def ingest(self, event: ConsoleEvent) -> List[str]:
        out: List[str] = []
        out.extend(self._ensure_role())

        if event.type == ConsoleEventType.REASONING_SUMMARY_DELTA:
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
            if enc:
                self.reasoning_parts.append(enc)
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
                        self.reasoning_parts.append(encrypted)
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
        if self.reasoning_parts:
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


def _extract_native_reasoning_text(item: Dict[str, Any]) -> str:
    chunks: List[str] = []
    for field in ("summary", "content"):
        parts = item.get(field)
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                if isinstance(part, str) and part.strip():
                    chunks.append(part.strip())
                continue
            part_type = str(part.get("type") or "").strip().lower()
            text = str(part.get("text") or part.get("content") or "").strip()
            if not text:
                continue
            if part_type in {"", "summary_text", "reasoning_text", "text", "summary"}:
                chunks.append(text)
    return "\n".join(chunks)


def _reasoning_item_has_summary_text(item: Dict[str, Any]) -> bool:
    summary = item.get("summary")
    if not isinstance(summary, list):
        return False
    for part in summary:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "summary_text" and str(part.get("text") or "").strip():
            return True
    content = item.get("content")
    if not isinstance(content, list):
        return False
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "reasoning_text" and str(part.get("text") or "").strip():
            return True
    return False


def _inject_reasoning_summary_text(item: Dict[str, Any], text: str) -> Dict[str, Any]:
    if not text or _reasoning_item_has_summary_text(item):
        return item
    if item.get("encrypted_content"):
        return item
    patched = copy.deepcopy(item)
    patched["summary"] = [{"type": "summary_text", "text": text}]
    return patched


class ConsoleResponsesStreamAdapter:
    """Passthrough upstream SSE while ensuring reasoning summary reaches clients."""

    _REASONING_DELTA_EVENTS = frozenset(
        {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }
    )
    _REASONING_DONE_EVENTS = frozenset(
        {
            "response.reasoning_summary_text.done",
            "response.reasoning_text.done",
        }
    )

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.completed_response: Optional[Dict[str, Any]] = None
        self._reasoning_summary_by_output_index: Dict[int, List[str]] = {}

    def _accumulate_reasoning_text(
        self,
        *,
        output_index: Any = None,
        text: str,
    ) -> None:
        if not text or not isinstance(output_index, int):
            return
        self._reasoning_summary_by_output_index.setdefault(output_index, []).append(text)

    def _summary_text_for_output_index(self, output_index: Any = None) -> str:
        if isinstance(output_index, int) and output_index in self._reasoning_summary_by_output_index:
            return "".join(self._reasoning_summary_by_output_index[output_index])
        return ""

    def _patch_reasoning_items_in_output(
        self,
        output: List[Any],
    ) -> List[Any]:
        patched: List[Any] = []
        for index, item in enumerate(output):
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                patched.append(item)
                continue
            if item.get("encrypted_content"):
                patched.append(item)
                continue
            summary_text = self._summary_text_for_output_index(index)
            if not summary_text:
                summary_text = _extract_native_reasoning_text(item)
            if summary_text and not _reasoning_item_has_summary_text(item):
                patched.append(_inject_reasoning_summary_text(item, summary_text))
            else:
                patched.append(item)
        return patched

    def _patch_response_reasoning(self, response: Dict[str, Any]) -> Dict[str, Any]:
        output = response.get("output")
        if not isinstance(output, list):
            return response
        patched = dict(response)
        patched["output"] = self._patch_reasoning_items_in_output(output)
        return patched

    def _process_event(self, data: Dict[str, Any]) -> Dict[str, Any]:
        event_type = str(data.get("type") or "")
        if event_type in self._REASONING_DELTA_EVENTS:
            self._accumulate_reasoning_text(
                output_index=data.get("output_index"),
                text=str(data.get("delta") or ""),
            )
        elif event_type in self._REASONING_DONE_EVENTS:
            self._accumulate_reasoning_text(
                output_index=data.get("output_index"),
                text=str(data.get("text") or ""),
            )
        elif event_type == "response.output_item.done":
            item = data.get("item")
            if isinstance(item, dict) and item.get("type") == "reasoning":
                if not item.get("encrypted_content"):
                    summary_text = self._summary_text_for_output_index(data.get("output_index"))
                    if summary_text:
                        data = dict(data)
                        data["item"] = _inject_reasoning_summary_text(item, summary_text)
        elif event_type == "response.completed":
            response = data.get("response")
            if isinstance(response, dict):
                data = dict(data)
                patched = self._patch_response_reasoning(response)
                patched["model"] = self.model_id
                data["response"] = patched
            self.completed_response = data
            return data

        response = data.get("response")
        if isinstance(response, dict):
            data = dict(data)
            data["response"] = dict(response)
            data["response"]["model"] = self.model_id
            return data
        return data

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
                data = self._process_event(data)
                return f"data: {orjson.dumps(data).decode()}\n\n"
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
            data = self._process_event(data)
        return f"data: {orjson.dumps(data).decode()}\n\n"


def anthropic_usage_from_chat(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    normalized = usage or from_upstream_responses_usage({})
    return {
        "input_tokens": int(normalized.get("prompt_tokens") or 0),
        "output_tokens": int(normalized.get("completion_tokens") or 0),
    }


__all__ = [
    "ConsoleChatStreamAdapter",
    "ConsoleResponsesStreamAdapter",
    "anthropic_usage_from_chat",
    "to_responses_usage",
]
