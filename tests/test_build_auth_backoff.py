"""Regression tests for background CLI OIDC failure backoff."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from grok2api.services.grok.services import build_auth as build_auth_module
from grok2api.services.token.models import TokenInfo, TokenStatus
from grok2api.services.token.pool import TokenPool


@pytest.fixture(autouse=True)
def _clear_background_backoff() -> Iterator[None]:
    build_auth_module.BuildAuthService._background_backoffs.clear()
    yield
    build_auth_module.BuildAuthService._background_backoffs.clear()


def test_background_failure_backoff_is_exponential_and_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]

    def fake_get_config(key: str, default: Any = None) -> Any:
        values = {
            "build.background_failure_backoff_initial_sec": 10,
            "build.background_failure_backoff_max_sec": 25,
        }
        return values.get(key, default)

    monkeypatch.setattr(build_auth_module, "get_config", fake_get_config)
    monkeypatch.setattr(build_auth_module.time, "monotonic", lambda: now[0])
    service = build_auth_module.BuildAuthService

    assert service._record_background_failure("test", "account") == 10
    assert service._background_backoff_remaining("test", "account") == 10

    now[0] += 11
    assert service._record_background_failure("test", "account") == 20

    now[0] += 21
    assert service._record_background_failure("test", "account") == 25

    service._clear_background_failure("test", "account")
    assert service._background_backoff_remaining("test", "account") == 0


@pytest.mark.asyncio
async def test_scheduled_refresh_limits_attempts_and_backs_off_full_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = TokenPool("oidcBuild")
    for index in range(5):
        pool.add(
            TokenInfo(
                token=f"account-{index}",
                status=TokenStatus.ACTIVE,
                quota=10,
                access_token=f"access-{index}",
                refresh_token=f"refresh-{index}",
                expired_at=1,
            )
        )

    class FakeManager:
        def __init__(self) -> None:
            self.pools: dict[str, TokenPool] = {"oidcBuild": pool}

        async def reload_if_stale(self) -> None:
            return None

    manager = FakeManager()
    attempted: list[str] = []

    async def fake_get_token_manager() -> FakeManager:
        return manager

    async def fail_refresh(
        token_info: TokenInfo,
        pool_name: str = "oidcBuild",
        *,
        force: bool = False,
    ) -> TokenInfo:
        del pool_name, force
        attempted.append(token_info.token)
        raise RuntimeError("TLS unavailable")

    def fake_get_config(key: str, default: Any = None) -> Any:
        values = {
            "build.background_failure_backoff_initial_sec": 300,
            "build.background_failure_backoff_max_sec": 3600,
        }
        return values.get(key, default)

    monkeypatch.setattr(build_auth_module, "get_token_manager", fake_get_token_manager)
    monkeypatch.setattr(build_auth_module, "get_config", fake_get_config)
    monkeypatch.setattr(
        build_auth_module.BuildAuthService,
        "refresh_token_info",
        staticmethod(fail_refresh),
    )

    refreshed = await build_auth_module.BuildAuthService.refresh_expiring_cli_tokens(max_tokens=2)
    assert refreshed == 0
    assert attempted == ["account-0", "account-1"]

    await build_auth_module.BuildAuthService.refresh_expiring_cli_tokens(max_tokens=2)
    assert attempted == ["account-0", "account-1"]


@pytest.mark.asyncio
async def test_auto_init_prioritizes_fresh_accounts_over_due_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pool = TokenPool("ssoBasic")
    for index in range(3):
        source_pool.add(
            TokenInfo(
                token=f"sso-{index}",
                status=TokenStatus.ACTIVE,
                quota=10,
            )
        )

    class FakeManager:
        def __init__(self) -> None:
            self.pools: dict[str, TokenPool] = {"ssoBasic": source_pool}

        async def reload_if_stale(self) -> None:
            return None

    manager = FakeManager()
    attempted: list[str] = []
    now = [100.0]

    async def fake_get_token_manager() -> FakeManager:
        return manager

    async def fake_ensure(
        cls: type[build_auth_module.BuildAuthService],
        sso_token: str,
        *,
        email: str = "",
        trigger: str = "request",
    ) -> TokenInfo:
        del cls, email, trigger
        attempted.append(sso_token)
        return TokenInfo(
            token=sso_token,
            status=TokenStatus.ACTIVE,
            quota=10,
            access_token="access",
            refresh_token="refresh",
        )

    def fake_get_config(key: str, default: Any = None) -> Any:
        values = {
            "build.background_failure_backoff_initial_sec": 10,
            "build.background_failure_backoff_max_sec": 60,
        }
        return values.get(key, default)

    monkeypatch.setattr(build_auth_module, "get_token_manager", fake_get_token_manager)
    monkeypatch.setattr(build_auth_module, "get_config", fake_get_config)
    monkeypatch.setattr(build_auth_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        build_auth_module.BuildAuthService,
        "ensure_oidc_for_sso",
        classmethod(fake_ensure),
    )

    service = build_auth_module.BuildAuthService
    service._record_background_failure(build_auth_module.BACKGROUND_INIT_SCOPE, "sso-0")
    now[0] += 11

    initialized = await service.init_missing_cli_oidc(max_tokens=1)

    assert initialized == 1
    assert attempted == ["sso-1"]
