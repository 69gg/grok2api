"""Unit tests for Console Voice (TTS/STT) helpers and API mapping."""

from __future__ import annotations

from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from grok2api.core.exceptions import ValidationException
from grok2api.main import create_app
from grok2api.services.grok.services.console_voice import (
    ConsoleVoiceService,
    build_stt_fields,
    build_tts_payload,
    normalize_voice_id,
    to_openai_verbose_json,
    words_to_srt,
    words_to_vtt,
)
from grok2api.services.reverse.console_native import ConsoleNativeResponse
from grok2api.services.grok.services.model import (
    CONSOLE_VOICE_MODEL_IDS,
    CONSOLE_VOICE_STT_MODEL_IDS,
    CONSOLE_VOICE_TTS_MODEL_IDS,
    ModelService,
)
from grok2api.services.reverse.utils.proxy import CurlCffiProxyKwargs


def test_console_voice_models_registered():
    for model_id in CONSOLE_VOICE_MODEL_IDS:
        assert ModelService.valid(model_id)
        info = ModelService.get(model_id)
        assert info is not None
        assert info.owned_by == "xai-console-voice<grok2api@69gg>"
        assert ModelService.pool_for_model(model_id) == "ssoBasic"


def test_tts_and_stt_model_sets():
    assert CONSOLE_VOICE_TTS_MODEL_IDS == {"grok-tts-1"}
    assert CONSOLE_VOICE_STT_MODEL_IDS == {"grok-stt-1"}


def test_normalize_voice_id_aliases_and_builtin():
    assert normalize_voice_id("alloy") == "ara"
    assert normalize_voice_id("Eve") == "eve"
    assert normalize_voice_id("LEO") == "leo"


def test_build_tts_payload_mp3_and_speed_clamp():
    payload = build_tts_payload(
        text="hello",
        voice="alloy",
        response_format="mp3",
        speed=2.0,
    )
    assert payload["voice_id"] == "ara"
    assert payload["output_format"]["codec"] == "mp3"
    assert payload["output_format"]["bit_rate"] == 128000
    assert payload["speed"] == 1.5


def test_build_tts_payload_opus_maps_to_mp3():
    payload = build_tts_payload(text="hi", voice="eve", response_format="opus")
    assert payload["output_format"]["codec"] == "mp3"


def test_build_tts_payload_rejects_empty_input():
    with pytest.raises(ValidationException):
        build_tts_payload(text="  ", voice="eve")


def test_build_stt_fields_order_and_flags():
    fields = build_stt_fields(
        language="en",
        formatted=True,
        diarize=True,
        keyterms=["hello", "world"],
    )
    names = [name for name, _ in fields]
    assert names == ["language", "format", "diarize", "keyterm", "keyterm"]


def test_build_stt_fields_defaults_language_when_formatted():
    fields = build_stt_fields(formatted=True)
    assert fields[0] == ("language", "en")
    assert ("format", "true") in fields


def test_build_stt_fields_skips_language_when_unformatted():
    fields = build_stt_fields(formatted=False)
    assert not any(name == "language" for name, _ in fields)


def test_words_to_srt_and_vtt():
    words = [
        {"text": "Hello", "start": 0.0, "end": 0.5},
        {"text": "world", "start": 0.5, "end": 1.0},
    ]
    srt = words_to_srt(words)
    assert "Hello" in srt
    assert "-->" in srt
    vtt = words_to_vtt(words)
    assert vtt.startswith("WEBVTT")


def test_to_openai_verbose_json_shape():
    data = to_openai_verbose_json(
        {
            "text": "hello world",
            "duration": 1.2,
            "words": [{"text": "hello", "start": 0, "end": 0.5}],
        }
    )
    assert data["task"] == "transcribe"
    assert data["text"] == "hello world"
    assert data["duration"] == 1.2
    assert len(data["segments"]) == 1


@pytest.mark.asyncio
async def test_speech_rejects_stt_model():
    with pytest.raises(ValidationException):
        await ConsoleVoiceService.speech(model="grok-stt-1", input_text="hi")


@pytest.mark.asyncio
async def test_transcribe_rejects_tts_model():
    with pytest.raises(ValidationException):
        await ConsoleVoiceService.transcribe(
            model="grok-tts-1",
            file_bytes=b"abc",
            filename="a.wav",
        )


@pytest.mark.asyncio
async def test_speech_mock_upstream():
    async def _fake_execute(_model, call):
        return await call("token")

    with patch(
        "grok2api.services.grok.services.console_voice.ConsoleTtsReverse.request",
        new=AsyncMock(return_value=(b"\xff\xfb", "audio/mpeg")),
    ):
        with patch.object(
            ConsoleVoiceService,
            "_execute_with_token",
            new=AsyncMock(side_effect=_fake_execute),
        ):
            body, content_type = await ConsoleVoiceService.speech(
                model="grok-tts-1",
                input_text="test",
                voice="eve",
                response_format="mp3",
            )
    assert body == b"\xff\xfb"
    assert content_type == "audio/mpeg"


@pytest.mark.asyncio
async def test_transcribe_mock_upstream():
    async def _fake_execute(_model, call):
        return await call("token")

    upstream = {"text": "hello", "duration": 0.5, "words": []}
    with patch(
        "grok2api.services.grok.services.console_voice.ConsoleSttReverse.request",
        new=AsyncMock(return_value=upstream),
    ):
        with patch.object(
            ConsoleVoiceService,
            "_execute_with_token",
            new=AsyncMock(side_effect=_fake_execute),
        ):
            result = await ConsoleVoiceService.transcribe(
                model="grok-stt-1",
                file_bytes=b"RIFF",
                filename="sample.wav",
            )
    assert result["text"] == "hello"


@pytest.mark.asyncio
async def test_console_voice_transport_uses_console_proxy_first(monkeypatch):
    from grok2api.services.reverse import console_voice_transport

    calls: list[dict[str, object]] = []

    class FakeSession:
        async def close(self) -> None:
            pass

    class FakeResponse:
        status_code = 200
        content = b"{}"
        headers = {}

    def fake_session() -> FakeSession:
        return FakeSession()

    def fake_proxy_kwargs(*keys: str) -> CurlCffiProxyKwargs:
        assert keys == ("proxy.console_proxy_url", "proxy.base_proxy_url")
        return CurlCffiProxyKwargs(
            "proxy.console_proxy_url",
            "socks5h://127.0.0.1:1080",
            None,
        )

    async def fake_do_post(
        session: FakeSession,
        proxy: str | None,
        proxies: dict[str, str] | None,
        browser: str | None,
    ) -> FakeResponse:
        calls.append({"proxy": proxy, "proxies": proxies, "browser": browser})
        return FakeResponse()

    async def fake_process(_response: FakeResponse) -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setattr(console_voice_transport, "AsyncSession", fake_session)
    monkeypatch.setattr(
        console_voice_transport,
        "build_curl_cffi_proxy_kwargs",
        fake_proxy_kwargs,
    )
    monkeypatch.setattr(
        console_voice_transport,
        "get_config",
        lambda key, default=None: "chrome136" if key == "proxy.browser" else default,
    )
    monkeypatch.setattr(
        "grok2api.services.reverse.utils.retry.get_config",
        lambda key, default=None: {
            "retry.max_retry": 0,
            "retry.retry_status_codes": [502],
            "retry.retry_budget": 1.0,
            "retry.retry_backoff_base": 0.1,
            "retry.retry_backoff_factor": 2.0,
            "retry.retry_backoff_max": 1.0,
        }.get(key, default),
    )

    result = await console_voice_transport.execute_console_voice_request(
        label="test",
        process=fake_process,
        do_post=fake_do_post,
    )

    assert result == {"ok": True}
    assert calls == [
        {
            "proxy": "socks5h://127.0.0.1:1080",
            "proxies": None,
            "browser": "chrome136",
        }
    ]


@pytest.mark.asyncio
async def test_translate_forces_english():
    async def _fake_execute(_model, call):
        return await call("token")

    with patch(
        "grok2api.services.grok.services.console_voice.ConsoleSttReverse.request",
        new=AsyncMock(return_value={"text": "hello", "words": []}),
    ) as mock_stt:
        with patch.object(
            ConsoleVoiceService,
            "_execute_with_token",
            new=AsyncMock(side_effect=_fake_execute),
        ):
            result = await ConsoleVoiceService.translate(
                model="grok-stt-1",
                file_bytes=b"RIFF",
                filename="sample.wav",
            )
    assert result["text"] == "hello"
    fields = mock_stt.call_args.kwargs["fields"]
    assert ("language", "en") in fields
    assert ("format", "true") in fields


def test_build_stt_ws_url():
    from grok2api.services.reverse.console_voice_ws import build_stt_ws_url

    assert build_stt_ws_url() == "wss://console.x.ai/v1/stt"
    url = build_stt_ws_url({"sample_rate": "16000", "encoding": "pcm"})
    assert url.startswith("wss://console.x.ai/v1/stt?")
    assert "sample_rate=16000" in url


@pytest.mark.asyncio
async def test_relay_stt_websocket_uses_console_proxy_keys(monkeypatch):
    from grok2api.services.reverse import console_voice_ws

    created: list[tuple[str, ...]] = []

    class FakeUpstreamWS:
        closed = False

        async def close(self) -> None:
            self.closed = True

        def __aiter__(self) -> AsyncIterator[object]:
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

    class FakeConn:
        def __init__(self) -> None:
            self.ws = FakeUpstreamWS()

        async def close(self) -> None:
            await self.ws.close()

    class FakeClient:
        def __init__(self, *, proxy_config_keys: tuple[str, ...]) -> None:
            created.append(proxy_config_keys)

        async def connect(self, *args: Any, **kwargs: Any) -> FakeConn:
            return FakeConn()

    class FakeClientWS:
        client_state = type("S", (), {"name": "DISCONNECTED"})()

        async def receive(self) -> dict[str, str]:
            return {"type": "websocket.disconnect"}

        async def close(self) -> None:
            pass

    monkeypatch.setattr(console_voice_ws, "WebSocketClient", FakeClient)

    await console_voice_ws.relay_stt_websocket(FakeClientWS(), "token")

    assert created == [("proxy.console_proxy_url", "proxy.base_proxy_url")]


@pytest.mark.asyncio
async def test_relay_stt_websocket_mock():
    from grok2api.services.reverse.console_voice_ws import relay_stt_websocket

    class FakeUpstreamWS:
        closed = False

        async def send_bytes(self, data):
            pass

        async def send_str(self, text):
            pass

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeConn:
        def __init__(self):
            self.ws = FakeUpstreamWS()

        async def close(self):
            await self.ws.close()

    class FakeClientWS:
        client_state = type("S", (), {"name": "CONNECTED"})()

        def __init__(self):
            self._messages = [{"type": "websocket.disconnect"}]

        async def receive(self):
            return self._messages.pop(0)

        async def close(self):
            pass

    with patch(
        "grok2api.services.reverse.console_voice_ws.WebSocketClient.connect",
        new=AsyncMock(return_value=FakeConn()),
    ):
        await relay_stt_websocket(FakeClientWS(), "token", {"sample_rate": "16000"})


def test_audio_api_translations_json():
    app = create_app()
    client = TestClient(app)
    with patch(
        "grok2api.api.v1.audio.ConsoleVoiceService.translate",
        new=AsyncMock(return_value={"text": "translated", "words": []}),
    ):
        response = client.post(
            "/v1/audio/translations",
            data={"model": "grok-stt-1", "response_format": "json"},
            files={"file": ("test.wav", b"RIFF", "audio/wav")},
            headers={"Authorization": "Bearer test-key"},
        )
    assert response.status_code == 200
    assert response.json() == {"text": "translated"}


def test_stt_ws_rejects_missing_api_key_when_configured():
    app = create_app()
    client = TestClient(app)
    with patch("grok2api.api.v1.audio_ws.get_admin_api_keys", return_value=["secret-key"]):
        try:
            with client.websocket_connect("/v1/audio/stt/ws"):
                pytest.fail("expected websocket connection to be rejected")
        except Exception:
            pass


def test_stt_ws_accepts_with_api_key_query():
    app = create_app()
    client = TestClient(app)

    async def _fake_relay(ws, *, model, query_params):
        await ws.send_text('{"type":"transcript.created"}')
        await ws.close()

    with patch("grok2api.api.v1.audio_ws.get_admin_api_keys", return_value=["secret-key"]):
        with patch("grok2api.core.auth.get_admin_api_keys", return_value=["secret-key"]):
            with patch(
                "grok2api.api.v1.audio_ws.ConsoleVoiceService.relay_stt_websocket",
                new=AsyncMock(side_effect=_fake_relay),
            ):
                with client.websocket_connect("/v1/audio/stt/ws?api_key=secret-key") as ws:
                    msg = ws.receive_text()
                    assert "transcript.created" in msg


def test_audio_api_speech_route():
    app = create_app()
    client = TestClient(app)
    with patch(
        "grok2api.api.v1.audio.ConsoleNativeService.json_request",
        new=AsyncMock(
            return_value=ConsoleNativeResponse(
                body=b"audio-bytes",
                status_code=200,
                content_type="audio/mpeg",
                headers={},
            )
        ),
    ):
        response = client.post(
            "/v1/audio/speech",
            json={
                "model": "grok-tts-1",
                "input": "Hello",
                "voice": "eve",
                "response_format": "mp3",
            },
            headers={"Authorization": "Bearer test-key"},
        )
    assert response.status_code == 200
    assert response.content == b"audio-bytes"
    assert response.headers["content-type"].startswith("audio/mpeg")


def test_audio_api_transcriptions_json():
    app = create_app()
    client = TestClient(app)
    with patch(
        "grok2api.api.v1.audio.ConsoleVoiceService.transcribe",
        new=AsyncMock(return_value={"text": "transcribed", "words": []}),
    ):
        response = client.post(
            "/v1/audio/transcriptions",
            data={"model": "grok-stt-1", "response_format": "json"},
            files={"file": ("test.wav", b"RIFF", "audio/wav")},
            headers={"Authorization": "Bearer test-key"},
        )
    assert response.status_code == 200
    assert response.json() == {"text": "transcribed"}
