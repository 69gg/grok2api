from __future__ import annotations

import pytest

from grok2api.services.grok.utils.retry import pick_token_round_robin
from grok2api.services.token.manager import TokenManager
from grok2api.services.token.models import TokenInfo, TokenStatus
from grok2api.services.token.pool import TokenPool
from grok2api.services.token import pool as token_pool_module


def _token(value: str, *, quota: int = 10, tags: list[str] | None = None) -> TokenInfo:
    return TokenInfo(token=value, quota=quota, tags=tags or [])


@pytest.fixture(autouse=True)
def _default_token_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get_config(key: str, default: object = None) -> object:
        if key == "token.consumed_mode_enabled":
            return False
        return default

    monkeypatch.setattr(token_pool_module, "get_config", fake_get_config)


def test_token_pool_round_robin_advances_each_selection() -> None:
    pool = TokenPool("ssoBasic")
    pool.add(_token("token-a"))
    pool.add(_token("token-b"))
    pool.add(_token("token-c"))

    assert pool.select_round_robin().token == "token-a"
    assert pool.select_round_robin().token == "token-b"
    assert pool.select_round_robin().token == "token-c"
    assert pool.select_round_robin().token == "token-a"


def test_token_pool_round_robin_skips_excluded_and_unavailable_tokens() -> None:
    pool = TokenPool("ssoBasic")
    pool.add(_token("token-a"))
    pool.add(_token("token-b"))
    cooling = _token("token-c")
    cooling.status = TokenStatus.COOLING
    pool.add(cooling)

    assert pool.select_round_robin().token == "token-a"
    assert pool.select_round_robin(exclude={"token-b"}).token == "token-a"
    assert pool.select_round_robin().token == "token-b"


def test_token_pool_round_robin_respects_preferred_tags() -> None:
    pool = TokenPool("ssoBasic")
    pool.add(_token("token-a", tags=["default"]))
    pool.add(_token("token-b", tags=["console"]))
    pool.add(_token("token-c", tags=["console"]))

    assert pool.select_round_robin(prefer_tags={"console"}).token == "token-b"
    assert pool.select_round_robin(prefer_tags={"console"}).token == "token-c"
    assert pool.select_round_robin(prefer_tags={"console"}).token == "token-b"


@pytest.mark.asyncio
async def test_console_token_picker_uses_round_robin_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TokenManager()
    manager.initialized = True
    manager.pools["ssoBasic"] = TokenPool("ssoBasic")
    manager.pools["ssoBasic"].add(_token("token-a"))
    manager.pools["ssoBasic"].add(_token("token-b"))

    async def fake_refresh() -> None:
        raise AssertionError("refresh should not be called while tokens are available")

    monkeypatch.setattr(manager, "refresh_cooling_tokens_on_demand", fake_refresh)

    assert await pick_token_round_robin(manager, "grok-4.3", set()) == "token-a"
    assert await pick_token_round_robin(manager, "grok-4.3", set()) == "token-b"
    assert await pick_token_round_robin(manager, "grok-4.3", set()) == "token-a"


@pytest.mark.asyncio
async def test_console_token_picker_round_robin_skips_tried_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TokenManager()
    manager.initialized = True
    manager.pools["ssoBasic"] = TokenPool("ssoBasic")
    manager.pools["ssoBasic"].add(_token("token-a"))
    manager.pools["ssoBasic"].add(_token("token-b"))

    async def fake_refresh() -> None:
        raise AssertionError("refresh should not be called while tokens are available")

    monkeypatch.setattr(manager, "refresh_cooling_tokens_on_demand", fake_refresh)

    first = await pick_token_round_robin(manager, "grok-4.3", set())
    second = await pick_token_round_robin(manager, "grok-4.3", {first})

    assert first == "token-a"
    assert second == "token-b"
