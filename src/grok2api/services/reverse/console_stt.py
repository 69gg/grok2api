"""Reverse interface: console.x.ai STT API."""

from __future__ import annotations

from typing import Dict, List, Tuple

import orjson
from curl_cffi.curl import CurlMime

from grok2api.services.reverse.console_constants import CONSOLE_STT_API, CONSOLE_VOICE_TIMEOUT
from grok2api.services.reverse.console_voice_transport import (
    execute_console_voice_request,
    read_error_text,
)
from grok2api.services.reverse.utils.headers import build_console_voice_headers


class ConsoleSttReverse:
    """POST console.x.ai/v1/stt."""

    @staticmethod
    async def request(
        token: str,
        *,
        fields: List[Tuple[str, str]],
        file_field: Tuple[str, bytes, str],
    ) -> Dict[str, Any]:
        headers = build_console_voice_headers(token, mode="multipart")
        multipart = CurlMime()
        for name, value in fields:
            multipart.addpart(name=name, data=value.encode() if isinstance(value, str) else value)
        filename, content, mime = file_field
        multipart.addpart(name="file", data=content, filename=filename, content_type=mime)

        async def _do_post(session, proxy, proxies, browser):
            return await session.post(
                CONSOLE_STT_API,
                headers=headers,
                multipart=multipart,
                timeout=float(CONSOLE_VOICE_TIMEOUT),
                proxy=proxy,
                proxies=proxies,
                impersonate=browser,
            )

        async def _process(response):
            text = await read_error_text(response)
            try:
                parsed = orjson.loads(text)
            except orjson.JSONDecodeError as exc:
                from grok2api.core.exceptions import UpstreamException

                raise UpstreamException(
                    message="ConsoleSttReverse: invalid JSON response",
                    details={"body": text[:2000]},
                ) from exc
            if not isinstance(parsed, dict):
                from grok2api.core.exceptions import UpstreamException

                raise UpstreamException(
                    message="ConsoleSttReverse: expected JSON object",
                    details={"body": text[:2000]},
                )
            return parsed

        return await execute_console_voice_request(
            label="ConsoleSttReverse",
            process=_process,
            do_post=_do_post,
        )


__all__ = ["ConsoleSttReverse"]
