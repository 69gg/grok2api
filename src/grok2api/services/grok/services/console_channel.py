"""Console.x.ai channel orchestration for chat / responses / anthropic APIs."""

from __future__ import annotations

import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

import orjson
from curl_cffi.requests import AsyncSession

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException, ValidationException
from grok2api.services.grok.services.console_capabilities import (
    filter_payload,
    get_console_capabilities,
    merge_tools,
)
from grok2api.services.grok.services.console_input import ConsoleInputBuilder
from grok2api.services.grok.services.console_replay import reject_incremental_previous_response
from grok2api.services.grok.services.console_output_adapters import (
    ConsoleChatStreamAdapter,
    ConsoleResponsesStreamAdapter,
    anthropic_usage_from_chat,
)
from grok2api.services.grok.services.console_stream_parser import ConsoleStreamParser
from grok2api.services.grok.services.model import Channel, ModelService
from grok2api.services.grok.utils.errors import no_token_error
from grok2api.services.grok.utils.retry import pick_token
from grok2api.services.reverse.console_constants import CONSOLE_ALLOW_PREVIOUS_RESPONSE_ID
from grok2api.services.reverse.console_payload import merge_console_payload, sanitize_console_upstream_payload
from grok2api.services.reverse.console_responses import ConsoleResponsesReverse
from grok2api.services.token import get_token_manager


def _resolve_stream(stream: Optional[bool]) -> bool:
    if stream is not None:
        return bool(stream)
    return bool(get_config("app.stream", True))


def _resolve_model(model_id: str):
    model = ModelService.get(model_id)
    if not model or model.channel != Channel.CONSOLE:
        raise ValidationException(
            message=f"Model `{model_id}` is not a console model",
            param="model",
            code="model_not_found",
        )
    return model


def _response_format_to_text_format(response_format: Any) -> Optional[Dict[str, Any]]:
    """Map OpenAI response_format to console Responses API text.format."""
    if response_format is None:
        return None
    if isinstance(response_format, str):
        if response_format == "json_object":
            return {"type": "json_object"}
        return {"type": "text"}
    if not isinstance(response_format, dict):
        return {"type": "text"}

    rf_type = response_format.get("type")
    if rf_type == "json_schema":
        # Responses API uses flat fields under text.format (not Chat's nested json_schema).
        if isinstance(response_format.get("schema"), dict):
            return {
                "type": "json_schema",
                "name": response_format.get("name") or "response",
                "schema": response_format["schema"],
                "strict": bool(response_format.get("strict", True)),
            }
        wrapper = response_format.get("json_schema") or {}
        if isinstance(wrapper, dict):
            return {
                "type": "json_schema",
                "name": wrapper.get("name") or response_format.get("name") or "response",
                "schema": wrapper.get("schema") or {},
                "strict": bool(wrapper.get("strict", response_format.get("strict", True))),
            }
        return {
            "type": "json_schema",
            "name": response_format.get("name") or "response",
            "schema": {},
            "strict": bool(response_format.get("strict", True)),
        }
    if rf_type == "json_object":
        return {"type": "json_object"}
    return {"type": "text"}


class ConsoleChannelService:
    @staticmethod
    async def _stream_upstream(
        payload: Dict[str, Any],
        *,
        token: str,
    ) -> AsyncIterator[str]:
        session = AsyncSession()
        line_iter = await ConsoleResponsesReverse.request(
            session,
            token,
            payload,
            stream=True,
        )
        async for line in line_iter:
            yield line

    @staticmethod
    async def _execute_with_token(
        model_id: str,
        build_payload_fn,
        *,
        stream: bool,
    ):
        token_mgr = await get_token_manager()
        await token_mgr.reload_if_stale()
        tried: set[str] = set()
        max_retries = int(get_config("retry.max_retry") or 3)
        last_error = None

        for _ in range(max_retries):
            token = await pick_token(token_mgr, model_id, tried)
            if not token:
                if last_error:
                    raise last_error
                raise no_token_error(model_id)
            tried.add(token)
            try:
                payload = await build_payload_fn(token)
                if stream:
                    async def gen():
                        async for line in ConsoleChannelService._stream_upstream(
                            payload, token=token
                        ):
                            yield line
                    return gen()
                lines = []
                async for line in ConsoleChannelService._stream_upstream(
                    payload, token=token
                ):
                    lines.append(line)
                return lines
            except UpstreamException as exc:
                last_error = exc
                continue
        if last_error:
            raise last_error
        raise no_token_error(model_id)

    @staticmethod
    async def chat_completions(
        *,
        model: str,
        messages: List[Dict[str, Any]],
        stream: Optional[bool] = None,
        reasoning_effort: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = None,
        parallel_tool_calls: Optional[bool] = True,
        max_tokens: Optional[int] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        response_format: Any = None,
    ):
        model_info = _resolve_model(model)
        caps = model_info.capabilities or get_console_capabilities(model_info.console_model or model)
        stream_flag = _resolve_stream(stream)

        async def build(_token: str) -> Dict[str, Any]:
            instructions, input_items, history_encrypted = ConsoleInputBuilder.from_chat_messages(
                messages
            )
            merged_tools = merge_tools(
                ConsoleInputBuilder.normalize_tools(tools),
                console_search=bool(model_info.console_search),
            )
            payload = ConsoleInputBuilder.build_payload(
                console_model=model_info.console_model or model,
                caps=caps,
                input_items=input_items,
                instructions=instructions,
                stream=stream_flag,
                tools=merged_tools or None,
                tool_choice=tool_choice,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                text_format=_response_format_to_text_format(response_format),
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
                parallel_tool_calls=parallel_tool_calls,
                history_has_encrypted=history_encrypted,
                thinking_enabled=bool(reasoning_effort and reasoning_effort.lower() != "none"),
            )
            return sanitize_console_upstream_payload(filter_payload(caps, payload))

        result = await ConsoleChannelService._execute_with_token(
            model, build, stream=stream_flag
        )

        if stream_flag:
            async def chat_stream():
                adapter = ConsoleChatStreamAdapter(model)
                parser = ConsoleStreamParser()
                async for line in result:
                    for event in parser.ingest_line(line):
                        for chunk in adapter.ingest(event):
                            yield chunk
                for chunk in adapter.finalize():
                    yield chunk
            return chat_stream()

        parser = ConsoleStreamParser()
        adapter = ConsoleChatStreamAdapter(model)
        for line in result:
            for event in parser.ingest_line(line):
                adapter.ingest(event)
        return adapter.build_non_stream_response()

    @staticmethod
    async def responses(
        *,
        model: str,
        input_value: Any,
        instructions: Optional[str] = None,
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = None,
        parallel_tool_calls: Optional[bool] = True,
        reasoning: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        text: Optional[Dict[str, Any]] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        include: Optional[List[str]] = None,
        store: Optional[bool] = False,
        previous_response_id: Optional[str] = None,
        **extra: Any,
    ):
        had_previous_response_id = bool(previous_response_id)
        if previous_response_id and not CONSOLE_ALLOW_PREVIOUS_RESPONSE_ID:
            previous_response_id = None

        model_info = _resolve_model(model)
        caps = model_info.capabilities or get_console_capabilities(model_info.console_model or model)
        stream_flag = _resolve_stream(stream)
        reasoning_effort = None
        if isinstance(reasoning, dict):
            reasoning_effort = reasoning.get("effort")

        async def build(_token: str) -> Dict[str, Any]:
            instr, input_items, history_encrypted = ConsoleInputBuilder.from_responses_input(
                input_value, instructions=instructions
            )
            reject_incremental_previous_response(
                had_previous_response_id=had_previous_response_id,
                input_items=input_items,
            )
            merged_tools = merge_tools(
                ConsoleInputBuilder.normalize_tools(tools),
                console_search=bool(model_info.console_search),
            )
            text_format = None
            if isinstance(text, dict):
                fmt = text.get("format") if isinstance(text.get("format"), dict) else text
                text_format = _response_format_to_text_format(fmt)
            payload = ConsoleInputBuilder.build_payload(
                console_model=model_info.console_model or model,
                caps=caps,
                input_items=input_items,
                instructions=instr,
                stream=stream_flag,
                tools=merged_tools or None,
                tool_choice=tool_choice,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                text_format=text_format,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
                parallel_tool_calls=parallel_tool_calls,
                request_include=include,
                history_has_encrypted=history_encrypted,
                store=bool(store),
            )
            if previous_response_id and CONSOLE_ALLOW_PREVIOUS_RESPONSE_ID:
                payload["previous_response_id"] = previous_response_id
            return sanitize_console_upstream_payload(
                filter_payload(caps, merge_console_payload(payload, extra))
            )

        result = await ConsoleChannelService._execute_with_token(
            model, build, stream=stream_flag
        )

        if stream_flag:
            async def resp_stream():
                adapter = ConsoleResponsesStreamAdapter(model)
                async for line in result:
                    out = adapter.ingest_raw_line(line)
                    if out:
                        yield out
            return resp_stream()

        adapter = ConsoleResponsesStreamAdapter(model)
        for line in result:
            adapter.ingest_raw_line(line)
        if adapter.completed_response:
            response = adapter.completed_response.get("response") or adapter.completed_response
            response["model"] = model
            return response
        return {"id": f"resp_{uuid.uuid4().hex[:24]}", "object": "response", "model": model, "output": []}

    @staticmethod
    async def messages(
        *,
        model: str,
        messages: List[Dict[str, Any]],
        system: Optional[Any] = None,
        stream: Optional[bool] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Any = None,
        thinking: Optional[Dict[str, Any]] = None,
    ):
        model_info = _resolve_model(model)
        caps = model_info.capabilities or get_console_capabilities(model_info.console_model or model)
        stream_flag = _resolve_stream(stream)

        instr, input_items, history_encrypted = ConsoleInputBuilder.from_anthropic_raw_messages(
            messages
        )
        if system:
            if isinstance(system, str):
                instr = f"{system}\n\n{instr}" if instr else system
            elif isinstance(system, list):
                parts = []
                for block in system:
                    if isinstance(block, dict) and block.get("text"):
                        parts.append(str(block["text"]))
                if parts:
                    sys_text = "\n".join(parts)
                    instr = f"{sys_text}\n\n{instr}" if instr else sys_text

        thinking_enabled = False
        reasoning_effort = None
        if isinstance(thinking, dict):
            thinking_type = str(thinking.get("type") or "").strip().lower()
            if thinking.get("enabled") or thinking_type in {"enabled", "on", "true"}:
                thinking_enabled = True
                reasoning_effort = thinking.get("effort") or "low"

        async def build(_token: str) -> Dict[str, Any]:
            merged_tools = merge_tools(
                ConsoleInputBuilder.normalize_tools(tools),
                console_search=bool(model_info.console_search),
            )
            payload = ConsoleInputBuilder.build_payload(
                console_model=model_info.console_model or model,
                caps=caps,
                input_items=input_items,
                instructions=instr,
                stream=stream_flag,
                tools=merged_tools or None,
                tool_choice=tool_choice,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_tokens,
                reasoning_effort=reasoning_effort if thinking_enabled else None,
                history_has_encrypted=history_encrypted,
                thinking_enabled=thinking_enabled,
            )
            return sanitize_console_upstream_payload(filter_payload(caps, payload))

        result = await ConsoleChannelService._execute_with_token(model, build, stream=stream_flag)

        if stream_flag:
            async def anthropic_stream():
                from grok2api.api.v1.messages import _AnthropicStreamAdapter

                message_id = f"msg_{uuid.uuid4().hex[:24]}"
                adapter = _AnthropicStreamAdapter(
                    model=model,
                    message_id=message_id,
                    include_thinking=thinking_enabled,
                    stop_sequences=[],
                )
                chat_adapter = ConsoleChatStreamAdapter(model)
                parser = ConsoleStreamParser()
                async for line in result:
                    for event in parser.ingest_line(line):
                        for chunk in chat_adapter.ingest(event):
                            if not chunk.startswith("data:"):
                                continue
                            payload_text = chunk[5:].strip()
                            if payload_text == "[DONE]":
                                break
                            try:
                                data = orjson.loads(payload_text)
                            except orjson.JSONDecodeError:
                                continue
                            if data.get("object") != "chat.completion.chunk":
                                continue
                            for ev in adapter.ensure_message_started():
                                yield ev
                            choice = (data.get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            if isinstance(delta.get("reasoning_content"), str):
                                for ev in adapter.ingest_reasoning(delta["reasoning_content"]):
                                    yield ev
                            if isinstance(delta.get("content"), str):
                                for ev in adapter.ingest_text(delta["content"]):
                                    yield ev
                            for tool_call in delta.get("tool_calls") or []:
                                if isinstance(tool_call, dict):
                                    for ev in adapter.ingest_tool_call(tool_call):
                                        yield ev
                            usage = data.get("usage")
                            finish = choice.get("finish_reason")
                            if finish is not None:
                                for ev in adapter.finalize(usage, finish):
                                    yield ev
            return anthropic_stream()

        parser = ConsoleStreamParser()
        chat_adapter = ConsoleChatStreamAdapter(model)
        for line in result:
            for event in parser.ingest_line(line):
                chat_adapter.ingest(event)
        chat_result = chat_adapter.build_non_stream_response()
        choice = (chat_result.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content_blocks: List[Dict[str, Any]] = []
        if message.get("reasoning_content"):
            content_blocks.append(
                {"type": "thinking", "thinking": message["reasoning_content"], "signature": ""}
            )
        if message.get("content"):
            content_blocks.append({"type": "text", "text": message["content"]})
        for tool_call in message.get("tool_calls") or []:
            fn = tool_call.get("function") or {}
            try:
                parsed = orjson.loads(fn.get("arguments") or "{}")
            except orjson.JSONDecodeError:
                parsed = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_call.get("id"),
                    "name": fn.get("name"),
                    "input": parsed if isinstance(parsed, dict) else {},
                }
            )
        usage = anthropic_usage_from_chat(chat_result.get("usage"))
        return {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content_blocks,
            "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
            "stop_sequence": None,
            "usage": usage,
        }


__all__ = ["ConsoleChannelService"]
