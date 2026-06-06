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
    """Forward upstream Responses SSE/JSON and optionally parse prompt tool calls."""

    def __init__(
        self,
        model_id: str,
        *,
        prompt_tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = None,
        parallel_tool_calls: Optional[bool] = None,
    ) -> None:
        self.model_id = model_id
        self.completed_response: Optional[Dict[str, Any]] = None
        self._prompt_tools = prompt_tools
        self._tool_choice = tool_choice
        self._parallel_tool_calls = parallel_tool_calls
        self._prompt_parser = (
            ToolCallStreamParser(prompt_tools)
            if prompt_tools and tool_choice != "none"
            else None
        )
        self._pending_text_lines: List[str] = []
        self._prompt_tool_records: List[Dict[str, Any]] = []
        self._prompt_tool_mode = False
        self._prompt_text_mode = False
        self._response_id: Optional[str] = None
        self._pending_message_item_id: Optional[str] = None
        self._pending_message_output_index: Optional[int] = None
        self._used_output_indexes: set[int] = set()
        self._max_output_index = -1

    def _apply_model_alias(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(data)
        response = data.get("response")
        if isinstance(response, dict):
            data["response"] = dict(response)
            data["response"]["model"] = self.model_id
        elif data.get("object") == "response":
            data["model"] = self.model_id
        return data

    @staticmethod
    def _line(line: str) -> str:
        return line if line.endswith("\n") else line + "\n"

    @staticmethod
    def _data_line(data: Dict[str, Any]) -> str:
        return f"data: {orjson.dumps(data).decode()}\n\n"

    def _finalize(self, data: Dict[str, Any]) -> str:
        if data.get("type") == "response.completed":
            self.completed_response = data
        return self._data_line(data)

    def _remember_response_id(self, data: Dict[str, Any]) -> None:
        response_id = data.get("response_id")
        if isinstance(response_id, str) and response_id:
            self._response_id = response_id
        response = data.get("response")
        if isinstance(response, dict):
            response_id = response.get("id")
            if isinstance(response_id, str) and response_id:
                self._response_id = response_id

    def _ensure_response_id(self) -> str:
        if not self._response_id:
            self._response_id = f"resp_{uuid.uuid4().hex[:24]}"
        return self._response_id

    def _remember_output_index(self, data: Dict[str, Any]) -> None:
        output_index = data.get("output_index")
        if isinstance(output_index, int):
            self._max_output_index = max(self._max_output_index, output_index)
            self._used_output_indexes.add(output_index)

    def _preferred_tool_output_index(self) -> Optional[int]:
        output_index = self._pending_message_output_index
        if output_index is None:
            return None
        self._used_output_indexes.discard(output_index)
        return output_index

    def _allocate_tool_output_index(self, preferred: Optional[int] = None) -> int:
        if preferred is not None and preferred not in self._used_output_indexes:
            self._used_output_indexes.add(preferred)
            self._max_output_index = max(self._max_output_index, preferred)
            return preferred
        output_index = self._max_output_index + 1
        while output_index in self._used_output_indexes:
            output_index += 1
        self._used_output_indexes.add(output_index)
        self._max_output_index = max(self._max_output_index, output_index)
        return output_index

    @staticmethod
    def _function_call_item(record: Dict[str, Any], *, status: str) -> Dict[str, Any]:
        return {
            "id": record["item_id"],
            "type": "function_call",
            "status": status,
            "call_id": record["call_id"],
            "name": record["name"],
            "arguments": record["arguments"],
        }

    def _make_tool_record(
        self,
        tool_call: Dict[str, Any],
        *,
        preferred_output_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        fn = tool_call.get("function") or {}
        arguments = fn.get("arguments")
        if not isinstance(arguments, str):
            arguments = orjson.dumps(arguments or {}).decode()
        call_id = tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}"
        name = fn.get("name") or ""
        return {
            "item_id": f"fc_{uuid.uuid4().hex[:24]}",
            "output_index": self._allocate_tool_output_index(preferred_output_index),
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        }

    def _emit_tool_call(self, tool_call: Dict[str, Any]) -> str:
        preferred = self._preferred_tool_output_index() if not self._prompt_tool_records else None
        record = self._make_tool_record(tool_call, preferred_output_index=preferred)
        self._prompt_tool_records.append(record)
        self._prompt_tool_mode = True
        response_id = self._ensure_response_id()
        chunks = [
            self._data_line(
                {
                    "type": "response.output_item.added",
                    "response_id": response_id,
                    "output_index": record["output_index"],
                    "item": self._function_call_item(record, status="in_progress"),
                }
            )
        ]
        if record["arguments"]:
            chunks.append(
                self._data_line(
                    {
                        "type": "response.function_call_arguments.delta",
                        "response_id": response_id,
                        "item_id": record["item_id"],
                        "output_index": record["output_index"],
                        "call_id": record["call_id"],
                        "delta": record["arguments"],
                    }
                )
            )
        chunks.extend(
            [
                self._data_line(
                    {
                        "type": "response.function_call_arguments.done",
                        "response_id": response_id,
                        "item_id": record["item_id"],
                        "output_index": record["output_index"],
                        "call_id": record["call_id"],
                        "name": record["name"],
                        "arguments": record["arguments"],
                    }
                ),
                self._data_line(
                    {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": record["output_index"],
                        "item": self._function_call_item(record, status="completed"),
                    }
                ),
            ]
        )
        return "".join(chunks)

    def _flush_pending_text(self) -> str:
        out = "".join(self._pending_text_lines)
        self._pending_text_lines = []
        return out

    def _discard_pending_text(self) -> None:
        self._pending_text_lines = []

    def _handle_prompt_parser_events(
        self,
        events: List[tuple[str, Any]],
        *,
        final: bool = False,
    ) -> Optional[str]:
        out: List[str] = []
        for kind, payload in events:
            if kind == "tool" and isinstance(payload, dict):
                self._discard_pending_text()
                out.append(self._emit_tool_call(payload))
                continue
            if kind != "text" or not isinstance(payload, str):
                continue
            if self._prompt_tool_mode:
                if payload.strip():
                    continue
                continue
            if payload.strip() or final:
                self._prompt_text_mode = True
                out.append(self._flush_pending_text())
        if out:
            return "".join(out)
        return None

    def _flush_prompt_parser(self) -> Optional[str]:
        if not self._prompt_parser:
            return None
        return self._handle_prompt_parser_events(self._prompt_parser.flush(), final=True)

    def _queue_text_event(self, data: Dict[str, Any]) -> None:
        self._pending_text_lines.append(self._data_line(data))

    def _is_pending_message_event(self, data: Dict[str, Any]) -> bool:
        item_id = data.get("item_id")
        if isinstance(item_id, str) and item_id and item_id == self._pending_message_item_id:
            return True
        output_index = data.get("output_index")
        return (
            isinstance(output_index, int)
            and self._pending_message_output_index is not None
            and output_index == self._pending_message_output_index
        )

    @staticmethod
    def _message_text(item: Dict[str, Any]) -> str:
        parts: List[str] = []
        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"output_text", "text"}:
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _message_with_text(item: Dict[str, Any], text: str) -> Dict[str, Any]:
        cloned = dict(item)
        cloned["content"] = [{"type": "output_text", "text": text, "annotations": []}]
        return cloned

    @staticmethod
    def _merge_response_tools(
        existing: Any,
        prompt_tools: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for tool in (existing if isinstance(existing, list) else []) + list(prompt_tools or []):
            if not isinstance(tool, dict):
                continue
            key = (str(tool.get("type") or ""), str(tool.get("name") or ""))
            if key in seen:
                continue
            seen.add(key)
            merged.append(tool)
        return merged

    def _records_for_tool_calls(
        self,
        tool_calls: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for idx, tool_call in enumerate(tool_calls):
            if idx < len(self._prompt_tool_records):
                records.append(self._prompt_tool_records[idx])
                continue
            record = self._make_tool_record(tool_call)
            self._prompt_tool_records.append(record)
            records.append(record)
        return records

    def _transform_completed_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if data.get("type") != "response.completed" or not self._prompt_parser:
            return data

        response = data.get("response")
        if not isinstance(response, dict):
            return data

        response = dict(response)
        output = response.get("output")
        if not isinstance(output, list):
            output = []

        new_output: List[Dict[str, Any]] = []
        transformed = False
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                if isinstance(item, dict):
                    new_output.append(item)
                continue
            text = self._message_text(item)
            text_content, tool_calls = parse_tool_calls(text, self._prompt_tools)
            if not tool_calls:
                new_output.append(item)
                continue
            transformed = True
            self._prompt_tool_mode = True
            if text_content:
                new_output.append(self._message_with_text(item, text_content))
            for record in self._records_for_tool_calls(tool_calls):
                new_output.append(self._function_call_item(record, status="completed"))

        if transformed or self._prompt_tool_records:
            if self._prompt_tool_records and not transformed:
                new_output.extend(
                    self._function_call_item(record, status="completed")
                    for record in self._prompt_tool_records
                )
            response["output"] = new_output or [
                self._function_call_item(record, status="completed")
                for record in self._prompt_tool_records
            ]
            response["tools"] = self._merge_response_tools(
                response.get("tools"),
                self._prompt_tools,
            )
            if self._tool_choice is not None:
                response["tool_choice"] = self._tool_choice
            if self._parallel_tool_calls is not None:
                response["parallel_tool_calls"] = self._parallel_tool_calls

        updated = dict(data)
        updated["response"] = response
        return updated

    def _ingest_prompt_data(self, data: Dict[str, Any]) -> Optional[str]:
        event_type = data.get("type")
        self._remember_response_id(data)
        self._remember_output_index(data)

        if event_type == "response.output_item.added":
            item = data.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "message":
                self._pending_message_item_id = item.get("id")
                output_index = data.get("output_index")
                self._pending_message_output_index = output_index if isinstance(output_index, int) else None
                if self._prompt_tool_mode:
                    return None
                if self._prompt_text_mode:
                    return self._finalize(data)
                self._queue_text_event(data)
                return None
            return self._finalize(data)

        if event_type == "response.content_part.added" and self._is_pending_message_event(data):
            if self._prompt_tool_mode:
                return None
            if self._prompt_text_mode:
                return self._finalize(data)
            self._queue_text_event(data)
            return None

        if event_type == "response.output_text.delta":
            if self._prompt_tool_mode:
                delta = data.get("delta") or ""
                if isinstance(delta, str) and self._prompt_parser:
                    return self._handle_prompt_parser_events(
                        self._prompt_parser.feed(delta),
                    )
                return None
            if self._prompt_text_mode:
                return self._finalize(data)
            self._queue_text_event(data)
            delta = data.get("delta") or ""
            if isinstance(delta, str) and self._prompt_parser:
                return self._handle_prompt_parser_events(
                    self._prompt_parser.feed(delta),
                )
            return None

        if event_type in {
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
        } and self._is_pending_message_event(data):
            if self._prompt_tool_mode:
                return self._flush_prompt_parser()
            if self._prompt_text_mode:
                return self._finalize(data)
            self._queue_text_event(data)
            flushed = self._flush_prompt_parser()
            if flushed:
                return flushed
            self._prompt_text_mode = True
            return self._flush_pending_text()

        if event_type == "response.completed":
            flushed = None
            if not self._prompt_text_mode:
                flushed = self._flush_prompt_parser()
            if not flushed and not self._prompt_tool_mode and self._pending_text_lines:
                flushed = self._flush_pending_text()
            transformed = self._transform_completed_response(data)
            finalized = self._finalize(transformed)
            return f"{flushed or ''}{finalized}"

        return self._finalize(data)

    def _ingest_data(self, data: Dict[str, Any]) -> Optional[str]:
        data = self._apply_model_alias(data)
        if not self._prompt_parser:
            return self._finalize(data)
        return self._ingest_prompt_data(data)

    def ingest_raw_line(self, line: str | bytes) -> Optional[str]:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                data = orjson.loads(stripped)
            except orjson.JSONDecodeError:
                return self._line(line)
            if isinstance(data, dict) and data.get("object") == "response":
                data = {"type": "response.completed", "response": data}
            if isinstance(data, dict):
                return self._ingest_data(data)
        if not stripped.startswith("data:"):
            return self._line(line)
        payload_text = stripped[5:].strip()
        if not payload_text or payload_text == "[DONE]":
            return self._line(line)
        try:
            data = orjson.loads(payload_text)
        except orjson.JSONDecodeError:
            return self._line(line)
        if isinstance(data, dict):
            return self._ingest_data(data)
        return self._line(line)


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
