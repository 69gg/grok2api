"""Unit tests for console channel helpers."""

from __future__ import annotations

import pytest

from grok2api.core.exceptions import UpstreamException
from grok2api.services.grok.services import console_channel as console_channel_module
from grok2api.services.grok.services.console_channel import _response_format_to_text_format
from grok2api.services.grok.services.console_capabilities import (
    CAP_GROK_43,
    CAP_MULTI_AGENT,
    CAP_420_REASONING,
    filter_payload,
    merge_tools,
    normalize_reasoning_effort,
    prepare_console_tooling,
    should_include_encrypted,
    detect_console_responses_client_mode,
)
from grok2api.services.grok.services.console_channel import (
    CONSOLE_RESPONSES_FALLBACK_STRATEGIES,
    ConsoleChannelService,
)
from grok2api.services.grok.services.console_input import (
    ConsoleInputBuilder,
    drop_compaction_blobs_from_payload,
    is_encrypted_reasoning,
    prompt_tool_history_items,
)
from grok2api.services.grok.services.console_output_adapters import (
    ConsoleChatStreamAdapter,
    ConsoleResponsesPassthroughAdapter,
)
from grok2api.services.grok.services.console_stream_parser import (
    ConsoleEvent,
    ConsoleEventType,
    ConsoleStreamParser,
)
from grok2api.services.grok.services.model import CONSOLE_MODEL_IDS, ModelService
from grok2api.services.grok.utils.usage import from_upstream_responses_usage
from grok2api.services.reverse.console_payload import (
    merge_console_payload,
    strip_console_client_extra,
)


def test_console_models_registered_and_listed():
    for model_id in CONSOLE_MODEL_IDS:
        assert ModelService.valid(model_id)
        info = ModelService.get(model_id)
        assert info is not None
        assert info.owned_by == "xai-console<grok2api@69gg>"


def test_from_upstream_responses_usage_maps_cached_and_reasoning():
    usage = from_upstream_responses_usage(
        {
            "input_tokens": 155,
            "input_tokens_details": {"cached_tokens": 64},
            "output_tokens": 2110,
            "output_tokens_details": {"reasoning_tokens": 1463},
            "total_tokens": 2265,
        }
    )
    assert usage["prompt_tokens"] == 155
    assert usage["completion_tokens"] == 2110
    assert usage["total_tokens"] == 2265
    assert usage["prompt_tokens_details"]["cached_tokens"] == 64
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 1463


def test_responses_input_passthrough_items_keeps_reasoning_shape():
    input_items = [
        {
            "type": "reasoning",
            "id": "rs_keep",
            "status": "in_progress",
            "summary": [{"type": "summary_text", "text": "visible"}],
        },
    ]
    _, items, history_encrypted = ConsoleInputBuilder.from_responses_input(
        input_items,
        passthrough_items=True,
    )
    assert history_encrypted is False
    assert items[0]["id"] == "rs_keep"
    assert items[0]["summary"][0]["text"] == "visible"
    assert items[0]["status"] == "in_progress"


def test_chat_messages_plaintext_reasoning_becomes_summary_item():
    messages = [
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "step one",
        },
        {"role": "user", "content": "next"},
    ]
    _, items, history_encrypted = ConsoleInputBuilder.from_chat_messages(messages)
    assert history_encrypted is False
    assert items[0]["type"] == "reasoning"
    assert items[0]["summary"][0]["text"] == "step one"
    assert items[1]["type"] == "message"
    assert items[1]["content"][0]["text"] == "answer"


def test_chat_messages_to_input_with_tool_and_encrypted_reasoning():
    blob = "A" * 120
    assert is_encrypted_reasoning(blob)
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "reasoning_content": blob,
            "content": "answer",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]
    _, items, history_encrypted = ConsoleInputBuilder.from_chat_messages(messages)
    assert history_encrypted is True
    assert any(i.get("type") == "reasoning" for i in items)
    assert any(i.get("type") == "function_call" for i in items)
    assert any(i.get("type") == "function_call_output" for i in items)


def test_merge_tools_adds_search_tools_for_search_variant():
    tools = merge_tools([{"type": "function", "name": "foo", "parameters": {}}], console_search=True)
    types = {t["type"] for t in tools}
    assert "web_search" in types
    assert "x_search" in types


def test_console_responses_fallback_strategy_order():
    names = [row[0] for row in CONSOLE_RESPONSES_FALLBACK_STRATEGIES]
    assert names == [
        "grok_passthrough",
        "grok_passthrough_strip_encrypted",
        "openai_compat",
        "openai_compat_strip_encrypted",
    ]


@pytest.mark.asyncio
async def test_console_responses_stream_fallback_retries_before_first_event(monkeypatch):
    calls: list[dict[str, object]] = []

    async def fake_execute_with_token(model_id, build_payload_fn, *, stream):
        payload = await build_payload_fn("token")
        calls.append(payload)

        async def fake_stream():
            if len(calls) == 1:
                raise UpstreamException(
                    message="ConsoleResponsesReverse: request failed, 400",
                    details={
                        "status": 400,
                        "body": "Could not decrypt the provided encrypted_content.",
                    },
                )
            yield 'data: {"type":"response.completed","response":{"id":"resp_ok"}}\n\n'

        return fake_stream()

    monkeypatch.setattr(
        ConsoleChannelService,
        "_execute_with_token",
        fake_execute_with_token,
    )

    def build_for_strategy(passthrough_items: bool, strip_encrypted: bool):
        async def build(_token: str) -> dict[str, object]:
            return {
                "passthrough_items": passthrough_items,
                "strip_encrypted": strip_encrypted,
            }

        return build

    result = await ConsoleChannelService._execute_responses_with_fallback(
        "grok-4.20-multi-agent-0309",
        stream=True,
        build_for_strategy=build_for_strategy,
        strategies=(
            ("first", True, False),
            ("strip", True, True),
        ),
    )

    chunks: list[str] = []
    async for chunk in result:
        chunks.append(chunk)

    assert chunks == ['data: {"type":"response.completed","response":{"id":"resp_ok"}}\n\n']
    assert calls == [
        {"passthrough_items": True, "strip_encrypted": False},
        {"passthrough_items": True, "strip_encrypted": True},
    ]


@pytest.mark.asyncio
async def test_stream_upstream_does_not_double_close_returned_stream(monkeypatch):
    sessions: list[object] = []

    class FakeSession:
        def __init__(self) -> None:
            self.close_count = 0
            sessions.append(self)

        async def close(self) -> None:
            self.close_count += 1

    async def fake_request(session, token, payload, *, stream):
        async def gen():
            try:
                yield "line-1"
            finally:
                await session.close()

        return gen()

    monkeypatch.setattr(console_channel_module, "AsyncSession", FakeSession)
    monkeypatch.setattr(
        console_channel_module.ConsoleResponsesReverse,
        "request",
        fake_request,
    )

    chunks: list[str] = []
    async for chunk in ConsoleChannelService._stream_upstream({"input": []}, token="token"):
        chunks.append(chunk)

    assert chunks == ["line-1"]
    assert len(sessions) == 1
    assert sessions[0].close_count == 1


@pytest.mark.asyncio
async def test_stream_upstream_closes_session_when_request_fails(monkeypatch):
    sessions: list[object] = []

    class FakeSession:
        def __init__(self) -> None:
            self.close_count = 0
            sessions.append(self)

        async def close(self) -> None:
            self.close_count += 1

    async def fake_request(session, token, payload, *, stream):
        raise UpstreamException(
            message="request failed before stream iterator",
            details={"status": 400},
        )

    monkeypatch.setattr(console_channel_module, "AsyncSession", FakeSession)
    monkeypatch.setattr(
        console_channel_module.ConsoleResponsesReverse,
        "request",
        fake_request,
    )

    with pytest.raises(UpstreamException):
        async for _ in ConsoleChannelService._stream_upstream({"input": []}, token="token"):
            pass

    assert len(sessions) == 1
    assert sessions[0].close_count == 1


def test_detect_console_responses_client_mode_grok_first():
    assert (
        detect_console_responses_client_mode(
            CAP_420_REASONING,
            input_value=[{"role": "user", "content": "hi"}],
        )
        == "xai_native"
    )
    assert (
        detect_console_responses_client_mode(
            CAP_GROK_43,
            input_value=[{"type": "reasoning", "encrypted_content": "E" * 120}],
        )
        == "xai_native"
    )
    assert (
        detect_console_responses_client_mode(
            CAP_GROK_43,
            request_include=["reasoning.encrypted_content"],
            input_value=[{"role": "user", "content": "hi"}],
        )
        == "xai_native"
    )
    assert (
        detect_console_responses_client_mode(
            CAP_GROK_43,
            input_value=[
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "search",
                    "arguments": "{}",
                }
            ],
        )
        == "xai_native"
    )
    assert (
        detect_console_responses_client_mode(
            CAP_GROK_43,
            input_value=[{"role": "user", "content": "hello"}],
            reasoning={"effort": "high"},
        )
        == "openai_compat"
    )
    assert (
        detect_console_responses_client_mode(
            CAP_GROK_43,
            reasoning={"effort": "high", "summary": "auto"},
        )
        == "xai_native"
    )


def test_grok43_summary_mode_skips_encrypted_include():
    assert (
        should_include_encrypted(
            CAP_GROK_43,
            reasoning_effort="high",
            thinking_enabled=False,
            request_include=None,
            history_has_encrypted=False,
        )
        is False
    )


def test_build_payload_filters_invalid_include_and_tool_choice():
    from grok2api.services.grok.services.console_capabilities import CAP_GROK_43

    payload = ConsoleInputBuilder.build_payload(
        console_model="grok-4.3",
        caps=CAP_GROK_43,
        input_items=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        stream=True,
        tools=None,
        tool_choice="required",
        request_include=["foo", "reasoning.encrypted_content"],
    )
    assert payload.get("include") == ["reasoning.encrypted_content"]
    assert "tool_choice" not in payload
    assert "tools" not in payload
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}


def test_build_payload_uses_openai_reasoning_summary_auto():
    from grok2api.services.grok.services.console_capabilities import CAP_GROK_43

    payload = ConsoleInputBuilder.build_payload(
        console_model="grok-4.3",
        caps=CAP_GROK_43,
        input_items=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        stream=True,
        reasoning_config={"effort": "medium"},
    )
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}


def test_build_payload_infers_reasoning_when_client_requests_encrypted_only():
    from grok2api.services.grok.services.console_capabilities import CAP_GROK_43

    payload = ConsoleInputBuilder.build_payload(
        console_model="grok-4.3",
        caps=CAP_GROK_43,
        input_items=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        stream=True,
        request_include=["reasoning.encrypted_content"],
    )
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert payload["include"] == ["reasoning.encrypted_content"]


def test_responses_passthrough_forwards_summary_delta_without_injection():
    from grok2api.services.grok.services.console_output_adapters import (
        ConsoleResponsesPassthroughAdapter,
    )

    adapter = ConsoleResponsesPassthroughAdapter("grok-4.3")
    delta_line = adapter.ingest_raw_line(
        'data: {"type":"response.reasoning_summary_text.delta","output_index":0,"delta":"Let me think"}'
    )
    assert delta_line is not None
    assert "Let me think" in delta_line
    completed = adapter.ingest_raw_line(
        'data: {"type":"response.completed","response":{"id":"resp_1","output":[{"type":"reasoning","id":"rs_1"}]}}'
    )
    assert completed is not None
    output = adapter.completed_response["response"]["output"]
    assert not output or "summary" not in (output[0] if output else {})


def test_chat_adapter_encrypted_reasoning_byte_exact_no_summary():
    blob = "E" * 120
    adapter = ConsoleChatStreamAdapter("grok-build-0.1", emit_plaintext_reasoning=False)
    summary_chunks = adapter.ingest(
        ConsoleEvent(ConsoleEventType.REASONING_SUMMARY_DELTA, {"delta": "visible"})
    )
    assert all("reasoning_content" not in chunk for chunk in summary_chunks)
    adapter.ingest(ConsoleEvent(ConsoleEventType.REASONING_DONE, {"encrypted_content": blob}))
    adapter.ingest(
        ConsoleEvent(
            ConsoleEventType.COMPLETED,
            {
                "response": {
                    "output": [
                        {
                            "type": "reasoning",
                            "encrypted_content": blob,
                            "summary": [{"type": "summary_text", "text": "leak"}],
                        }
                    ]
                }
            },
        )
    )
    message = adapter.build_non_stream_response()["choices"][0]["message"]
    assert message["reasoning_content"] == blob


def test_responses_passthrough_preserves_upstream_encrypted_output():
    import orjson

    from grok2api.services.grok.services.console_output_adapters import (
        ConsoleResponsesPassthroughAdapter,
    )

    blob = "F" * 120
    adapter = ConsoleResponsesPassthroughAdapter("grok-build-0.1")
    payload = {
        "type": "response.completed",
        "response": {
            "id": "resp_enc",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": blob,
                    "summary": [{"type": "summary_text", "text": "hide"}],
                }
            ],
        },
    }
    completed = adapter.ingest_raw_line(f"data: {orjson.dumps(payload).decode()}")
    assert completed is not None
    reasoning = adapter.completed_response["response"]["output"][0]
    assert reasoning["encrypted_content"] == blob
    assert reasoning["summary"][0]["text"] == "hide"


def test_responses_input_replay_preserves_summary_only_reasoning():
    input_items = [
        {
            "type": "reasoning",
            "id": "rs_abc",
            "summary": [{"type": "summary_text", "text": "thinking..."}],
        },
        {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "hello"}],
        },
    ]
    _, items, history_encrypted = ConsoleInputBuilder.from_responses_input(input_items)
    assert history_encrypted is False
    assert items[0]["type"] == "reasoning"
    assert items[0]["summary"][0]["text"] == "thinking..."
    assert items[1]["phase"] == "final_answer"


def test_stream_parser_completed_carries_usage():
    parser = ConsoleStreamParser()
    events = parser.ingest_line(
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}}}'
    )
    assert events[-1].type == ConsoleEventType.COMPLETED
    assert events[-1].data["usage"]["total_tokens"] == 3


def test_stream_parser_accepts_bytes_sse_line():
    parser = ConsoleStreamParser()
    events = parser.ingest_line(
        b'data: {"type":"response.output_text.delta","delta":"hi"}'
    )
    assert len(events) == 1
    assert events[0].type == ConsoleEventType.TEXT_DELTA
    assert events[0].data["delta"] == "hi"


def test_stream_parser_accepts_non_sse_json_response():
    parser = ConsoleStreamParser()
    body = (
        '{"object":"response","output":[{"type":"message","content":'
        '[{"type":"output_text","text":"OK"}]}],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}'
    )
    events = parser.ingest_line(body)
    assert events[-1].type == ConsoleEventType.COMPLETED
    assert events[-1].data["response"]["output"][0]["content"][0]["text"] == "OK"


def test_response_format_to_text_format_uses_responses_api_flat_json_schema():
    chat_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "answer_obj",
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }
    text_format = _response_format_to_text_format(chat_format)
    assert text_format == {
        "type": "json_schema",
        "name": "answer_obj",
        "schema": chat_format["json_schema"]["schema"],
        "strict": True,
    }
    assert "json_schema" not in text_format


def test_chat_adapter_completed_function_call():
    adapter = ConsoleChatStreamAdapter("grok-4.3")
    parser = ConsoleStreamParser()
    body = (
        '{"object":"response","output":[{"type":"function_call","call_id":"call_abc",'
        '"name":"get_weather","arguments":"{\\"city\\":\\"Tokyo\\"}"}],'
        '"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}'
    )
    for event in parser.ingest_line(body):
        adapter.ingest(event)
    result = adapter.build_non_stream_response()
    message = result["choices"][0]["message"]
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert len(message["tool_calls"]) == 1
    assert message["tool_calls"][0]["function"]["name"] == "get_weather"


def test_normalize_tools_accepts_responses_flat_function():
    tools = ConsoleInputBuilder.normalize_tools(
        [
            {
                "type": "function",
                "name": "run_python",
                "description": "Run code",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
    )
    assert len(tools) == 1
    assert tools[0]["name"] == "run_python"


def test_chat_adapter_extracts_non_stream_response_text():
    adapter = ConsoleChatStreamAdapter("grok-4.3")
    parser = ConsoleStreamParser()
    body = (
        '{"object":"response","output":[{"type":"message","content":'
        '[{"type":"output_text","text":"hello"}]}],"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4}}'
    )
    for event in parser.ingest_line(body):
        adapter.ingest(event)
    result = adapter.build_non_stream_response()
    message = result["choices"][0]["message"]
    assert message["content"] == "hello"
    assert result["usage"]["total_tokens"] == 4


def test_strip_console_client_extra_drops_incompatible_fields():
    extra = {
        "stream_options": {"include_usage": True},
        "prompt_cache_key": "abc",
        "thinking": {"type": "enabled"},
        "output_config": {"effort": "high"},
        "temperature": 0.5,
    }
    cleaned = strip_console_client_extra(extra)
    assert cleaned == {"temperature": 0.5}


def test_merge_console_payload_whitelists_upstream_keys():
    payload = merge_console_payload(
        {
            "model": "grok-4.3",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            "stream": True,
            "store": False,
        },
        {"stream_options": {"include_usage": True}, "temperature": 0.2},
    )
    assert "stream_options" not in payload
    assert payload["temperature"] == 0.2
    assert payload["model"] == "grok-4.3"


def test_chat_messages_convert_image_url_to_input_image():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png", "detail": "high"}},
            ],
        }
    ]
    _, items, _ = ConsoleInputBuilder.from_chat_messages(messages)
    assert len(items) == 1
    blocks = items[0]["content"]
    assert blocks[0]["type"] == "input_text"
    assert blocks[1]["type"] == "input_image"
    assert blocks[1]["image_url"] == "https://example.com/a.png"
    assert blocks[1]["detail"] == "high"


def test_responses_input_replay_strips_injected_summary_from_encrypted_reasoning():
    blob = "B" * 120
    input_items = [
        {
            "type": "reasoning",
            "id": "rs_abc",
            "encrypted_content": blob,
            "summary": [{"type": "summary_text", "text": "visible cot"}],
        },
        {
            "type": "function_call",
            "id": "call_bad",
            "call_id": "call_123",
            "name": "search",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "call_123", "output": "ok"},
    ]
    _, items, history_encrypted = ConsoleInputBuilder.from_responses_input(input_items)
    assert history_encrypted is True
    assert items[0] == {
        "type": "reasoning",
        "encrypted_content": blob,
        "status": "completed",
        "summary": [],
        "id": "rs_abc",
    }


def test_responses_input_replay_passthrough_reasoning_and_function_call():
    blob = "B" * 120
    input_items = [
        {"type": "reasoning", "id": "rs_abc", "encrypted_content": blob, "summary": []},
        {
            "type": "function_call",
            "id": "call_bad",
            "call_id": "call_123",
            "name": "search",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "call_123", "output": "ok"},
    ]
    _, items, history_encrypted = ConsoleInputBuilder.from_responses_input(input_items)
    assert history_encrypted is True
    assert items[0]["type"] == "reasoning"
    assert items[0]["encrypted_content"] == blob
    assert items[0]["summary"] == []
    assert items[0]["id"] == "rs_abc"
    assert items[1]["call_id"] == "call_123"
    assert "id" not in items[1]
    assert items[2]["type"] == "function_call_output"


def test_responses_input_replay_passthrough_compaction_item():
    blob = "D" * 120
    input_items = [
        {
            "type": "compaction",
            "id": "cmp_abc",
            "encrypted_content": blob,
            "status": "completed",
        },
        {"role": "user", "content": "continue"},
    ]
    _, items, history_encrypted = ConsoleInputBuilder.from_responses_input(input_items)
    assert history_encrypted is True
    assert items[0]["type"] == "compaction"
    assert items[0]["encrypted_content"] == blob
    assert items[0]["summary"] == []
    assert items[0]["id"] == "cmp_abc"
    assert items[0].get("status") == "completed"


def test_responses_input_replay_user_message_with_input_image():
    input_items = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "https://example.com/x.png"},
            ],
        }
    ]
    _, items, _ = ConsoleInputBuilder.from_responses_input(input_items)
    blocks = items[0]["content"]
    assert blocks[1]["type"] == "input_image"
    assert blocks[1]["image_url"] == "https://example.com/x.png"


def test_drop_compaction_blobs_from_payload():
    blob = "C" * 120
    payload = {
        "model": "grok-4.3",
        "input": [
            {"type": "reasoning", "encrypted_content": "R" * 120},
            {"type": "compaction", "encrypted_content": blob},
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "compaction", "encrypted_content": blob},
        ],
    }
    removed = drop_compaction_blobs_from_payload(payload)
    assert removed == 3
    assert len(payload["input"]) == 1
    assert payload["input"][0]["role"] == "user"


def test_is_compaction_blob_decode_error():
    from grok2api.services.reverse.console_responses import (
        _is_compaction_blob_decode_error,
        _is_encrypted_replay_decode_error,
    )

    body = (
        '{"code":"Client specified an invalid argument","error":'
        '"Could not decode the compaction blob. Ensure it is unmodified from the compact response."}'
    )
    decrypt_body = (
        '{"code":"Client specified an invalid argument","error":'
        '"Could not decrypt the provided encrypted_content. Ensure the value is the '
        'unmodified encrypted_content from a previous response."}'
    )
    assert _is_compaction_blob_decode_error(400, body) is True
    assert _is_encrypted_replay_decode_error(400, decrypt_body) is True
    assert _is_compaction_blob_decode_error(401, body) is False
    assert _is_compaction_blob_decode_error(400, '{"error":"other"}') is False


def test_multi_agent_prepare_console_tooling_prompt_mode():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    upstream_tools, instructions, prompt_tools, upstream_tool_choice = prepare_console_tooling(
        caps=CAP_MULTI_AGENT,
        client_tools=tools,
        console_search=False,
        tool_choice="auto",
        parallel_tool_calls=True,
        instructions="You are helpful.",
    )
    assert upstream_tools is None
    assert upstream_tool_choice is None
    assert prompt_tools == tools
    assert instructions is not None
    assert "Tool Calling Contract" in instructions
    assert "get_weather" in instructions
    assert "You are helpful." in instructions


def test_multi_agent_search_uses_prompt_tools_and_search_params():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    upstream_tools, instructions, prompt_tools, upstream_tool_choice = prepare_console_tooling(
        caps=CAP_MULTI_AGENT,
        client_tools=tools,
        console_search=True,
        tool_choice="required",
        parallel_tool_calls=True,
        instructions=None,
    )
    assert prompt_tools == tools
    assert upstream_tool_choice is None
    assert upstream_tools is not None
    assert {t["type"] for t in upstream_tools} == {"web_search", "x_search"}
    assert "Tool Calling Contract" in (instructions or "")


def test_multi_agent_search_without_client_tools():
    upstream_tools, instructions, prompt_tools, upstream_tool_choice = prepare_console_tooling(
        caps=CAP_MULTI_AGENT,
        client_tools=None,
        console_search=True,
        tool_choice="auto",
        parallel_tool_calls=True,
        instructions="base",
    )
    assert upstream_tools is not None
    assert {t["type"] for t in upstream_tools} == {"web_search", "x_search"}
    assert prompt_tools is None
    assert upstream_tool_choice is None
    assert instructions == "base"


def test_multi_agent_prepare_console_tooling_no_tools():
    upstream_tools, instructions, prompt_tools, upstream_tool_choice = prepare_console_tooling(
        caps=CAP_MULTI_AGENT,
        client_tools=None,
        console_search=False,
        tool_choice="auto",
        parallel_tool_calls=True,
        instructions="base",
    )
    assert upstream_tools is None
    assert prompt_tools is None
    assert upstream_tool_choice is None
    assert instructions == "base"


def test_filter_payload_strips_function_tools_for_multi_agent():
    payload = {
        "model": "grok-4.20-multi-agent-0309",
        "tools": [
            {"type": "function", "name": "x", "parameters": {}},
            {"type": "web_search"},
        ],
        "tool_choice": "auto",
    }
    filtered = filter_payload(CAP_MULTI_AGENT, payload)
    assert filtered["tools"] == [{"type": "web_search"}]
    assert filtered["tool_choice"] == "auto"


def test_filter_payload_drops_tools_when_only_function_tools():
    payload = {
        "model": "grok-4.20-multi-agent-0309",
        "tools": [{"type": "function", "name": "x", "parameters": {}}],
        "tool_choice": "auto",
    }
    filtered = filter_payload(CAP_MULTI_AGENT, payload)
    assert "tools" not in filtered
    assert "tool_choice" not in filtered


def test_multi_agent_build_payload_forwards_agent_effort():
    payload = ConsoleInputBuilder.build_payload(
        console_model="grok-4.20-multi-agent-0309",
        caps=CAP_MULTI_AGENT,
        input_items=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        stream=False,
        reasoning_effort="high",
        reasoning_config={"effort": "high"},
    )
    assert payload["reasoning"] == {"effort": "high"}
    assert "summary" not in payload["reasoning"]


def test_multi_agent_filter_payload_keeps_effort():
    payload = {
        "model": "grok-4.20-multi-agent-0309",
        "reasoning": {"effort": "xhigh"},
    }
    filtered = filter_payload(CAP_MULTI_AGENT, payload)
    assert filtered["reasoning"] == {"effort": "xhigh"}


def test_grok43_filter_allows_xhigh_effort():
    payload = {"model": "grok-4.3", "reasoning": {"effort": "xhigh", "summary": "auto"}}
    filtered = filter_payload(CAP_GROK_43, payload)
    assert filtered["reasoning"]["effort"] == "xhigh"


def test_normalize_reasoning_effort_maps_aliases():
    assert normalize_reasoning_effort("max") == "xhigh"
    assert normalize_reasoning_effort("MAX") == "xhigh"
    assert normalize_reasoning_effort("minimal") == "low"
    assert normalize_reasoning_effort("MINIMAL") == "low"
    assert normalize_reasoning_effort("high") == "high"


def test_multi_agent_count_for_effort():
    from grok2api.services.grok.services.console_capabilities import multi_agent_count_for_effort

    assert multi_agent_count_for_effort(None) is None
    assert multi_agent_count_for_effort("none") is None
    assert multi_agent_count_for_effort("low") == 4
    assert multi_agent_count_for_effort("minimal") == 4
    assert multi_agent_count_for_effort("medium") == 8
    assert multi_agent_count_for_effort("high") == 12
    assert multi_agent_count_for_effort("max") == 16
    assert multi_agent_count_for_effort("xhigh") == 16


def test_grok43_filter_maps_minimal_to_low():
    payload = {"model": "grok-4.3", "reasoning": {"effort": "minimal", "summary": "auto"}}
    filtered = filter_payload(CAP_GROK_43, payload)
    assert filtered["reasoning"]["effort"] == "low"


def test_multi_agent_build_payload_normalizes_minimal():
    payload = ConsoleInputBuilder.build_payload(
        console_model="grok-4.20-multi-agent-0309",
        caps=CAP_MULTI_AGENT,
        input_items=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        stream=False,
        reasoning_effort="minimal",
        reasoning_config={"effort": "minimal"},
    )
    filtered = filter_payload(CAP_MULTI_AGENT, payload)
    assert filtered["reasoning"] == {"effort": "low"}


def test_multi_agent_filter_payload_maps_max_to_xhigh():
    payload = {"model": "grok-4.20-multi-agent-0309", "reasoning": {"effort": "max"}}
    filtered = filter_payload(CAP_MULTI_AGENT, payload)
    assert filtered["reasoning"] == {"effort": "xhigh"}


def test_grok43_filter_maps_max_to_xhigh():
    payload = {"model": "grok-4.3", "reasoning": {"effort": "max", "summary": "auto"}}
    filtered = filter_payload(CAP_GROK_43, payload)
    assert filtered["reasoning"]["effort"] == "xhigh"


def test_build_and_non_reasoning_models_strip_effort():
    from grok2api.services.grok.services.console_capabilities import CAP_420_NON_REASONING

    payload = ConsoleInputBuilder.build_payload(
        console_model="grok-4.20-0309-non-reasoning",
        caps=CAP_420_NON_REASONING,
        input_items=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        stream=False,
        reasoning_effort="high",
        reasoning_config={"effort": "high"},
    )
    filtered = filter_payload(CAP_420_NON_REASONING, payload)
    assert "reasoning" not in filtered


def test_grok_43_keeps_native_function_tools():
    tools = [{"type": "function", "name": "x", "parameters": {}}]
    upstream_tools, instructions, prompt_tools, upstream_tool_choice = prepare_console_tooling(
        caps=CAP_GROK_43,
        client_tools=tools,
        console_search=False,
        tool_choice="auto",
        parallel_tool_calls=True,
        instructions=None,
    )
    assert upstream_tools == tools
    assert prompt_tools is None
    assert instructions is None
    assert upstream_tool_choice == "auto"


def test_multi_agent_search_does_not_forward_function_tool_choice():
    from grok2api.services.grok.services.console_input import ConsoleInputBuilder

    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    upstream_tools, instructions, prompt_tools, upstream_tool_choice = prepare_console_tooling(
        caps=CAP_MULTI_AGENT,
        client_tools=tools,
        console_search=True,
        tool_choice="required",
        parallel_tool_calls=True,
        instructions=None,
    )
    payload = ConsoleInputBuilder.build_payload(
        console_model="grok-4.20-multi-agent-0309",
        caps=CAP_MULTI_AGENT,
        input_items=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        instructions=instructions,
        tools=upstream_tools,
        tool_choice=upstream_tool_choice if upstream_tools else None,
        stream=True,
    )
    assert prompt_tools == tools
    assert payload.get("tools") == [{"type": "web_search"}, {"type": "x_search"}]
    assert "tool_choice" not in payload


def test_chat_adapter_prompt_tool_call_stream():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    adapter = ConsoleChatStreamAdapter("grok-4.20-multi-agent-0309", prompt_tools=tools)
    event = ConsoleEvent(
        ConsoleEventType.TEXT_DELTA,
        {"delta": '<call>\nget_weather\n{"city":"Paris"}\n</call>'},
    )
    adapter.ingest(event)
    assert adapter.tool_calls
    response = adapter.build_non_stream_response()
    message = response["choices"][0]["message"]
    assert message.get("tool_calls")
    assert message["tool_calls"][0]["function"]["name"] == "get_weather"
    assert response["choices"][0]["finish_reason"] == "tool_calls"


def test_chat_adapter_prompt_tool_call_non_stream():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Ping",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    adapter = ConsoleChatStreamAdapter("grok-4.20-multi-agent-0309", prompt_tools=tools)
    adapter.content_parts.append('<call>\nping\n{}\n</call>')
    response = adapter.build_non_stream_response()
    message = response["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "ping"
    assert message.get("content") in (None, "")


def test_responses_prompt_tool_history_items_for_multi_agent_replay():
    items = prompt_tool_history_items(
        [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"q":"Paris"}',
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
        ]
    )
    assert items[0]["type"] == "message"
    assert "<call>\nlookup\n" in items[0]["content"][0]["text"]
    assert items[1]["role"] == "user"
    assert "tool (lookup, call_1): sunny" == items[1]["content"][0]["text"]


def test_anthropic_tool_history_items_for_multi_agent_replay():
    _, raw_items, _ = ConsoleInputBuilder.from_anthropic_raw_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "Paris"}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "sunny"}
                ],
            },
        ]
    )
    items = prompt_tool_history_items(raw_items)
    assert items[0]["type"] == "message"
    assert "<call>\nlookup\n" in items[0]["content"][0]["text"]
    assert items[1]["role"] == "user"
    assert items[1]["content"][0]["text"] == "tool (lookup, call_1): sunny"


def test_responses_prompt_tool_call_non_stream_transforms_output():
    import orjson

    tools = [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]
    adapter = ConsoleResponsesPassthroughAdapter(
        "grok-4.20-multi-agent-0309",
        prompt_tools=tools,
        tool_choice="required",
    )
    completed = adapter.ingest_raw_line(
        'data: {"type":"response.completed","response":{"id":"resp_1","output":['
        '{"type":"message","id":"msg_1","role":"assistant","content":['
        '{"type":"output_text","text":"<call>\\nlookup\\n{\\"q\\":\\"Paris\\"}\\n</call>"}]}]}}'
    )
    assert completed is not None
    response = adapter.completed_response["response"]
    assert response["model"] == "grok-4.20-multi-agent-0309"
    assert response["tool_choice"] == "required"
    assert response["tools"] == tools
    assert response["output"][0]["type"] == "function_call"
    assert response["output"][0]["name"] == "lookup"
    assert orjson.loads(response["output"][0]["arguments"]) == {"q": "Paris"}


def test_responses_prompt_tool_call_stream_transforms_split_delta():
    tools = [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]
    adapter = ConsoleResponsesPassthroughAdapter(
        "grok-4.20-multi-agent-0309-search",
        prompt_tools=tools,
    )
    assert adapter.ingest_raw_line(
        'data: {"type":"response.output_item.added","response_id":"resp_1",'
        '"output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","content":[]}}'
    ) is None
    assert adapter.ingest_raw_line(
        'data: {"type":"response.content_part.added","response_id":"resp_1",'
        '"item_id":"msg_1","output_index":0,"content_index":0,'
        '"part":{"type":"output_text","text":"","annotations":[]}}'
    ) is None
    assert adapter.ingest_raw_line(
        'data: {"type":"response.output_text.delta","response_id":"resp_1",'
        '"item_id":"msg_1","output_index":0,"content_index":0,"delta":"<ca"}'
    ) is None
    transformed = adapter.ingest_raw_line(
        'data: {"type":"response.output_text.delta","response_id":"resp_1",'
        '"item_id":"msg_1","output_index":0,"content_index":0,'
        '"delta":"ll>\\nlookup\\n{\\"q\\":\\"Paris\\"}\\n</call>"}'
    )
    assert transformed is not None
    assert "<call>" not in transformed
    assert '"type":"function_call"' in transformed
    assert '"type":"response.function_call_arguments.delta"' in transformed
    assert '"call_id":"' in transformed
    assert '"name":"lookup"' in transformed

    assert adapter.ingest_raw_line(
        'data: {"type":"response.output_text.done","response_id":"resp_1",'
        '"item_id":"msg_1","output_index":0,"content_index":0,'
        '"text":"<call>\\nlookup\\n{\\"q\\":\\"Paris\\"}\\n</call>"}'
    ) is None
    completed = adapter.ingest_raw_line(
        'data: {"type":"response.completed","response":{"id":"resp_1","output":['
        '{"type":"message","id":"msg_1","role":"assistant","content":['
        '{"type":"output_text","text":"<call>\\nlookup\\n{\\"q\\":\\"Paris\\"}\\n</call>"}]}]}}'
    )
    assert completed is not None
    output = adapter.completed_response["response"]["output"]
    assert output[0]["type"] == "function_call"
    assert output[0]["call_id"] in transformed


def test_responses_prompt_tool_call_stream_passes_normal_text():
    tools = [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]
    adapter = ConsoleResponsesPassthroughAdapter(
        "grok-4.20-multi-agent-0309",
        prompt_tools=tools,
    )
    assert adapter.ingest_raw_line(
        'data: {"type":"response.output_item.added","response_id":"resp_1",'
        '"output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","content":[]}}'
    ) is None
    passed = adapter.ingest_raw_line(
        'data: {"type":"response.output_text.delta","response_id":"resp_1",'
        '"item_id":"msg_1","output_index":0,"content_index":0,"delta":"Hello"}'
    )
    assert passed is not None
    assert '"type":"response.output_item.added"' in passed
    assert '"delta":"Hello"' in passed
    assert '"type":"function_call"' not in passed
