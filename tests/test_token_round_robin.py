from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from grok2api.core.exceptions import UpstreamException
from grok2api.services.grok.services.chat import ChatService
from grok2api.services.grok.services.console_channel import ConsoleChannelService
from grok2api.services.grok.utils.retry import pick_token, pick_token_round_robin
from grok2api.services.token.manager import TokenManager
from grok2api.services.token.models import TokenInfo, TokenStatus
from grok2api.services.token.pool import TokenPool
from grok2api.services.token import pool as token_pool_module


def _token(value: str, *, quota: int = 10, tags: list[str] | None = None) -> TokenInfo:
    return TokenInfo(token=value, quota=quota, tags=tags or [])


def _first_token(tokens: Sequence[TokenInfo]) -> TokenInfo:
    return tokens[0]


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
async def test_token_picker_uses_round_robin_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TokenManager()
    manager.initialized = True
    manager.pools["ssoBasic"] = TokenPool("ssoBasic")
    manager.pools["ssoBasic"].add(_token("token-a"))
    manager.pools["ssoBasic"].add(_token("token-b"))

    async def fake_refresh() -> None:
        raise AssertionError("refresh should not be called while tokens are available")

    monkeypatch.setattr(token_pool_module.random, "choice", _first_token)
    monkeypatch.setattr(manager, "refresh_cooling_tokens_on_demand", fake_refresh)

    assert await pick_token(manager, "grok-4.3", set()) == "token-a"
    assert await pick_token(manager, "grok-4.3", set()) == "token-b"
    assert await pick_token(manager, "grok-4.3", set()) == "token-a"


@pytest.mark.asyncio
async def test_token_picker_round_robin_skips_tried_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TokenManager()
    manager.initialized = True
    manager.pools["ssoBasic"] = TokenPool("ssoBasic")
    manager.pools["ssoBasic"].add(_token("token-a"))
    manager.pools["ssoBasic"].add(_token("token-b"))

    async def fake_refresh() -> None:
        raise AssertionError("refresh should not be called while tokens are available")

    monkeypatch.setattr(token_pool_module.random, "choice", _first_token)
    monkeypatch.setattr(manager, "refresh_cooling_tokens_on_demand", fake_refresh)

    first = await pick_token(manager, "grok-4.3", set())
    second = await pick_token(manager, "grok-4.3", {first})

    assert first == "token-a"
    assert second == "token-b"


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
async def test_console_execute_retries_with_next_token_after_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TokenManager()
    manager.initialized = True
    manager.pools["ssoBasic"] = TokenPool("ssoBasic")
    manager.pools["ssoBasic"].add(_token("token-a"))
    manager.pools["ssoBasic"].add(_token("token-b"))
    used_tokens: list[str] = []

    async def fake_get_token_manager() -> TokenManager:
        return manager

    async def fake_reload_if_stale() -> None:
        return None

    async def fake_stream_upstream(
        payload: dict[str, str],
        *,
        token: str,
    ) -> AsyncIterator[str]:
        used_tokens.append(token)
        if token == "token-a":
            raise UpstreamException(
                "rate limited",
                status_code=429,
                details={"status": 429},
            )
        yield f"ok:{payload['token']}"

    async def fake_handle_failure(
        token_mgr: TokenManager,
        token: str,
        exc: UpstreamException,
    ) -> None:
        return None

    def fake_get_config(key: str, default: object = None) -> object:
        if key == "retry.max_retry":
            return 2
        return default

    async def build_payload(token: str) -> dict[str, str]:
        return {"token": token}

    monkeypatch.setattr(
        "grok2api.services.grok.services.console_channel.get_token_manager",
        fake_get_token_manager,
    )
    monkeypatch.setattr(manager, "reload_if_stale", fake_reload_if_stale)
    monkeypatch.setattr(
        "grok2api.services.grok.services.console_channel.get_config",
        fake_get_config,
    )
    monkeypatch.setattr(
        ConsoleChannelService,
        "_stream_upstream",
        fake_stream_upstream,
    )
    monkeypatch.setattr(
        ConsoleChannelService,
        "_handle_token_upstream_failure",
        fake_handle_failure,
    )

    result = await ConsoleChannelService._execute_with_token(
        "grok-4.3",
        build_payload,
        stream=False,
    )

    assert used_tokens == ["token-a", "token-b"]
    assert result == ["ok:token-b"]


@pytest.mark.asyncio
async def test_app_chat_transient_error_uses_next_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TokenManager()
    manager.initialized = True
    manager.pools["ssoBasic"] = TokenPool("ssoBasic")
    manager.pools["ssoBasic"].add(_token("token-a"))
    manager.pools["ssoBasic"].add(_token("token-b"))
    used_tokens: list[str] = []

    async def fake_get_token_manager() -> TokenManager:
        return manager

    async def fake_reload_if_stale() -> None:
        return None

    async def fake_chat_openai(
        self: object,
        token: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        used_tokens.append(token)
        raise UpstreamException(
            "curl: (92) HTTP/2 stream was not closed cleanly",
            status_code=502,
            details={
                "status": 502,
                "error": "curl: (92) HTTP/2 stream was not closed cleanly",
            },
        )

    def fake_get_config(key: str, default: object = None) -> object:
        values: dict[str, object] = {
            "retry.max_retry": 3,
            "app.stream": False,
        }
        return values.get(key, default)

    monkeypatch.setattr(
        "grok2api.services.grok.services.chat.get_token_manager",
        fake_get_token_manager,
    )
    monkeypatch.setattr(
        "grok2api.services.grok.services.chat.get_config",
        fake_get_config,
    )
    monkeypatch.setattr(manager, "reload_if_stale", fake_reload_if_stale)
    monkeypatch.setattr(
        "grok2api.services.grok.services.chat.GrokChatService.chat_openai",
        fake_chat_openai,
    )

    with pytest.raises(UpstreamException):
        await ChatService.completions(
            model="grok-4.3-fast",
            messages=[{"role": "user", "content": "hello"}],
            stream=False,
        )

    assert used_tokens == ["token-a", "token-b"]
