from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from grok2api.core.exceptions import UpstreamException
from grok2api.main import create_app
from grok2api.services.grok.services.image import ImageGenerationResult
from grok2api.services.grok.services.image_edit import ImageEditResult
from grok2api.services.reverse.console_native import (
    ConsoleNativeResponse,
    build_console_body_log_preview,
    sanitize_console_request_headers,
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
    native_body = b'{ "data" : [ { "b64_json" : "abc" } ] }'
    native = AsyncMock(return_value=_json_response(native_body))

    with patch("grok2api.api.v1.image.ConsoleNativeService.json_request", new=native):
        response = client.post(
            "/v1/images/generations",
            json={"model": "grok-imagine-image", "prompt": "red square"},
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200
    assert response.content == native_body
    assert response.json()["data"][0]["b64_json"] == "abc"
    call = native.await_args.kwargs
    assert call["model_id"] == "grok-imagine-image"
    assert call["path"] == "/v1/images/generations"
    assert call["payload"]["response_format"] == "b64_json"
    assert call["payload"]["quality"] == "medium"


def test_console_image_generation_maps_openai_quality_to_console() -> None:
    app = create_app()
    client = TestClient(app)
    native = AsyncMock(return_value=_json_response())

    with patch("grok2api.api.v1.image.ConsoleNativeService.json_request", new=native):
        response = client.post(
            "/v1/images/generations",
            json={
                "model": "grok-imagine-image",
                "prompt": "red square",
                "quality": "hd",
            },
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200
    call = native.await_args.kwargs
    assert call["payload"]["quality"] == "high"


def test_console_image_generation_rejects_unknown_quality_before_upstream() -> None:
    app = create_app()
    client = TestClient(app)
    native = AsyncMock(return_value=_json_response())

    with patch("grok2api.api.v1.image.ConsoleNativeService.json_request", new=native):
        response = client.post(
            "/v1/images/generations",
            json={
                "model": "grok-imagine-image",
                "prompt": "red square",
                "quality": "ultra",
            },
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "quality"
    assert native.await_count == 0


def test_console_image_edit_multipart_converts_to_native_json_data_uri() -> None:
    app = create_app()
    client = TestClient(app)
    native_body = b'{ "data" : [ { "b64_json" : "abc" } ] }'
    native = AsyncMock(return_value=_json_response(native_body))
    image_bytes = b"\xff\xd8\xff"

    with patch("grok2api.api.v1.image.ConsoleNativeService.json_request", new=native):
        response = client.post(
            "/v1/images/edits",
            data={"model": "grok-imagine-image", "prompt": "make it blue"},
            files={"image": ("input.jpg", image_bytes, "image/jpeg")},
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200
    assert response.content == native_body
    call = native.await_args.kwargs
    assert call["path"] == "/v1/images/edits"
    payload = call["payload"]
    assert payload["model"] == "grok-imagine-image"
    assert payload["response_format"] == "b64_json"
    expected = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
    assert payload["image"]["url"] == expected


def test_console_image_generation_failure_falls_back_to_legacy() -> None:
    app = create_app()
    client = TestClient(app)
    native = AsyncMock(
        side_effect=UpstreamException(
            "console failed",
            details={"status": 502},
            status_code=502,
        )
    )
    legacy = AsyncMock(
        return_value=ImageGenerationResult(
            stream=False,
            data=["legacy-b64"],
            usage_override=None,
        )
    )
    get_token = AsyncMock(return_value=(object(), "legacy-token"))

    with (
        patch("grok2api.api.v1.image.ConsoleNativeService.json_request", new=native),
        patch("grok2api.api.v1.image.ImageGenerationService.generate", new=legacy),
        patch("grok2api.api.v1.image._get_token", new=get_token),
    ):
        response = client.post(
            "/v1/images/generations",
            json={
                "model": "grok-imagine-image",
                "prompt": "red square",
                "response_format": "b64_json",
            },
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["b64_json"] == "legacy-b64"
    assert legacy.await_count == 1


def test_console_image_edit_failure_falls_back_to_legacy() -> None:
    app = create_app()
    client = TestClient(app)
    native = AsyncMock(
        side_effect=UpstreamException(
            "console failed",
            details={"status": 502},
            status_code=502,
        )
    )
    legacy = AsyncMock(
        return_value=ImageEditResult(
            stream=False,
            data=["legacy-edit-b64"],
        )
    )
    get_token = AsyncMock(return_value=(object(), "legacy-token"))

    with (
        patch("grok2api.api.v1.image.ConsoleNativeService.json_request", new=native),
        patch("grok2api.api.v1.image.ImageEditService.edit", new=legacy),
        patch("grok2api.api.v1.image._get_token", new=get_token),
    ):
        response = client.post(
            "/v1/images/edits",
            data={
                "model": "grok-imagine-image",
                "prompt": "make it blue",
                "response_format": "b64_json",
            },
            files={"image": ("input.jpg", b"\xff\xd8\xff", "image/jpeg")},
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["b64_json"] == "legacy-edit-b64"
    assert legacy.await_count == 1


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


def test_console_native_request_headers_are_redacted() -> None:
    headers = sanitize_console_request_headers(
        {
            "content-type": "application/json",
            "Cookie": "sso=secret",
            "Authorization": "Bearer secret",
            "x-session-token": "secret",
            "x-request-id": "req-1",
        }
    )

    assert headers["content-type"] == "application/json"
    assert headers["Cookie"] == "<redacted>"
    assert headers["Authorization"] == "<redacted>"
    assert headers["x-session-token"] == "<redacted>"
    assert headers["x-request-id"] == "req-1"


def test_console_native_body_log_preview_redacts_sensitive_json_values() -> None:
    body = (
        b'{"model":"grok-4.3","token":"secret-token","messages":[{"role":"user",'
        b'"content":"hello"}],"metadata":{"refresh_token":"refresh-secret",'
        b'"note":"visible"}}'
    )

    preview = build_console_body_log_preview(body, "application/json")

    assert "grok-4.3" in preview
    assert "visible" in preview
    assert "hello" in preview
    assert "secret-token" not in preview
    assert "refresh-secret" not in preview
    assert preview.count("<redacted>") == 2


def test_console_native_body_log_preview_truncates_large_strings() -> None:
    body = b'{"image":"data:image/png;base64,' + (b"a" * 5000) + b'"}'

    preview = build_console_body_log_preview(body, "application/json")

    assert "data:image/png;base64," in preview
    assert "<truncated" in preview
    assert len(preview) < 1300
