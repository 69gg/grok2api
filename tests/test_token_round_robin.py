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


def _token(
    value: str,
    *,
    quota: int = 10,
    tags: list[str] | None = None,
    console_team_id: str = "33ec95d2-5364-4c7f-b1b3-b5bff151adb0",
) -> TokenInfo:
    return TokenInfo(
        token=value,
        quota=quota,
        tags=tags or [],
        console_team_id=console_team_id,
    )


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


def test_token_info_console_team_fields_are_backward_compatible() -> None:
    token = TokenInfo(token="token-a")

    assert token.console_team_id == ""
    assert token.console_team_name == ""
    assert "console_team_id" in TokenInfo.model_fields
    assert "console_team_name" in TokenInfo.model_fields


def test_token_info_preserves_console_team_fields() -> None:
    token = TokenInfo(
        token="token-a",
        console_team_id="33ec95d2-5364-4c7f-b1b3-b5bff151adb0",
        console_team_name="Grok2API team token-a",
    )
    dumped = token.model_dump()

    assert dumped["console_team_id"] == "33ec95d2-5364-4c7f-b1b3-b5bff151adb0"
    assert dumped["console_team_name"] == "Grok2API team token-a"


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
    recorded: list[tuple[str, int, str, int | None]] = []

    async def fake_get_token_manager() -> TokenManager:
        return manager

    async def fake_reload_if_stale() -> None:
        return None

    async def fake_stream_upstream(
        payload: dict[str, str],
        *,
        token: str,
        team_id: str | None = None,
    ) -> AsyncIterator[str]:
        used_tokens.append(token)
        if token == "token-a":
            raise UpstreamException(
                "rate limited",
                status_code=429,
                details={"status": 429},
            )
        yield f"ok:{payload['token']}"

    async def fake_record_fail(
        token: str,
        status_code: int = 401,
        reason: str = "",
        threshold: int | None = None,
    ) -> bool:
        recorded.append((token, status_code, reason, threshold))
        return True

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
        "grok2api.services.grok.services.console_channel.TokenService.record_fail",
        fake_record_fail,
    )

    result = await ConsoleChannelService._execute_with_token(
        "grok-4.3",
        build_payload,
        stream=False,
    )

    assert used_tokens == ["token-a", "token-b"]
    assert recorded == []
    assert result == ["ok:token-b"]


@pytest.mark.asyncio
async def test_console_execute_records_blocked_user_and_uses_next_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TokenManager()
    manager.initialized = True
    manager.pools["ssoBasic"] = TokenPool("ssoBasic")
    manager.pools["ssoBasic"].add(_token("token-a"))
    manager.pools["ssoBasic"].add(_token("token-b"))
    used_tokens: list[str] = []
    recorded: list[tuple[str, int, str, int | None]] = []

    async def fake_get_token_manager() -> TokenManager:
        return manager

    async def fake_reload_if_stale() -> None:
        return None

    async def fake_stream_upstream(
        payload: dict[str, str],
        *,
        token: str,
        team_id: str | None = None,
    ) -> AsyncIterator[str]:
        used_tokens.append(token)
        if token == "token-a":
            raise UpstreamException(
                "forbidden",
                status_code=403,
                details={
                    "status": 403,
                    "body": '{"code":"unauthorized:blocked-user","error":"User is blocked"}',
                },
            )
        yield f"ok:{payload['token']}"

    def fake_get_config(key: str, default: object = None) -> object:
        if key == "retry.max_retry":
            return 2
        return default

    async def fake_record_fail(
        token: str,
        status_code: int = 401,
        reason: str = "",
        threshold: int | None = None,
    ) -> bool:
        recorded.append((token, status_code, reason, threshold))
        return True

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
        "grok2api.services.grok.services.console_channel.TokenService.record_fail",
        fake_record_fail,
    )

    result = await ConsoleChannelService._execute_with_token(
        "grok-4.3",
        build_payload,
        stream=False,
    )

    assert used_tokens == ["token-a", "token-b"]
    assert recorded == [("token-a", 401, "console_user_blocked", 1)]
    assert result == ["ok:token-b"]


@pytest.mark.asyncio
async def test_console_execute_does_not_disable_cloudflare_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TokenManager()
    manager.initialized = True
    manager.pools["ssoBasic"] = TokenPool("ssoBasic")
    manager.pools["ssoBasic"].add(_token("token-a"))
    manager.pools["ssoBasic"].add(_token("token-b"))
    used_tokens: list[str] = []
    recorded: list[tuple[str, int, str, int | None]] = []

    async def fake_get_token_manager() -> TokenManager:
        return manager

    async def fake_reload_if_stale() -> None:
        return None

    async def fake_stream_upstream(
        payload: dict[str, str],
        *,
        token: str,
        team_id: str | None = None,
    ) -> AsyncIterator[str]:
        used_tokens.append(token)
        if token == "token-a":
            raise UpstreamException(
                "cloudflare challenge",
                status_code=403,
                details={
                    "status": 403,
                    "body": "<!DOCTYPE html><title>Attention Required! | Cloudflare</title>",
                },
            )
        yield f"ok:{payload['token']}"

    def fake_get_config(key: str, default: object = None) -> object:
        if key == "retry.max_retry":
            return 2
        return default

    async def fake_record_fail(
        token: str,
        status_code: int = 401,
        reason: str = "",
        threshold: int | None = None,
    ) -> bool:
        recorded.append((token, status_code, reason, threshold))
        return True

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
        "grok2api.services.grok.services.console_channel.TokenService.record_fail",
        fake_record_fail,
    )

    result = await ConsoleChannelService._execute_with_token(
        "grok-4.3",
        build_payload,
        stream=False,
    )

    assert used_tokens == ["token-a", "token-b"]
    assert recorded == []
    assert result == ["ok:token-b"]


@pytest.mark.asyncio
async def test_console_execute_initializes_missing_team_id_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TokenManager()
    manager.initialized = True
    manager.pools["ssoBasic"] = TokenPool("ssoBasic")
    manager.pools["ssoBasic"].add(_token("token-a", console_team_id=""))
    seen_team_ids: list[str | None] = []
    ensure_calls: list[tuple[str, str, str]] = []

    async def fake_get_token_manager() -> TokenManager:
        return manager

    async def fake_reload_if_stale() -> None:
        return None

    async def fake_ensure_console_team_id(
        token_info: TokenInfo,
        pool_name: str,
        *,
        trigger: str = "request",
    ) -> str:
        ensure_calls.append((token_info.token, pool_name, trigger))
        token_info.console_team_id = "33ec95d2-5364-4c7f-b1b3-b5bff151adb0"
        return token_info.console_team_id

    async def fake_stream_upstream(
        payload: dict[str, str],
        *,
        token: str,
        team_id: str | None = None,
    ) -> AsyncIterator[str]:
        seen_team_ids.append(team_id)
        yield f"ok:{payload['token']}"

    def fake_get_config(key: str, default: object = None) -> object:
        if key == "retry.max_retry":
            return 1
        return default

    async def build_payload(token: str) -> dict[str, str]:
        return {"token": token}

    monkeypatch.setattr(
        "grok2api.services.grok.services.console_channel.get_token_manager",
        fake_get_token_manager,
    )
    monkeypatch.setattr(manager, "reload_if_stale", fake_reload_if_stale)
    monkeypatch.setattr(
        manager,
        "ensure_console_team_id",
        fake_ensure_console_team_id,
    )
    monkeypatch.setattr(
        "grok2api.services.grok.services.console_channel.get_config",
        fake_get_config,
    )
    monkeypatch.setattr(
        ConsoleChannelService,
        "_stream_upstream",
        fake_stream_upstream,
    )

    result = await ConsoleChannelService._execute_with_token(
        "grok-4.3",
        build_payload,
        stream=False,
    )

    assert ensure_calls == [("token-a", "ssoBasic", "request")]
    assert seen_team_ids == ["33ec95d2-5364-4c7f-b1b3-b5bff151adb0"]
    assert result == ["ok:token-a"]


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
