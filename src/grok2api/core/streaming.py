"""Helpers for safe SSE streaming without post-start exception handler crashes."""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, AsyncIterable, Callable, Optional

import orjson

from grok2api.core.exceptions import AppException
from grok2api.core.logger import logger


def _openai_chat_error_chunks(exc: Exception) -> list[str]:
    if isinstance(exc, AppException):
        payload = {
            "error": {
                "message": exc.message,
                "type": exc.error_type,
                "code": exc.code,
            }
        }
    else:
        payload = {
            "error": {
                "message": str(exc) or "stream_error",
                "type": "server_error",
                "code": "stream_error",
            }
        }
    encoded = orjson.dumps(payload).decode()
    return [f"event: error\ndata: {encoded}\n\n", "data: [DONE]\n\n"]


def _anthropic_error_chunks(exc: Exception) -> list[str]:
    if isinstance(exc, AppException):
        message = exc.message
        error_type = exc.error_type
    else:
        message = str(exc) or "stream_error"
        error_type = "api_error"
    payload = {"type": "error", "error": {"type": error_type, "message": message}}
    return [f"event: error\ndata: {orjson.dumps(payload).decode()}\n\n"]


def _responses_error_chunks(exc: Exception) -> list[str]:
    message = exc.message if isinstance(exc, AppException) else (str(exc) or "stream_error")
    payload = {"type": "error", "error": {"message": message}}
    encoded = orjson.dumps(payload).decode()
    return [f"data: {encoded}\n\n"]


async def safe_sse_stream(
    stream: AsyncIterable[str],
    *,
    error_chunks: Optional[Callable[[Exception], list[str]]] = None,
) -> AsyncGenerator[str, None]:
    """Swallow stream exceptions and emit SSE error events instead of crashing ASGI."""
    emit_error = error_chunks or _openai_chat_error_chunks
    try:
        async for chunk in stream:
            yield chunk
    except asyncio.CancelledError:
        raise
    except GeneratorExit:
        raise
    except Exception as exc:
        logger.warning("SSE stream interrupted: %s: %s", type(exc).__name__, exc)
        for part in emit_error(exc):
            yield part


async def safe_openai_chat_stream(stream: AsyncIterable[str]) -> AsyncGenerator[str, None]:
    async for chunk in safe_sse_stream(stream, error_chunks=_openai_chat_error_chunks):
        yield chunk


async def safe_anthropic_stream(stream: AsyncIterable[str]) -> AsyncGenerator[str, None]:
    async for chunk in safe_sse_stream(stream, error_chunks=_anthropic_error_chunks):
        yield chunk


async def safe_responses_stream(stream: AsyncIterable[str]) -> AsyncGenerator[str, None]:
    async for chunk in safe_sse_stream(stream, error_chunks=_responses_error_chunks):
        yield chunk


__all__ = [
    "safe_anthropic_stream",
    "safe_openai_chat_stream",
    "safe_responses_stream",
    "safe_sse_stream",
]
