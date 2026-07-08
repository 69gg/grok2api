from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from grok2api.main import create_app
from grok2api.services.reverse.console_native import (
    ConsoleNativeResponse,
    sanitize_console_response_headers,
)


def _json_response(body: bytes = b'{"data":[{"b64_json":"abc"}]}') -> ConsoleNativeResponse:
    return ConsoleNativeResponse(
        body=body,
        status_code=200,
        content_type="application/json",
        headers={},
    )


def test_console_image_generation_uses_native_b64_json() -> None:
    app = create_app()
    client = TestClient(app)
    native = AsyncMock(return_value=_json_response())

    with patch("grok2api.api.v1.image.ConsoleNativeService.json_request", new=native):
        response = client.post(
            "/v1/images/generations",
            json={"model": "grok-imagine-image", "prompt": "red square"},
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["b64_json"] == "abc"
    call = native.await_args.kwargs
    assert call["model_id"] == "grok-imagine-image"
    assert call["path"] == "/v1/images/generations"
    assert call["payload"]["response_format"] == "b64_json"


def test_console_image_edit_multipart_converts_to_native_json_data_uri() -> None:
    app = create_app()
    client = TestClient(app)
    native = AsyncMock(return_value=_json_response())
    image_bytes = b"\xff\xd8\xff"

    with patch("grok2api.api.v1.image.ConsoleNativeService.json_request", new=native):
        response = client.post(
            "/v1/images/edits",
            data={"model": "grok-imagine-image", "prompt": "make it blue"},
            files={"image": ("input.jpg", image_bytes, "image/jpeg")},
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200
    call = native.await_args.kwargs
    assert call["path"] == "/v1/images/edits"
    payload = call["payload"]
    assert payload["model"] == "grok-imagine-image"
    assert payload["response_format"] == "b64_json"
    expected = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
    assert payload["image"]["url"] == expected


def test_console_responses_route_uses_native_passthrough() -> None:
    app = create_app()
    client = TestClient(app)
    native = AsyncMock(return_value=_json_response(b'{"id":"resp_1"}'))

    with patch("grok2api.api.v1.response.ConsoleNativeService.json_request", new=native):
        response = client.post(
            "/v1/responses",
            json={"model": "grok-4.3", "input": "hello", "stream": False},
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200
    assert response.json() == {"id": "resp_1"}
    call = native.await_args.kwargs
    assert call["path"] == "/v1/responses"
    assert call["payload"]["input"] == "hello"


def test_console_chat_route_uses_native_passthrough() -> None:
    app = create_app()
    client = TestClient(app)
    native = AsyncMock(return_value=_json_response(b'{"id":"chatcmpl_1"}'))

    with patch("grok2api.api.v1.chat.ConsoleNativeService.json_request", new=native):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200
    assert response.json() == {"id": "chatcmpl_1"}
    call = native.await_args.kwargs
    assert call["path"] == "/v1/chat/completions"
    assert call["payload"]["messages"][0]["content"] == "hello"


def test_console_native_response_headers_are_redacted() -> None:
    headers = sanitize_console_response_headers(
        {
            "content-type": "application/json",
            "set-cookie": "sso=secret; Path=/",
            "Authorization": "Bearer secret",
            "x-trace-id": "trace-1",
        }
    )

    assert headers["content-type"] == "application/json"
    assert headers["set-cookie"] == "<redacted>"
    assert headers["Authorization"] == "<redacted>"
    assert headers["x-trace-id"] == "trace-1"
