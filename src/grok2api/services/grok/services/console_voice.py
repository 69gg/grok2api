"""Console.x.ai Voice (TTS/STT) orchestration with OpenAI-compatible mapping."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import orjson

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException, ValidationException
from grok2api.core.logger import logger
from grok2api.services.grok.services.console_channel import _is_console_user_blocked
from grok2api.services.grok.services.model import CONSOLE_VOICE_MODEL_IDS, CONSOLE_VOICE_STT_MODEL_IDS, CONSOLE_VOICE_TTS_MODEL_IDS
from grok2api.services.grok.utils.errors import no_token_error
from grok2api.services.grok.utils.retry import pick_token_round_robin, rate_limited
from grok2api.services.reverse.console_constants import CONSOLE_TTS_VOICES_API, CONSOLE_VOICE_TIMEOUT
from grok2api.services.reverse.console_stt import ConsoleSttReverse
from grok2api.services.reverse.console_tts import ConsoleTtsReverse
from grok2api.services.reverse.console_voice_transport import (
    execute_console_voice_request,
    read_error_text,
)
from grok2api.services.reverse.utils.headers import build_console_voice_headers
from grok2api.services.token import get_token_manager
from grok2api.services.token.service import TokenService

OPENAI_VOICE_ALIASES = {
    "alloy": "ara",
    "echo": "rex",
    "fable": "sal",
    "onyx": "leo",
    "nova": "eve",
    "shimmer": "sal",
}

XAI_BUILTIN_VOICES = frozenset({"eve", "ara", "rex", "sal", "leo"})

CODEC_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
    "mulaw": "audio/basic",
    "alaw": "audio/basic",
}


def normalize_voice_id(voice: str) -> str:
    value = str(voice or "eve").strip().lower()
    return OPENAI_VOICE_ALIASES.get(value, value)


def resolve_tts_codec(response_format: Optional[str]) -> str:
    codec = str(response_format or "mp3").strip().lower()
    if codec == "opus":
        return "mp3"
    if codec in {"mp3", "wav", "pcm", "mulaw", "alaw"}:
        return codec
    raise ValidationException(
        message=f"Unsupported response_format: {response_format}",
        param="response_format",
    )


def build_tts_payload(
    *,
    text: str,
    voice: str,
    response_format: Optional[str] = None,
    speed: Optional[float] = None,
    language: Optional[str] = None,
    text_normalization: bool = True,
) -> Dict[str, Any]:
    if not text or not str(text).strip():
        raise ValidationException(message="input text is required", param="input")
    if len(text) > 15_000:
        raise ValidationException(message="input exceeds 15000 characters", param="input")

    codec = resolve_tts_codec(response_format)
    output_format: Dict[str, Any] = {"codec": codec}
    if codec == "mp3":
        output_format["sample_rate"] = 44100
        output_format["bit_rate"] = 128000
    elif codec == "pcm":
        output_format["sample_rate"] = 48000
    elif codec == "wav":
        output_format["sample_rate"] = 44100

    payload: Dict[str, Any] = {
        "text": text,
        "voice_id": normalize_voice_id(voice),
        "language": (language or "en").strip() or "en",
        "output_format": output_format,
        "text_normalization": bool(text_normalization),
    }
    if speed is not None:
        try:
            payload["speed"] = max(0.7, min(1.5, float(speed)))
        except (TypeError, ValueError) as exc:
            raise ValidationException(message="speed must be a number", param="speed") from exc
    return payload


def codec_content_type(codec: str, upstream_content_type: str) -> str:
    if upstream_content_type and upstream_content_type != "application/octet-stream":
        return upstream_content_type.split(";")[0].strip()
    return CODEC_CONTENT_TYPES.get(codec, "application/octet-stream")


def build_stt_fields(
    *,
    language: Optional[str] = None,
    formatted: bool = True,
    diarize: Optional[bool] = None,
    multichannel: Optional[bool] = None,
    filler_words: Optional[bool] = None,
    keyterms: Optional[List[str]] = None,
) -> List[Tuple[str, str]]:
    fields: List[Tuple[str, str]] = []
    lang = (language or "").strip()
    if formatted and not lang:
        lang = "en"
    if lang:
        fields.append(("language", lang))
    if formatted:
        fields.append(("format", "true"))
    if diarize is not None:
        fields.append(("diarize", "true" if diarize else "false"))
    if multichannel is not None:
        fields.append(("multichannel", "true" if multichannel else "false"))
    if filler_words is not None:
        fields.append(("filler_words", "true" if filler_words else "false"))
    for term in keyterms or []:
        value = str(term or "").strip()
        if value:
            fields.append(("keyterm", value))
    return fields


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _format_vtt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, rem = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{rem:03d}"


def words_to_srt(words: List[Dict[str, Any]], *, fallback_text: str = "") -> str:
    if not words:
        return fallback_text
    lines: List[str] = []
    for index, word in enumerate(words, start=1):
        start = float(word.get("start") or 0)
        end = float(word.get("end") or start)
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        lines.append(str(index))
        lines.append(f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def words_to_vtt(words: List[Dict[str, Any]], *, fallback_text: str = "") -> str:
    if not words:
        return fallback_text
    lines = ["WEBVTT", ""]
    for word in words:
        start = float(word.get("start") or 0)
        end = float(word.get("end") or start)
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def to_openai_verbose_json(result: Dict[str, Any]) -> Dict[str, Any]:
    words = result.get("words") if isinstance(result.get("words"), list) else []
    duration = result.get("duration")
    try:
        duration_f = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_f = None
    return {
        "task": "transcribe",
        "language": str(result.get("language") or ""),
        "duration": duration_f,
        "text": str(result.get("text") or ""),
        "words": words,
        "segments": [
            {
                "id": 0,
                "seek": 0,
                "start": float(words[0].get("start") or 0) if words else 0.0,
                "end": float(words[-1].get("end") or 0) if words else (duration_f or 0.0),
                "text": str(result.get("text") or ""),
                "tokens": [],
                "temperature": 0.0,
                "avg_logprob": 0.0,
                "compression_ratio": 0.0,
                "no_speech_prob": 0.0,
            }
        ]
        if words or result.get("text")
        else [],
    }


def ensure_voice_model(model_id: str, *, kind: str = "any") -> None:
    if model_id not in CONSOLE_VOICE_MODEL_IDS:
        raise ValidationException(message=f"Unknown voice model: {model_id}", param="model")
    if kind == "tts" and model_id not in CONSOLE_VOICE_TTS_MODEL_IDS:
        raise ValidationException(
            message=f"Model {model_id} is not a TTS model; use grok-tts-1",
            param="model",
        )
    if kind == "stt" and model_id not in CONSOLE_VOICE_STT_MODEL_IDS:
        raise ValidationException(
            message=f"Model {model_id} is not an STT model; use grok-stt-1",
            param="model",
        )


class ConsoleVoiceService:
    @staticmethod
    async def _handle_token_upstream_failure(token_mgr, token: str, exc: UpstreamException) -> None:
        status = (exc.details or {}).get("status")
        if status == 401:
            try:
                await TokenService.record_fail(
                    token,
                    status,
                    "console_voice_auth_failed",
                )
            except Exception:
                pass
        elif _is_console_user_blocked(exc):
            try:
                await TokenService.record_fail(
                    token,
                    401,
                    "console_voice_user_blocked",
                    threshold=1,
                )
            except Exception:
                pass
        elif status == 403:
            logger.info(
                f"Console voice upstream 403 for token {token[:10]}...; "
                "treating as upstream/proxy forbidden and trying next token without disabling it"
            )
        elif rate_limited(exc):
            # Console Playground 429 is endpoint throttling, not grok.com SSO quota exhaustion.
            logger.info(
                f"Console voice upstream 429 for token {token[:10]}...; "
                "skipping SSO quota/cooling update"
            )

    @staticmethod
    async def _execute_with_token(model_id: str, call):
        token_mgr = await get_token_manager()
        await token_mgr.reload_if_stale()
        tried: set[str] = set()
        max_retries = int(get_config("retry.max_retry") or 3)
        last_error = None
        for attempt in range(max_retries):
            token = await pick_token_round_robin(token_mgr, model_id, tried)
            if not token:
                if last_error:
                    raise last_error
                raise no_token_error(model_id)
            tried.add(token)
            try:
                return await call(token)
            except UpstreamException as exc:
                last_error = exc
                await ConsoleVoiceService._handle_token_upstream_failure(token_mgr, token, exc)
                status = (exc.details or {}).get("status")
                logger.warning(
                    f"Console voice upstream failed for token {token[:10]}... "
                    f"status={status}, trying next token "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                continue
        if last_error:
            raise last_error
        raise no_token_error(model_id)

    @staticmethod
    async def speech(
        *,
        model: str,
        input_text: str,
        voice: str = "eve",
        response_format: Optional[str] = None,
        speed: Optional[float] = None,
        language: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        ensure_voice_model(model, kind="tts")
        payload = build_tts_payload(
            text=input_text,
            voice=voice,
            response_format=response_format,
            speed=speed,
            language=language,
        )
        codec = payload["output_format"]["codec"]

        async def _call(token: str):
            body, content_type = await ConsoleTtsReverse.request(token, payload)
            return body, codec_content_type(codec, content_type)

        return await ConsoleVoiceService._execute_with_token(model, _call)

    @staticmethod
    async def transcribe(
        *,
        model: str,
        file_bytes: bytes,
        filename: str,
        content_type: Optional[str] = None,
        language: Optional[str] = None,
        formatted: bool = True,
        diarize: Optional[bool] = None,
        multichannel: Optional[bool] = None,
        filler_words: Optional[bool] = None,
        keyterms: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        ensure_voice_model(model, kind="stt")
        if not file_bytes:
            raise ValidationException(message="audio file is required", param="file")

        fields = build_stt_fields(
            language=language,
            formatted=formatted,
            diarize=diarize,
            multichannel=multichannel,
            filler_words=filler_words,
            keyterms=keyterms,
        )
        mime = content_type or "application/octet-stream"
        file_field = (filename or "audio.wav", file_bytes, mime)

        async def _call(token: str):
            return await ConsoleSttReverse.request(
                token,
                fields=fields,
                file_field=file_field,
            )

        return await ConsoleVoiceService._execute_with_token(model, _call)

    @staticmethod
    async def translate(
        *,
        model: str,
        file_bytes: bytes,
        filename: str,
        content_type: Optional[str] = None,
        diarize: Optional[bool] = None,
        multichannel: Optional[bool] = None,
        filler_words: Optional[bool] = None,
        keyterms: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """OpenAI /audio/translations alias: STT with forced English output."""
        return await ConsoleVoiceService.transcribe(
            model=model,
            file_bytes=file_bytes,
            filename=filename,
            content_type=content_type,
            language="en",
            formatted=True,
            diarize=diarize,
            multichannel=multichannel,
            filler_words=filler_words,
            keyterms=keyterms,
        )

    @staticmethod
    async def relay_stt_websocket(
        websocket,
        *,
        model: str = "grok-stt-1",
        query_params: Optional[Dict[str, str]] = None,
    ) -> None:
        from grok2api.services.reverse.console_voice_ws import relay_stt_websocket as _relay

        ensure_voice_model(model, kind="stt")
        upstream_query = {
            k: v
            for k, v in (query_params or {}).items()
            if k not in {"model", "api_key"} and v is not None and str(v) != ""
        }

        async def _call(token: str):
            await _relay(websocket, token, upstream_query)

        await ConsoleVoiceService._execute_with_token(model, _call)

    @staticmethod
    async def list_voices(*, model: str = "grok-tts-1") -> Dict[str, Any]:
        ensure_voice_model(model, kind="tts")

        async def _call(token: str):
            headers = build_console_voice_headers(token, mode="json")

            async def _do_post(session, proxy, proxies, browser):
                return await session.get(
                    CONSOLE_TTS_VOICES_API,
                    headers=headers,
                    timeout=float(CONSOLE_VOICE_TIMEOUT),
                    proxy=proxy,
                    proxies=proxies,
                    impersonate=browser,
                )

            async def _process(response):
                text = await read_error_text(response)
                return orjson.loads(text)

            return await execute_console_voice_request(
                label="ConsoleTtsVoices",
                process=_process,
                do_post=_do_post,
            )

        return await ConsoleVoiceService._execute_with_token(model, _call)


__all__ = [
    "ConsoleVoiceService",
    "build_stt_fields",
    "build_tts_payload",
    "normalize_voice_id",
    "to_openai_verbose_json",
    "words_to_srt",
    "words_to_vtt",
]
