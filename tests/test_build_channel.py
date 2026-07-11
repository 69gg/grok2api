"""Tests for free CLI Build channel (models, payload, pool preference)."""

from __future__ import annotations

import time

from grok2api.services.grok.services.build_channel import (
    inject_web_search_tools,
    is_free_usage_exhausted,
    prepare_cli_payload,
)
from grok2api.services.grok.services.model import Channel, ModelService
from grok2api.core.exceptions import UpstreamException
from grok2api.services.token.models import TokenInfo, TokenStatus
from grok2api.services.token.pool import TokenPool
from grok2api.services.grok.services.build_auth import token_cli_selectable, token_has_cli_auth


def test_cli_models_registered_and_priority() -> None:
    ids = {
        "grok-4.5",
        "grok-4.5-search",
        "grok-composer-2.5-fast",
        "grok-composer-2.5-fast-search",
    }
    for mid in ids:
        m = ModelService.get(mid)
        assert m is not None
        assert m.channel == Channel.CLI
        assert m.owned_by == "xai-cli<grok2api@69gg>"
        assert ModelService.pool_for_model(mid) == "oidcBuild"
        assert ModelService.pool_candidates_for_model(mid) == ["oidcBuild"]
        assert ModelService.is_cli(mid)

    listed = [m.model_id for m in ModelService.list()]
    # CLI models appear before console models
    assert listed.index("grok-4.5") < listed.index("grok-4.3")


def test_search_alias_injects_web_search() -> None:
    payload = prepare_cli_payload("grok-4.5-search", {"model": "x", "input": "hi"})
    assert payload["model"] == "grok-4.5"
    tools = payload.get("tools") or []
    assert any(isinstance(t, dict) and t.get("type") == "web_search" for t in tools)

    payload2 = inject_web_search_tools({"tools": [{"type": "web_search"}]})
    assert len(payload2["tools"]) == 1


def test_free_usage_exhausted_detection() -> None:
    exc = UpstreamException(
        message="rate",
        details={
            "status": 429,
            "body": '{"code":"subscription:free-usage-exhausted","error":"included free usage"}',
        },
    )
    assert is_free_usage_exhausted(exc)


def test_pool_prefers_accounts_with_cli_auth() -> None:
    pool = TokenPool("oidcBuild")
    bare = TokenInfo(token="bare", status=TokenStatus.ACTIVE, quota=10)
    with_auth = TokenInfo(
        token="auth1",
        status=TokenStatus.ACTIVE,
        quota=10,
        access_token="access",
        refresh_token="refresh",
    )
    pool.add(bare)
    pool.add(with_auth)
    picked = pool.select_round_robin(require_cli_auth=True)
    assert picked is not None
    assert picked.token == "auth1"
    assert token_has_cli_auth(with_auth)
    assert not token_has_cli_auth(bare)
    assert token_cli_selectable(with_auth)


def test_staged_drop_encrypted_then_reasoning_content() -> None:
    from grok2api.services.grok.services.build_channel import (
        drop_encrypted_content,
        drop_reasoning_content,
        payload_has_encrypted_content,
        payload_has_reasoning_content,
        _body_drop_encrypted_content,
        _body_drop_reasoning_content,
    )
    import orjson

    payload = {
        "model": "grok-4.5",
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            {
                "type": "reasoning",
                "content": [{"type": "summary_text", "text": "think"}],
                "encrypted_content": "abc123",
            },
        ],
        "reasoning": {"effort": "low", "content": "x"},
    }
    assert payload_has_encrypted_content(payload)
    assert payload_has_reasoning_content(payload)

    # Stage 1: drop encrypted only
    stage1 = drop_encrypted_content(payload)
    assert not payload_has_encrypted_content(stage1)
    assert payload_has_reasoning_content(stage1)
    reasoning_item = next(i for i in stage1["input"] if i.get("type") == "reasoning")
    assert reasoning_item.get("content") is not None
    assert "encrypted_content" not in reasoning_item

    # Stage 2: drop reasoning.content
    stage2 = drop_reasoning_content(stage1)
    assert not payload_has_reasoning_content(stage2)
    assert all(i.get("type") != "reasoning" for i in stage2["input"])
    assert "content" not in (stage2.get("reasoning") or {})

    body1, changed1 = _body_drop_encrypted_content(orjson.dumps(payload))
    assert changed1 and body1 is not None
    body2, changed2 = _body_drop_reasoning_content(body1)
    assert changed2 and body2 is not None
    assert not payload_has_encrypted_content(orjson.loads(body2))
    assert not payload_has_reasoning_content(orjson.loads(body2))


def test_sanitize_cli_responses_payload() -> None:
    from grok2api.services.grok.services.build_channel import sanitize_cli_responses_payload

    payload = {
        "model": "grok-4.5",
        "input": [
            {
                "type": "reasoning",
                "content": None,
                "encrypted_content": None,
            },
            {
                "type": "function_call",
                "name": "end",
                "call_id": "c1",
                "arguments": "{}",
                "namespace": None,
                "id": None,
            },
        ],
        "tools": [],
        "tool_choice": "auto",
        "reasoning": {"effort": None},
    }
    out = sanitize_cli_responses_payload(payload)
    reasoning = out["input"][0]
    assert "content" not in reasoning
    assert "encrypted_content" not in reasoning
    fc = out["input"][1]
    assert "namespace" not in fc
    assert "id" not in fc
    assert "tools" not in out
    assert "tool_choice" not in out
    assert "reasoning" not in out


def test_device_consent_urls() -> None:
    from grok2api.services.reverse.build_device_consent import (
        DEVICE_APPROVE_URL,
        DEVICE_VERIFY_URL,
    )

    assert DEVICE_VERIFY_URL.endswith("/oauth2/device/verify")
    assert DEVICE_APPROVE_URL.endswith("/oauth2/device/approve")


def test_permanent_device_consent_errors() -> None:
    from grok2api.services.reverse.build_device_consent import (
        is_permanent_device_consent_error,
    )

    assert is_permanent_device_consent_error(
        RuntimeError("device approve failed HTTP 400: Invalid or expired code")
    )
    assert is_permanent_device_consent_error(
        RuntimeError("SSO cookie rejected (redirected to sign-in)")
    )
    assert is_permanent_device_consent_error(
        RuntimeError("device verify requires login (SSO invalid)")
    )
    assert not is_permanent_device_consent_error(
        RuntimeError("Connection reset by peer")
    )
    assert not is_permanent_device_consent_error(RuntimeError("HTTP 429 rate limited"))


def test_free_usage_cooling_not_selectable() -> None:
    info = TokenInfo(
        token="cool",
        status=TokenStatus.ACTIVE,
        quota=10,
        access_token="a",
        refresh_token="r",
        last_fail_reason="free-usage-exhausted",
        last_sync_at=int(time.time() * 1000) + 3600_000,
    )
    assert not token_cli_selectable(info)
