"""Unit tests for console channel helpers."""

from __future__ import annotations

from grok2api.services.grok.services.console_channel import _response_format_to_text_format
from grok2api.services.grok.services.console_capabilities import (
    CAP_GROK_43,
    merge_tools,
    should_include_encrypted,
)
from grok2api.services.grok.services.console_input import (
    ConsoleInputBuilder,
    is_encrypted_reasoning,
)
from grok2api.services.grok.services.console_output_adapters import ConsoleChatStreamAdapter
from grok2api.services.grok.services.console_stream_parser import (
    ConsoleEventType,
    ConsoleStreamParser,
)
from grok2api.services.grok.services.model import CONSOLE_MODEL_IDS, ModelService
from grok2api.services.grok.utils.usage import from_upstream_responses_usage


def test_console_models_registered_and_listed():
    for model_id in CONSOLE_MODEL_IDS:
        assert ModelService.valid(model_id)
        info = ModelService.get(model_id)
        assert info is not None
        assert info.owned_by == "xai-console"


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
