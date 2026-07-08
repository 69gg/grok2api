"""Console native endpoint orchestration."""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional

import orjson

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger
from grok2api.services.grok.services.console_channel import ConsoleChannelService
from grok2api.services.grok.utils.errors import no_token_error
from grok2api.services.grok.utils.retry import pick_token_info_round_robin
from grok2api.services.reverse.console_native import ConsoleNativeResponse, ConsoleNativeReverse
from grok2api.services.token import get_token_manager


class ConsoleNativeService:
    """Run Console native requests with account rotation."""

    @staticmethod
    def _json_body(payload: Dict[str, Any]) -> bytes:
        return orjson.dumps(payload)

    @staticmethod
    async def _execute(
        *,
        model_id: str,
        method: str,
        path: str,
        body: bytes | None,
        content_type: Optional[str],
        stream: bool,
        referer_path: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> ConsoleNativeResponse | AsyncIterator[bytes]:
        token_mgr = await get_token_manager()
        await token_mgr.reload_if_stale()
        tried: set[str] = set()
        max_retries = int(get_config("retry.max_retry") or 3)
        last_error: Optional[UpstreamException] = None

        for attempt in range(max_retries):
            selected = await pick_token_info_round_robin(token_mgr, model_id, tried)
            if not selected:
                break
            pool_name, token_info = selected
            token = token_mgr.token_value(token_info)
            tried.add(token)
            try:
                team_id = await token_mgr.ensure_console_team_id(
                    token_info,
                    pool_name,
                    trigger="native_request",
                )
                return await ConsoleNativeReverse.request(
                    token=token,
                    method=method,
                    path=path,
                    body=body,
                    content_type=content_type,
                    stream=stream,
                    team_id=team_id,
                    referer_path=referer_path,
                    extra_headers=extra_headers,
                )
            except UpstreamException as exc:
                last_error = exc
                await ConsoleChannelService._handle_token_upstream_failure(
                    token_mgr,
                    token,
                    exc,
                )
                status = (exc.details or {}).get("status")
                logger.warning(
                    "Console native upstream failed for token {}... status={}, trying next token (attempt {}/{})",
                    token[:10],
                    status,
                    attempt + 1,
                    max_retries,
                )
                continue

        if last_error:
            raise last_error
        raise no_token_error(model_id)

    @staticmethod
    async def json_request(
        *,
        model_id: str,
        path: str,
        payload: Dict[str, Any],
        stream: bool = False,
        referer_path: Optional[str] = None,
    ) -> ConsoleNativeResponse | AsyncIterator[bytes]:
        return await ConsoleNativeService._execute(
            model_id=model_id,
            method="POST",
            path=path,
            body=ConsoleNativeService._json_body(payload),
            content_type="application/json",
            stream=stream,
            referer_path=referer_path,
        )

    @staticmethod
    async def raw_request(
        *,
        model_id: str,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: Optional[str] = None,
        stream: bool = False,
        referer_path: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> ConsoleNativeResponse | AsyncIterator[bytes]:
        return await ConsoleNativeService._execute(
            model_id=model_id,
            method=method,
            path=path,
            body=body,
            content_type=content_type,
            stream=stream,
            referer_path=referer_path,
            extra_headers=extra_headers,
        )


__all__ = ["ConsoleNativeService"]
