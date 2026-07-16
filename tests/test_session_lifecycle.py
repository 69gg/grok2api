"""Regression tests for upstream session ownership and cancellation cleanup."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from grok2api.services.grok.services import chat as chat_module
from grok2api.services.reverse import build_native as build_native_module
from grok2api.services.reverse.build_native import (
    BuildNativeResponse,
    BuildNativeReverse,
)
from grok2api.services.reverse.utils.proxy import CurlCffiProxyKwargs


class _FakeBuildResponse:
    status_code = 200
    headers: dict[str, str] = {"content-type": "application/json"}
    content = b'{"ok":true}'


def _no_proxy(*_keys: str) -> CurlCffiProxyKwargs:
    return CurlCffiProxyKwargs(None, None, None)


@pytest.mark.asyncio
async def test_build_native_closes_session_before_transport_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    config_keys: list[str] = []

    class FakeSession:
        def __init__(self) -> None:
            self.index = len(sessions)
            self.close_count = 0
            sessions.append(self)
            events.append(f"create:{self.index}")

        async def post(
            self,
            _url: str,
            *,
            data: bytes | None = None,
            **_kwargs: object,
        ) -> _FakeBuildResponse:
            del data
            events.append(f"post:{self.index}")
            if self.index == 0:
                raise RuntimeError("connection reset")
            return _FakeBuildResponse()

        async def close(self) -> None:
            self.close_count += 1
            events.append(f"close:{self.index}")

    sessions: list[FakeSession] = []

    def fake_get_config(key: str, default: object = None) -> object:
        config_keys.append(key)
        if key == "build.transport_max_retry":
            return 1
        return default

    def fake_extract_status(_error: Exception) -> int:
        return 502

    async def fake_sleep(delay: float) -> None:
        events.append(f"sleep:{delay}")

    monkeypatch.setattr(build_native_module, "AsyncSession", FakeSession)
    monkeypatch.setattr(build_native_module, "get_config", fake_get_config)
    monkeypatch.setattr(build_native_module, "build_curl_cffi_proxy_kwargs", _no_proxy)
    monkeypatch.setattr(build_native_module, "extract_status_for_retry", fake_extract_status)
    monkeypatch.setattr(build_native_module.asyncio, "sleep", fake_sleep)

    result = await BuildNativeReverse.request(
        access_token="token",
        method="POST",
        path="responses",
        body=b"{}",
        base_url="https://example.invalid/v1",
    )

    assert isinstance(result, BuildNativeResponse)
    assert result.body == _FakeBuildResponse.content
    assert [session.close_count for session in sessions] == [1, 1]
    assert events == [
        "create:0",
        "post:0",
        "close:0",
        "sleep:0.5",
        "create:1",
        "post:1",
        "close:1",
    ]
    assert "build.transport_max_retry" in config_keys
    assert "retry.max_retry" not in config_keys


@pytest.mark.asyncio
async def test_build_native_closes_session_when_request_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_started = asyncio.Event()
    never_complete = asyncio.Event()

    class FakeSession:
        def __init__(self) -> None:
            self.close_count = 0
            sessions.append(self)

        async def post(
            self,
            _url: str,
            *,
            data: bytes | None = None,
            **_kwargs: object,
        ) -> _FakeBuildResponse:
            del data
            request_started.set()
            await never_complete.wait()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            self.close_count += 1

    sessions: list[FakeSession] = []

    def fake_get_config(_key: str, default: object = None) -> object:
        return default

    monkeypatch.setattr(build_native_module, "AsyncSession", FakeSession)
    monkeypatch.setattr(build_native_module, "get_config", fake_get_config)
    monkeypatch.setattr(build_native_module, "build_curl_cffi_proxy_kwargs", _no_proxy)

    task = asyncio.create_task(
        BuildNativeReverse.request(
            access_token="token",
            method="POST",
            path="responses",
            body=b"{}",
            base_url="https://example.invalid/v1",
        )
    )
    await request_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sessions) == 1
    assert sessions[0].close_count == 1


@pytest.mark.asyncio
async def test_chat_stream_cancellation_closes_session_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_started = asyncio.Event()
    never_complete = asyncio.Event()

    class FakeSemaphore:
        def __init__(self) -> None:
            self.acquire_count = 0
            self.release_count = 0

        async def acquire(self) -> None:
            self.acquire_count += 1

        def release(self) -> None:
            self.release_count += 1

    class FakeChatSession:
        def __init__(self, **_kwargs: object) -> None:
            self.close_count = 0
            sessions.append(self)

        async def close(self) -> None:
            self.close_count += 1

    sessions: list[FakeChatSession] = []

    semaphore = FakeSemaphore()

    def fake_get_config(_key: str, default: object = None) -> object:
        return default

    async def fake_request(
        session: FakeChatSession,
        token: str,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        assert session is sessions[0]
        assert token == "token"

        async def upstream() -> AsyncIterator[str]:
            stream_started.set()
            await never_complete.wait()
            yield "unreachable"

        return upstream()

    def fake_get_semaphore() -> FakeSemaphore:
        return semaphore

    monkeypatch.setattr(chat_module, "ResettableSession", FakeChatSession)
    monkeypatch.setattr(chat_module, "get_config", fake_get_config)
    monkeypatch.setattr(chat_module, "_get_chat_semaphore", fake_get_semaphore)
    monkeypatch.setattr(
        chat_module.AppChatReverse,
        "request",
        staticmethod(fake_request),
    )

    stream = await chat_module.GrokChatService().chat(
        "token",
        "hello",
        "grok-4.3-fast",
        stream=True,
    )

    async def consume() -> None:
        async for _line in stream:
            pass

    task = asyncio.create_task(consume())
    await stream_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sessions) == 1
    assert sessions[0].close_count == 1
    assert semaphore.acquire_count == 1
    assert semaphore.release_count == 1


@pytest.mark.asyncio
async def test_chat_connection_cancellation_closes_session_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_started = asyncio.Event()
    never_complete = asyncio.Event()

    class FakeSemaphore:
        def __init__(self) -> None:
            self.acquire_count = 0
            self.release_count = 0

        async def acquire(self) -> None:
            self.acquire_count += 1

        def release(self) -> None:
            self.release_count += 1

    class FakeChatSession:
        def __init__(self, **_kwargs: object) -> None:
            self.close_count = 0
            sessions.append(self)

        async def close(self) -> None:
            self.close_count += 1

    sessions: list[FakeChatSession] = []
    semaphore = FakeSemaphore()

    def fake_get_config(_key: str, default: object = None) -> object:
        return default

    async def fake_request(
        _session: FakeChatSession,
        _token: str,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        request_started.set()
        await never_complete.wait()
        raise AssertionError("unreachable")

    def fake_get_semaphore() -> FakeSemaphore:
        return semaphore

    monkeypatch.setattr(chat_module, "ResettableSession", FakeChatSession)
    monkeypatch.setattr(chat_module, "get_config", fake_get_config)
    monkeypatch.setattr(chat_module, "_get_chat_semaphore", fake_get_semaphore)
    monkeypatch.setattr(
        chat_module.AppChatReverse,
        "request",
        staticmethod(fake_request),
    )

    task = asyncio.create_task(
        chat_module.GrokChatService().chat(
            "token",
            "hello",
            "grok-4.3-fast",
            stream=True,
        )
    )
    await request_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sessions) == 1
    assert sessions[0].close_count == 1
    assert semaphore.acquire_count == 1
    assert semaphore.release_count == 1
