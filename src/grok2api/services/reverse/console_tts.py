"""Reverse interface: console.x.ai TTS API."""

from __future__ import annotations

from typing import Any, Dict, Tuple

import orjson

from grok2api.services.reverse.console_constants import CONSOLE_TTS_API, CONSOLE_VOICE_TIMEOUT
from grok2api.services.reverse.console_voice_transport import (
    execute_console_voice_request,
    read_response_body,
)
from grok2api.services.reverse.utils.headers import build_console_voice_headers


class ConsoleTtsReverse:
    """POST console.x.ai/v1/tts."""

    @staticmethod
    async def request(token: str, payload: Dict[str, Any]) -> Tuple[bytes, str]:
        headers = build_console_voice_headers(token, mode="json")

        async def _do_post(session, proxy, proxies, browser):
            return await session.post(
                CONSOLE_TTS_API,
                headers=headers,
                data=orjson.dumps(payload),
                timeout=float(CONSOLE_VOICE_TIMEOUT),
                proxy=proxy,
                proxies=proxies,
                impersonate=browser,
            )

        async def _process(response):
            content_type = str(response.headers.get("content-type") or "application/octet-stream")
            body = await read_response_body(response)
            return body, content_type

        return await execute_console_voice_request(
            label="ConsoleTtsReverse",
            process=_process,
            do_post=_do_post,
        )


__all__ = ["ConsoleTtsReverse"]
