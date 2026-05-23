"""Tests for safe SSE streaming helpers."""

from __future__ import annotations

import pytest

from grok2api.core.exceptions import UpstreamException
from grok2api.core.streaming import safe_openai_chat_stream


async def _broken_stream():
    yield "data: {}\n\n"
    raise UpstreamException(message="upstream failed", details={"status": 502})


@pytest.mark.asyncio
async def test_safe_openai_chat_stream_emits_error_instead_of_raising():
    chunks: list[str] = []
    async for chunk in safe_openai_chat_stream(_broken_stream()):
        chunks.append(chunk)
    assert chunks[0] == "data: {}\n\n"
    assert any("event: error" in c for c in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"
