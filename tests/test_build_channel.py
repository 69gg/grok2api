"""Tests for free CLI Build channel (models, payload, pool preference)."""

from __future__ import annotations

import time
from typing import Any

import pytest

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

    spending = UpstreamException(
        message="BuildNativeReverse: request failed, 403",
        details={
            "status": 403,
            "body": (
                '{"code":"personal-team-blocked:spending-limit",'
                '"error":"You have run out of credits or need a Grok subscription. '
                'Add credits at https://grok.com/?_s=usage or upgrade at https://grok.com/supergrok."}'
            ),
        },
    )
    assert is_free_usage_exhausted(spending)

    other_403 = UpstreamException(
        message="forbidden",
        details={"status": 403, "body": '{"code":"unauthorized:blocked-user"}'},
    )
    assert not is_free_usage_exhausted(other_403)


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


def test_oidc_refresh_revoked_detection() -> None:
    from grok2api.services.grok.services.build_auth import (
        is_oidc_refresh_revoked_error,
    )
    from grok2api.services.reverse.build_oauth import OAuthRefreshError

    assert is_oidc_refresh_revoked_error(
        OAuthRefreshError(
            "refresh failed HTTP 400: {'error': 'invalid_grant', "
            "'error_description': 'Refresh token has been revoked'}"
        )
    )
    assert is_oidc_refresh_revoked_error(
        OAuthRefreshError("invalid_grant: token revoked")
    )
    assert not is_oidc_refresh_revoked_error(
        OAuthRefreshError("refresh failed HTTP 503: upstream")
    )


@pytest.mark.asyncio
async def test_refresh_revoked_triggers_remint(monkeypatch: pytest.MonkeyPatch) -> None:
    from grok2api.services.grok.services import build_auth as ba
    from grok2api.services.reverse.build_oauth import OAuthRefreshError, TokenBundle

    info = TokenInfo(
        token="acct-1",
        status=TokenStatus.ACTIVE,
        quota=10,
        access_token="old-access",
        refresh_token="dead-refresh",
        expired_at=1,
        sso_source="sso-jwt-value",
        email="a@b.c",
    )
    reminted = TokenInfo(
        token="acct-1",
        status=TokenStatus.ACTIVE,
        quota=10,
        access_token="new-access",
        refresh_token="new-refresh",
        expired_at=int(time.time() * 1000) + 3600_000,
        sso_source="sso-jwt-value",
        email="a@b.c",
    )
    calls: dict[str, Any] = {"remint": 0}

    async def boom_refresh(*_a: object, **_k: object) -> TokenBundle:
        raise OAuthRefreshError(
            "refresh failed HTTP 400: {'error': 'invalid_grant', "
            "'error_description': 'Refresh token has been revoked'}"
        )

    async def fake_remint(
        cls: type,
        token_info: TokenInfo,
        pool_name: str = "oidcBuild",
        *,
        trigger: str = "refresh_revoked",
    ) -> TokenInfo:
        calls["remint"] += 1
        assert token_info.token == "acct-1"
        assert trigger == "refresh_revoked"
        return reminted

    monkeypatch.setattr(ba, "refresh_tokens_async", boom_refresh)
    monkeypatch.setattr(
        ba.BuildAuthService,
        "remint_oidc_for_token_info",
        classmethod(fake_remint),
    )

    out = await ba.BuildAuthService.refresh_token_info(info, force=True)
    assert calls["remint"] == 1
    assert out.access_token == "new-access"
    assert out.refresh_token == "new-refresh"


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

    cooling = TokenInfo(
        token="cool2",
        status=TokenStatus.COOLING,
        quota=10,
        access_token="a",
        refresh_token="r",
        last_fail_reason="free-usage-exhausted",
        last_sync_at=int(time.time() * 1000) + 3600_000,
    )
    assert not token_cli_selectable(cooling)

    recovered = TokenInfo(
        token="cool3",
        status=TokenStatus.COOLING,
        quota=10,
        access_token="a",
        refresh_token="r",
        last_fail_reason="free-usage-exhausted",
        last_sync_at=int(time.time() * 1000) - 1000,
    )
    assert token_cli_selectable(recovered)


@pytest.mark.asyncio
async def test_spending_limit_cools_and_retries_next_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 personal-team-blocked:spending-limit cools the account and switches."""
    from grok2api.services.grok.services import build_channel as bc
    from grok2api.services.reverse.build_native import BuildNativeResponse
    from grok2api.services.token.manager import TokenManager

    manager = TokenManager()
    manager.initialized = True
    manager.pools["oidcBuild"] = TokenPool("oidcBuild")
    tok_a = TokenInfo(
        token="token-a",
        status=TokenStatus.ACTIVE,
        quota=10,
        access_token="access-a",
        refresh_token="refresh-a",
        expired_at=int(time.time() * 1000) + 3600_000,
    )
    tok_b = TokenInfo(
        token="token-b",
        status=TokenStatus.ACTIVE,
        quota=10,
        access_token="access-b",
        refresh_token="refresh-b",
        expired_at=int(time.time() * 1000) + 3600_000,
    )
    manager.pools["oidcBuild"].add(tok_a)
    manager.pools["oidcBuild"].add(tok_b)

    used_access: list[str] = []
    fail_first = True

    async def fake_get_token_manager() -> TokenManager:
        return manager

    async def fake_reload_if_stale() -> None:
        return None

    async def fake_update_token_fields(
        pool_name: str, token: str, fields: dict[str, Any]
    ) -> None:
        info = manager.pools[pool_name].get(token)
        if info:
            for k, v in fields.items():
                if k == "status":
                    info.status = TokenStatus(v)
                else:
                    setattr(info, k, v)

    async def fake_request(
        *,
        access_token: str,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = None,
        stream: bool = False,
        conv_id: str | None = None,
        base_url: str | None = None,
    ) -> BuildNativeResponse:
        nonlocal fail_first
        used_access.append(access_token)
        if fail_first:
            fail_first = False
            raise UpstreamException(
                message="BuildNativeReverse: request failed, 403",
                details={
                    "status": 403,
                    "body": (
                        '{"code":"personal-team-blocked:spending-limit",'
                        '"error":"You have run out of credits or need a Grok subscription."}'
                    ),
                },
            )
        return BuildNativeResponse(
            body=b'{"ok":true}',
            status_code=200,
            content_type="application/json",
            headers={},
        )

    async def fake_ensure(
        token_info: TokenInfo, pool_name: str
    ) -> TokenInfo:
        return token_info

    def fake_get_config(key: str, default: object = None) -> object:
        if key == "retry.max_retry":
            return 3
        if key == "build.enabled":
            return True
        if key == "build.free_usage_cooldown_sec":
            return 86400
        if key == "build.default_web_search":
            return False
        return default

    monkeypatch.setattr(bc, "get_token_manager", fake_get_token_manager)
    monkeypatch.setattr(manager, "reload_if_stale", fake_reload_if_stale)
    monkeypatch.setattr(manager, "update_token_fields", fake_update_token_fields)
    monkeypatch.setattr(bc, "get_config", fake_get_config)
    monkeypatch.setattr(bc.BuildNativeReverse, "request", staticmethod(fake_request))
    monkeypatch.setattr(
        bc.BuildChannelService, "ensure_fresh_access", staticmethod(fake_ensure)
    )

    result = await bc.BuildChannelService.proxy(
        model_id="grok-4.5",
        method="POST",
        path="/v1/messages",
        payload={"model": "grok-4.5", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
    )

    # First account fails with spending-limit, second succeeds.
    assert len(used_access) == 2
    assert used_access[0] != used_access[1]
    assert isinstance(result, BuildNativeResponse)
    assert result.status_code == 200

    cooled = [t for t in (tok_a, tok_b) if t.status == TokenStatus.COOLING]
    active = [t for t in (tok_a, tok_b) if t.status == TokenStatus.ACTIVE]
    assert len(cooled) == 1
    assert cooled[0].last_fail_reason == "free-usage-exhausted"
    assert not token_cli_selectable(cooled[0])
    assert len(active) == 1
    assert token_cli_selectable(active[0])
