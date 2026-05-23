"""OpenAI-compatible audio API (Console Voice TTS/STT)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from grok2api.core.exceptions import ValidationException
from grok2api.services.grok.services.console_voice import (
    ConsoleVoiceService,
    to_openai_verbose_json,
    words_to_srt,
    words_to_vtt,
)

router = APIRouter(tags=["Audio"])

ALLOWED_RESPONSE_FORMATS = {"json", "text", "srt", "verbose_json", "vtt"}


def _format_stt_response(result: dict, fmt: str) -> Response:
    text = str(result.get("text") or "")
    words = result.get("words") if isinstance(result.get("words"), list) else []

    if fmt == "text":
        return PlainTextResponse(text)
    if fmt == "srt":
        return PlainTextResponse(words_to_srt(words, fallback_text=text), media_type="text/plain")
    if fmt == "vtt":
        return PlainTextResponse(words_to_vtt(words, fallback_text=text), media_type="text/vtt")
    if fmt == "verbose_json":
        return JSONResponse(to_openai_verbose_json(result))
    return JSONResponse({"text": text})


class SpeechRequest(BaseModel):
    """OpenAI-compatible TTS request."""

    model: str = Field("grok-tts-1", description="TTS model id")
    input: str = Field(..., description="Text to synthesize")
    voice: str = Field("eve", description="Voice id or OpenAI alias")
    response_format: Optional[str] = Field("mp3", description="mp3, wav, pcm, opus")
    speed: Optional[float] = Field(None, ge=0.7, le=1.5, description="Speech speed")
    instructions: Optional[str] = Field(None, description="Ignored (xAI has no equivalent)")


@router.post("/audio/speech")
async def create_speech(request: SpeechRequest) -> Response:
    body, content_type = await ConsoleVoiceService.speech(
        model=request.model,
        input_text=request.input,
        voice=request.voice,
        response_format=request.response_format,
        speed=request.speed,
    )
    return Response(content=body, media_type=content_type)


@router.post("/audio/transcriptions")
async def create_transcription(
    file: UploadFile = File(...),
    model: str = Form("grok-stt-1"),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: Optional[str] = Form("json"),
    temperature: Optional[float] = Form(None),
    format: Optional[bool] = Form(True),
    diarize: Optional[bool] = Form(None),
    multichannel: Optional[bool] = Form(None),
    filler_words: Optional[bool] = Form(None),
) -> Response:
    if prompt or temperature is not None:
        pass  # v1: ignored per plan

    fmt = (response_format or "json").strip().lower()
    if fmt not in ALLOWED_RESPONSE_FORMATS:
        raise ValidationException(
            message=f"Unsupported response_format: {response_format}",
            param="response_format",
        )

    file_bytes = await file.read()
    result = await ConsoleVoiceService.transcribe(
        model=model,
        file_bytes=file_bytes,
        filename=file.filename or "audio.wav",
        content_type=file.content_type,
        language=language,
        formatted=bool(format) if format is not None else True,
        diarize=diarize,
        multichannel=multichannel,
        filler_words=filler_words,
    )

    return _format_stt_response(result, fmt)


@router.post("/audio/translations")
async def create_translation(
    file: UploadFile = File(...),
    model: str = Form("grok-stt-1"),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: Optional[str] = Form("json"),
    temperature: Optional[float] = Form(None),
    format: Optional[bool] = Form(True),
    diarize: Optional[bool] = Form(None),
    multichannel: Optional[bool] = Form(None),
    filler_words: Optional[bool] = Form(None),
) -> Response:
    if language or prompt or temperature is not None:
        pass  # v1: ignored; translations always force English via STT

    fmt = (response_format or "json").strip().lower()
    if fmt not in ALLOWED_RESPONSE_FORMATS:
        raise ValidationException(
            message=f"Unsupported response_format: {response_format}",
            param="response_format",
        )

    file_bytes = await file.read()
    result = await ConsoleVoiceService.translate(
        model=model,
        file_bytes=file_bytes,
        filename=file.filename or "audio.wav",
        content_type=file.content_type,
        diarize=diarize,
        multichannel=multichannel,
        filler_words=filler_words,
    )

    return _format_stt_response(result, fmt)


@router.get("/audio/voices")
async def list_voices(model: str = "grok-tts-1") -> JSONResponse:
    data = await ConsoleVoiceService.list_voices(model=model)
    return JSONResponse(data)


__all__ = ["router"]
