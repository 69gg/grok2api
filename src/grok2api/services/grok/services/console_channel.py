"""Console.x.ai channel orchestration for chat / responses / anthropic APIs."""

from __future__ import annotations

import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

import orjson
from curl_cffi.requests import AsyncSession

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException, ValidationException
from grok2api.core.logger import logger
from grok2api.services.grok.services.console_capabilities import (
    filter_payload,
    get_console_capabilities,
    merge_tools,
    prepare_console_tooling,
)
from grok2api.services.grok.services.console_input import ConsoleInputBuilder
from grok2api.services.grok.services.console_output_adapters import (
    ConsoleChatStreamAdapter,
    ConsoleResponsesStreamAdapter,
    anthropic_usage_from_chat,
)
from grok2api.services.grok.services.console_stream_parser import ConsoleStreamParser
from grok2api.services.grok.services.model import Channel, ModelService
from grok2api.services.grok.utils.errors import no_token_error
from grok2api.services.grok.utils.retry import pick_token, rate_limited
from grok2api.services.grok.utils.tool_call import format_tool_history
from grok2api.services.reverse.console_payload import merge_console_payload, sanitize_console_upstream_payload
from grok2api.services.reverse.console_responses import ConsoleResponsesReverse
from grok2api.services.token import get_token_manager
from grok2api.services.token.service import TokenService


def _reasoning_config_from_thinking(thinking: Any) -> Optional[Dict[str, Any]]:
    """Map legacy Undefined ``thinking`` payloads to Responses ``reasoning``."""
    if not isinstance(thinking, dict):
        return None
    thinking_type = str(thinking.get("type") or "").strip().lower()
    if not (thinking.get("enabled") or thinking_type in {"enabled", "on", "true"}):
        return None
    effort = str(thinking.get("effort") or "medium").strip().lower() or "medium"
    return {"effort": effort}


def _resolve_stream(stream: Optional[bool]) -> bool:
    if stream is not None:
        return bool(stream)
    return bool(get_config("app.stream", True))


def _resolve_console_tooling(
    *,
    caps,
    tools: Optional[List[Dict[str, Any]]],
    console_search: bool,
    tool_choice: Any,
    parallel_tool_calls: Optional[bool],
    instructions: Optional[str],
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str], Optional[List[Dict[str, Any]]], Any]:
    normalized_tools = ConsoleInputBuilder.normalize_tools(tools)
    return prepare_console_tooling(
        caps=caps,
        client_tools=normalized_tools or None,
        console_search=console_search,
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls if parallel_tool_calls is not None else True,
        instructions=instructions,
    )


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
    async def _handle_token_upstream_failure(
        token_mgr: Any,
        token: str,
        exc: UpstreamException,
    ) -> None:
        status = (exc.details or {}).get("status")
        if status == 401:
            try:
                await TokenService.record_fail(token, status, "console_auth_failed")
            except Exception:
                pass
        elif rate_limited(exc):
            try:
                await token_mgr.mark_rate_limited(token)
            except Exception:
                pass

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

        if stream:

            async def gen():
                last_err: Optional[UpstreamException] = None
                for attempt in range(max_retries):
                    token = await pick_token(token_mgr, model_id, tried)
                    if not token:
                        break
                    tried.add(token)
                    try:
                        payload = await build_payload_fn(token)
                        async for line in ConsoleChannelService._stream_upstream(
                            payload, token=token
                        ):
                            yield line
                        return
                    except UpstreamException as exc:
                        last_err = exc
                        await ConsoleChannelService._handle_token_upstream_failure(
                            token_mgr, token, exc
                        )
                        status = (exc.details or {}).get("status")
                        logger.warning(
                            f"Console upstream failed for token {token[:10]}... "
                            f"status={status}, trying next token "
                            f"(attempt {attempt + 1}/{max_retries})"
                        )
                        continue
                if last_err:
                    raise last_err
                raise no_token_error(model_id)

            return gen()

        for attempt in range(max_retries):
            token = await pick_token(token_mgr, model_id, tried)
            if not token:
                if last_error:
                    raise last_error
                raise no_token_error(model_id)
            tried.add(token)
            try:
                payload = await build_payload_fn(token)
                lines = []
                async for line in ConsoleChannelService._stream_upstream(
                    payload, token=token
                ):
                    lines.append(line)
                return lines
            except UpstreamException as exc:
                last_error = exc
                await ConsoleChannelService._handle_token_upstream_failure(
                    token_mgr, token, exc
                )
                status = (exc.details or {}).get("status")
                logger.warning(
                    f"Console upstream failed for token {token[:10]}... "
                    f"status={status}, trying next token "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
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

        chat_messages = messages
        if not caps.supports_function_calling and tools and tool_choice != "none":
            chat_messages = format_tool_history(messages)
        instructions, input_items, history_encrypted = ConsoleInputBuilder.from_chat_messages(
            chat_messages
        )
        upstream_tools, instructions, prompt_tools, upstream_tool_choice = _resolve_console_tooling(
            caps=caps,
            tools=tools,
            console_search=bool(model_info.console_search),
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            instructions=instructions,
        )

        async def build(_token: str) -> Dict[str, Any]:
            payload = ConsoleInputBuilder.build_payload(
                console_model=model_info.console_model or model,
                caps=caps,
                input_items=input_items,
                instructions=instructions,
                stream=stream_flag,
                tools=upstream_tools,
                tool_choice=upstream_tool_choice if upstream_tools else None,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                reasoning_config={"effort": reasoning_effort} if reasoning_effort else None,
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
                adapter = ConsoleChatStreamAdapter(
                    model, prompt_tools=prompt_tools, tool_choice=tool_choice
                )
                parser = ConsoleStreamParser()
                async for line in result:
                    for event in parser.ingest_line(line):
                        for chunk in adapter.ingest(event):
                            yield chunk
                for chunk in adapter.finalize():
                    yield chunk
            return chat_stream()

        parser = ConsoleStreamParser()
        adapter = ConsoleChatStreamAdapter(
            model, prompt_tools=prompt_tools, tool_choice=tool_choice
        )
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
        **extra: Any,
    ):
        model_info = _resolve_model(model)
        caps = model_info.capabilities or get_console_capabilities(model_info.console_model or model)
        stream_flag = _resolve_stream(stream)
        reasoning_effort = None
        reasoning_config = dict(reasoning) if isinstance(reasoning, dict) else None
        if reasoning_config:
            reasoning_effort = reasoning_config.get("effort")
        if not reasoning_config:
            derived = _reasoning_config_from_thinking(extra.get("thinking"))
            if derived:
                reasoning_config = derived
                reasoning_effort = derived.get("effort")

        async def build(_token: str) -> Dict[str, Any]:
            instr, input_items, history_encrypted = ConsoleInputBuilder.from_responses_input(
                input_value, instructions=instructions
            )
            upstream_tools, instr, _prompt_tools, upstream_tool_choice = _resolve_console_tooling(
                caps=caps,
                tools=tools,
                console_search=bool(model_info.console_search),
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                instructions=instr,
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
                tools=upstream_tools,
                tool_choice=upstream_tool_choice if upstream_tools else None,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                reasoning_config=reasoning_config,
                text_format=text_format,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
                parallel_tool_calls=parallel_tool_calls,
                request_include=include,
                history_has_encrypted=history_encrypted,
                store=bool(store),
            )
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

        anthropic_messages = messages
        if not caps.supports_function_calling and tools and tool_choice != "none":
            anthropic_messages = format_tool_history(messages)
        instr, input_items, history_encrypted = ConsoleInputBuilder.from_anthropic_raw_messages(
            anthropic_messages
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

        upstream_tools, instr, prompt_tools, upstream_tool_choice = _resolve_console_tooling(
            caps=caps,
            tools=tools,
            console_search=bool(model_info.console_search),
            tool_choice=tool_choice,
            parallel_tool_calls=True,
            instructions=instr,
        )

        async def build(_token: str) -> Dict[str, Any]:
            payload = ConsoleInputBuilder.build_payload(
                console_model=model_info.console_model or model,
                caps=caps,
                input_items=input_items,
                instructions=instr,
                stream=stream_flag,
                tools=upstream_tools,
                tool_choice=upstream_tool_choice if upstream_tools else None,
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_tokens,
                reasoning_effort=reasoning_effort if thinking_enabled else None,
                reasoning_config={"effort": reasoning_effort} if thinking_enabled and reasoning_effort else None,
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
                chat_adapter = ConsoleChatStreamAdapter(
                    model, prompt_tools=prompt_tools, tool_choice=tool_choice
                )
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
        chat_adapter = ConsoleChatStreamAdapter(
            model, prompt_tools=prompt_tools, tool_choice=tool_choice
        )
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
