"""Parse console.x.ai Responses SSE into internal events."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional


class ConsoleEventType(str, Enum):
    REASONING_START = "reasoning_start"
    REASONING_SUMMARY_DELTA = "reasoning_summary_delta"
    REASONING_DONE = "reasoning_done"
    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_ARGS_DELTA = "tool_call_args_delta"
    TOOL_CALL_DONE = "tool_call_done"
    RAW = "raw"
    COMPLETED = "completed"


@dataclass
class ConsoleEvent:
    type: ConsoleEventType
    data: Dict[str, Any] = field(default_factory=dict)


def _normalize_line(line: str | bytes) -> str:
    if isinstance(line, bytes):
        return line.decode("utf-8", errors="replace")
    return line


def _parse_sse_payload(line: str | bytes) -> Optional[Dict[str, Any]]:
    text = _normalize_line(line).strip()
    if not text:
        return None
    if text.startswith("data:"):
        payload = text[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("type") == "response.completed":
            return data
        if data.get("object") == "response" or "output" in data:
            return {"type": "response.completed", "response": data}
        return data
    return None


class ConsoleStreamParser:
    """Convert upstream SSE lines to unified console events."""

    def __init__(self) -> None:
        self._tool_index = 0
        self._tool_index_by_output: Dict[int, int] = {}
        self._has_tool_calls = False

    def ingest_line(self, line: str | bytes) -> List[ConsoleEvent]:
        events: List[ConsoleEvent] = []
        normalized = _normalize_line(line).strip()
        data = _parse_sse_payload(line)
        if data is None:
            if normalized.startswith("data:"):
                return events
            if normalized:
                events.append(ConsoleEvent(ConsoleEventType.RAW, {"line": normalized}))
            return events

        event_type = data.get("type") or ""
        if event_type == "response.reasoning_summary_text.delta":
            delta = data.get("delta") or ""
            if delta:
                events.append(
                    ConsoleEvent(
                        ConsoleEventType.REASONING_SUMMARY_DELTA,
                        {"delta": delta},
                    )
                )
            return events

        if event_type == "response.output_item.added":
            item = data.get("item") or {}
            item_type = item.get("type")
            output_index = data.get("output_index", 0)
            if item_type == "reasoning":
                events.append(
                    ConsoleEvent(
                        ConsoleEventType.REASONING_START,
                        {"id": item.get("id"), "output_index": output_index},
                    )
                )
            elif item_type == "function_call":
                index = self._tool_index
                self._tool_index += 1
                self._tool_index_by_output[output_index] = index
                self._has_tool_calls = True
                events.append(
                    ConsoleEvent(
                        ConsoleEventType.TOOL_CALL_START,
                        {
                            "index": index,
                            "output_index": output_index,
                            "id": item.get("call_id") or item.get("id"),
                            "name": item.get("name"),
                        },
                    )
                )
            return events

        if event_type == "response.function_call_arguments.delta":
            output_index = data.get("output_index", 0)
            index = self._tool_index_by_output.get(output_index, 0)
            delta = data.get("delta") or ""
            events.append(
                ConsoleEvent(
                    ConsoleEventType.TOOL_CALL_ARGS_DELTA,
                    {"index": index, "delta": delta},
                )
            )
            return events

        if event_type == "response.function_call_arguments.done":
            output_index = data.get("output_index", 0)
            index = self._tool_index_by_output.get(output_index, 0)
            events.append(
                ConsoleEvent(
                    ConsoleEventType.TOOL_CALL_DONE,
                    {
                        "index": index,
                        "arguments": data.get("arguments") or "",
                        "id": data.get("call_id") or data.get("item_id"),
                        "name": data.get("name"),
                    },
                )
            )
            return events

        if event_type == "response.output_item.done":
            item = data.get("item") or {}
            if item.get("type") == "reasoning":
                encrypted = item.get("encrypted_content")
                if encrypted:
                    events.append(
                        ConsoleEvent(
                            ConsoleEventType.REASONING_DONE,
                            {
                                "id": item.get("id"),
                                "encrypted_content": encrypted,
                                "summary": item.get("summary") or [],
                            },
                        )
                    )
            return events

        if event_type == "response.output_text.delta":
            delta = data.get("delta") or ""
            if delta:
                events.append(
                    ConsoleEvent(ConsoleEventType.TEXT_DELTA, {"delta": delta})
                )
            return events

        if event_type == "response.completed":
            response = data.get("response") or {}
            usage = response.get("usage")
            finish_reason = "tool_calls" if self._has_tool_calls else "stop"
            events.append(
                ConsoleEvent(
                    ConsoleEventType.COMPLETED,
                    {
                        "usage": usage,
                        "finish_reason": finish_reason,
                        "response": response,
                        "raw": data,
                    },
                )
            )
            return events

        events.append(ConsoleEvent(ConsoleEventType.RAW, {"raw": data}))
        return events

    async def parse(self, lines: AsyncIterator[str]) -> AsyncIterator[ConsoleEvent]:
        async for line in lines:
            for event in self.ingest_line(line):
                yield event

    def parse_sync(self, lines: Iterator[str]) -> Iterator[ConsoleEvent]:
        for line in lines:
            for event in self.ingest_line(line):
                yield event


__all__ = ["ConsoleEvent", "ConsoleEventType", "ConsoleStreamParser"]
