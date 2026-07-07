"""Reverse interface: console.x.ai Responses API."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, AsyncIterator, Dict, Optional

import orjson
from curl_cffi.requests import AsyncSession

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger
from grok2api.core.proxy_pool import rotate_proxy, should_rotate_proxy
from grok2api.services.grok.services.console_input import drop_compaction_blobs_from_payload
from grok2api.services.reverse.console_constants import CONSOLE_RESPONSES_API, CONSOLE_TIMEOUT
from grok2api.services.reverse.utils.headers import build_console_headers
from grok2api.services.reverse.utils.proxy import (
    CONSOLE_PROXY_KEYS,
    build_curl_cffi_proxy_kwargs,
)
from grok2api.services.reverse.utils.retry import RetryContext, extract_status_for_retry


def _is_encrypted_replay_decode_error(status_code: int, body: str) -> bool:
    if status_code != 400:
        return False
    lowered = body.lower()
    return (
        "could not decode the compaction blob" in lowered
        or "compact response" in lowered
        or "could not decrypt the provided encrypted_content" in lowered
        or "unmodified encrypted_content from a previous response" in lowered
    )


def _is_compaction_blob_decode_error(status_code: int, body: str) -> bool:
    return _is_encrypted_replay_decode_error(status_code, body)


class ConsoleResponsesReverse:
    """POST console.x.ai/v1/responses."""

    @staticmethod
    async def _read_error_body(response: Any) -> str:
        for attr_name in ("text", "atext", "read", "aread"):
            attr = getattr(response, attr_name, None)
            if attr is None:
                continue
            try:
                value = attr() if callable(attr) else attr
                if inspect.isawaitable(value):
                    value = await value
                if value:
                    return value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value)
            except Exception:
                continue
        content = getattr(response, "content", None)
        if content:
            return content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content)
        return ""

    @staticmethod
    async def request(
        session: AsyncSession,
        token: str,
        payload: Dict[str, Any],
        *,
        stream: bool = True,
        team_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        headers = build_console_headers(cookie_token=token, team_id=team_id)
        timeout = float(CONSOLE_TIMEOUT)
        browser = get_config("proxy.browser")
        active_proxy_key = None

        async def _do_request():
            nonlocal active_proxy_key
            compaction_stripped = False
            while True:
                proxy_kwargs = build_curl_cffi_proxy_kwargs(*CONSOLE_PROXY_KEYS)
                active_proxy_key = proxy_kwargs.active_proxy_key
                body = orjson.dumps(payload)
                response = await session.post(
                    CONSOLE_RESPONSES_API,
                    headers=headers,
                    data=body,
                    timeout=timeout,
                    stream=stream,
                    proxy=proxy_kwargs.proxy,
                    proxies=proxy_kwargs.proxies,
                    impersonate=browser,
                )
                if response.status_code == 200:
                    return response
                content = await ConsoleResponsesReverse._read_error_body(response)
                logger.error(
                    f"ConsoleResponsesReverse: upstream {response.status_code} body: {content[:500]}"
                )
                if (
                    not compaction_stripped
                    and _is_encrypted_replay_decode_error(response.status_code, content)
                ):
                    removed = drop_compaction_blobs_from_payload(payload)
                    if removed > 0:
                        compaction_stripped = True
                        logger.warning(
                            f"ConsoleResponsesReverse: dropped {removed} encrypted replay blob(s), retrying request"
                        )
                        continue
                    logger.warning(
                        "ConsoleResponsesReverse: encrypted replay decode/decrypt error but input has no encrypted replay blobs to drop"
                    )
                raise UpstreamException(
                    message=f"ConsoleResponsesReverse: request failed, {response.status_code}",
                    details={"status": response.status_code, "body": content[:2000]},
                )

        async def _on_transport_retry(
            attempt: int, status_code: int, error: Exception, delay: float
        ) -> None:
            if active_proxy_key and should_rotate_proxy(status_code):
                rotate_proxy(active_proxy_key)

        ctx = RetryContext()
        response = None
        while ctx.attempt <= ctx.max_retry:
            try:
                response = await _do_request()
                break
            except UpstreamException:
                raise
            except Exception as exc:
                status_code = extract_status_for_retry(exc)
                if status_code is None:
                    raise
                ctx.record_error(status_code, exc)
                if not ctx.should_retry(status_code, exc):
                    raise
                delay = ctx.calculate_delay(status_code)
                if ctx.total_delay + delay > ctx.retry_budget:
                    raise
                ctx.record_delay(delay)
                logger.warning(
                    f"ConsoleResponsesReverse transport retry {ctx.attempt}/{ctx.max_retry} "
                    f"for status {status_code}, waiting {delay:.2f}s"
                )
                await _on_transport_retry(ctx.attempt, status_code, exc, delay)
                await asyncio.sleep(delay)
                continue
        if response is None:
            raise UpstreamException(
                message="ConsoleResponsesReverse: request failed after transport retries",
                details={"status": ctx.last_status},
            )

        async def stream_lines() -> AsyncIterator[str]:
            try:
                if stream:
                    async for line in response.aiter_lines():
                        if line is None:
                            continue
                        if isinstance(line, bytes):
                            line = line.decode("utf-8", errors="replace")
                        yield line
                else:
                    text = await ConsoleResponsesReverse._read_error_body(response)
                    for line in text.splitlines():
                        yield line
            finally:
                await session.close()

        return stream_lines()


__all__ = ["ConsoleResponsesReverse", "CONSOLE_RESPONSES_API"]
