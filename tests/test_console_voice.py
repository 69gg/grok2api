"""Unit tests for Console Voice (TTS/STT) helpers and API mapping."""

from __future__ import annotations

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
from grok2api.services.grok.services.model import (
    CONSOLE_VOICE_MODEL_IDS,
    CONSOLE_VOICE_STT_MODEL_IDS,
    CONSOLE_VOICE_TTS_MODEL_IDS,
    ModelService,
)


def test_console_voice_models_registered():
    for model_id in CONSOLE_VOICE_MODEL_IDS:
        assert ModelService.valid(model_id)
        info = ModelService.get(model_id)
        assert info is not None
        assert info.owned_by == "xai-console-voice"
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


def test_audio_api_speech_route():
    app = create_app()
    client = TestClient(app)
    with patch(
        "grok2api.api.v1.audio.ConsoleVoiceService.speech",
        new=AsyncMock(return_value=(b"audio-bytes", "audio/mpeg")),
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
