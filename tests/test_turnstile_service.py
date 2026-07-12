"""Tests for TurnstileService local solver debug helpers and poll handling."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from grok2api.services.register.services.turnstile_service import (
    TurnstileService,
    _summarize_solver_payload,
    _token_preview,
)


def test_token_preview_redacts_long_tokens() -> None:
    token = "a" * 40 + "zzzz"
    preview = _token_preview(token)
    assert "zzzz" not in preview or preview.endswith("(len=44)")
    assert "..." in preview
    assert "len=44" in preview
    assert _token_preview("CAPTCHA_FAIL") == "CAPTCHA_FAIL"
    assert _token_preview(None) == "<empty>"


def test_summarize_solver_payload() -> None:
    summary = _summarize_solver_payload(
        {
            "status": "processing",
            "errorId": 0,
            "solution": {"token": "tok1234567890"},
        }
    )
    assert "status='processing'" in summary
    assert "errorId=0" in summary
    assert "token=" in summary


def test_local_create_task_success() -> None:
    service = TurnstileService(solver_url="http://127.0.0.1:5072", yescaptcha_key="")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"errorId":0,"taskId":"abc-1"}'
    mock_resp.json.return_value = {"errorId": 0, "taskId": "abc-1"}
    mock_resp.raise_for_status = MagicMock()

    with patch(
        "grok2api.services.register.services.turnstile_service.requests.get",
        return_value=mock_resp,
    ) as get_mock:
        task_id = service.create_task("https://accounts.x.ai/sign-up", "0x4AAAAAAAhr9JGVDZbrZOo0")

    assert task_id == "abc-1"
    get_mock.assert_called_once()
    args, kwargs = get_mock.call_args
    assert args[0].endswith("/turnstile")
    assert kwargs["params"]["url"] == "https://accounts.x.ai/sign-up"


def test_local_get_response_fail_surfaces_error_description() -> None:
    service = TurnstileService(solver_url="http://127.0.0.1:5072", yescaptcha_key="")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "errorId": 1,
        "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
        "errorDescription": "timeout waiting for cf-turnstile-response token",
        "debug": {"page": {"url": "https://accounts.x.ai/sign-up"}},
    }
    mock_resp.raise_for_status = MagicMock()

    with patch(
        "grok2api.services.register.services.turnstile_service.requests.get",
        return_value=mock_resp,
    ):
        token = service.get_response("task-1", max_retries=1, initial_delay=0, retry_delay=0)

    assert token is None
    assert service.last_error == "timeout waiting for cf-turnstile-response token"


def test_local_get_response_ready_token() -> None:
    service = TurnstileService(solver_url="http://127.0.0.1:5072", yescaptcha_key="")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "errorId": 0,
        "status": "ready",
        "solution": {"token": "cf-token-value-xyz"},
    }
    mock_resp.raise_for_status = MagicMock()

    with patch(
        "grok2api.services.register.services.turnstile_service.requests.get",
        return_value=mock_resp,
    ):
        token = service.get_response("task-2", max_retries=1, initial_delay=0, retry_delay=0)

    assert token == "cf-token-value-xyz"
