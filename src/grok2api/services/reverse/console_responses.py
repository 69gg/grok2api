"""Reverse interface: console.x.ai Responses API."""

from __future__ import annotations

import inspect
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import urlparse

import orjson
from curl_cffi.requests import AsyncSession

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger
from grok2api.core.proxy_pool import get_current_proxy_from, rotate_proxy, should_rotate_proxy
from grok2api.services.grok.services.console_input import drop_compaction_blobs_from_payload
from grok2api.services.reverse.console_constants import CONSOLE_RESPONSES_API, CONSOLE_TIMEOUT
from grok2api.services.reverse.utils.headers import build_console_headers
from grok2api.services.reverse.utils.retry import extract_status_for_retry, retry_on_status
from grok2api.services.token.service import TokenService


def _normalize_proxy(proxy_url: str) -> str:
    if not proxy_url:
        return proxy_url
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme == "socks5":
        return proxy_url.replace("socks5://", "socks5h://", 1)
    if scheme == "socks4":
        return proxy_url.replace("socks4://", "socks4a://", 1)
    return proxy_url


def _is_compaction_blob_decode_error(status_code: int, body: str) -> bool:
    if status_code != 400:
        return False
    lowered = body.lower()
    return (
        "could not decode the compaction blob" in lowered
        or "compact response" in lowered
    )


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
    ) -> AsyncIterator[str]:
        headers = build_console_headers(cookie_token=token)
        timeout = float(CONSOLE_TIMEOUT)
        browser = get_config("proxy.browser")
        active_proxy_key = None

        async def _do_request():
            nonlocal active_proxy_key
            compaction_stripped = False
            while True:
                active_proxy_key, base_proxy = get_current_proxy_from("proxy.base_proxy_url")
                proxy = None
                proxies = None
                if base_proxy:
                    normalized = _normalize_proxy(base_proxy)
                    scheme = urlparse(normalized).scheme.lower()
                    if scheme.startswith("socks"):
                        proxy = normalized
                    else:
                        proxies = {"http": normalized, "https": normalized}
                body = orjson.dumps(payload)
                response = await session.post(
                    CONSOLE_RESPONSES_API,
                    headers=headers,
                    data=body,
                    timeout=timeout,
                    stream=stream,
                    proxy=proxy,
                    proxies=proxies,
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
                    and _is_compaction_blob_decode_error(response.status_code, content)
                ):
                    removed = drop_compaction_blobs_from_payload(payload)
                    if removed > 0:
                        compaction_stripped = True
                        logger.warning(
                            f"ConsoleResponsesReverse: dropped {removed} compaction blob(s), retrying request"
                        )
                        continue
                raise UpstreamException(
                    message=f"ConsoleResponsesReverse: request failed, {response.status_code}",
                    details={"status": response.status_code, "body": content[:2000]},
                )

        async def _on_retry(attempt: int, status_code: int, error: Exception, delay: float):
            if active_proxy_key and should_rotate_proxy(status_code):
                rotate_proxy(active_proxy_key)

        def extract_status(e: Exception) -> Optional[int]:
            status = extract_status_for_retry(e)
            if status == 429:
                return None
            return status

        try:
            response = await retry_on_status(
                _do_request,
                extract_status=extract_status,
                on_retry=_on_retry,
            )
        except UpstreamException as exc:
            status = (exc.details or {}).get("status")
            if status == 401:
                try:
                    await TokenService.record_fail(token, status, "console_auth_failed")
                except Exception:
                    pass
            raise

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
