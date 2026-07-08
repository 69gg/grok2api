"""Native passthrough requests to console.x.ai /v1 endpoints."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Mapping, Optional

from curl_cffi.requests import AsyncSession

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger
from grok2api.core.proxy_pool import rotate_proxy, should_rotate_proxy
from grok2api.services.reverse.console_constants import CONSOLE_BASE_URL, CONSOLE_TIMEOUT
from grok2api.services.reverse.utils.headers import build_console_headers
from grok2api.services.reverse.utils.proxy import (
    CONSOLE_PROXY_KEYS,
    build_curl_cffi_proxy_kwargs,
)
from grok2api.services.reverse.utils.retry import RetryContext, extract_status_for_retry


@dataclass(frozen=True)
class ConsoleNativeResponse:
    """Raw upstream response body plus response metadata."""

    body: bytes
    status_code: int
    content_type: str
    headers: Dict[str, str]


_SENSITIVE_RESPONSE_HEADER_NAMES = {
    "authorization",
    "cf_clearance",
    "cookie",
    "proxy-authenticate",
    "proxy-authorization",
    "set-cookie",
    "www-authenticate",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
}


def sanitize_console_response_headers(
    headers: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    """Return upstream response headers without credential-bearing values."""
    if not headers:
        return {}
    safe: Dict[str, str] = {}
    for key, value in headers.items():
        key_str = str(key)
        if key_str.lower() in _SENSITIVE_RESPONSE_HEADER_NAMES:
            safe[key_str] = "<redacted>"
        else:
            safe[key_str] = str(value)
    return safe


class ConsoleNativeReverse:
    """Make native Console API requests with only auth/header rewriting."""

    @staticmethod
    async def _read_body(response: Any) -> bytes:
        for attr_name in ("content", "read", "aread"):
            attr = getattr(response, attr_name, None)
            if attr is None:
                continue
            try:
                value = attr() if callable(attr) else attr
                if inspect.isawaitable(value):
                    value = await value
                if value:
                    return value if isinstance(value, bytes) else str(value).encode()
            except Exception:
                continue
        text_attr = getattr(response, "text", None)
        if text_attr is not None:
            try:
                value = text_attr() if callable(text_attr) else text_attr
                if inspect.isawaitable(value):
                    value = await value
                if value:
                    return value if isinstance(value, bytes) else str(value).encode()
            except Exception:
                pass
        return b""

    @staticmethod
    def _headers_for_request(
        *,
        token: str,
        content_type: Optional[str],
        team_id: Optional[str],
        referer_path: Optional[str],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, str]:
        headers = build_console_headers(
            cookie_token=token,
            content_type=content_type,
            team_id=team_id,
            referer_path=referer_path,
        )
        headers.pop("Authorization", None)
        for key, value in (extra_headers or {}).items():
            lowered = key.lower()
            if lowered in {"authorization", "cookie", "host", "content-length"}:
                continue
            if lowered == "content-type" and content_type is None:
                continue
            headers[key] = value
        return headers

    @staticmethod
    async def request(
        *,
        token: str,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: Optional[str] = "application/json",
        stream: bool = False,
        team_id: Optional[str] = None,
        referer_path: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> ConsoleNativeResponse | AsyncIterator[bytes]:
        url = f"{CONSOLE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        headers = ConsoleNativeReverse._headers_for_request(
            token=token,
            content_type=content_type,
            team_id=team_id,
            referer_path=referer_path,
            extra_headers=extra_headers,
        )
        timeout = float(CONSOLE_TIMEOUT)
        browser = get_config("proxy.browser")
        active_proxy_key: Optional[str] = None
        session = AsyncSession()

        async def _do_request() -> Any:
            nonlocal active_proxy_key
            proxy_kwargs = build_curl_cffi_proxy_kwargs(*CONSOLE_PROXY_KEYS)
            active_proxy_key = proxy_kwargs.active_proxy_key
            request_method = method.upper()
            kwargs = {
                "headers": headers,
                "timeout": timeout,
                "stream": stream,
                "proxy": proxy_kwargs.proxy,
                "proxies": proxy_kwargs.proxies,
                "impersonate": browser,
            }
            if request_method == "GET":
                return await session.get(url, **kwargs)
            if request_method == "POST":
                return await session.post(url, data=body, **kwargs)
            return await session.request(request_method, url, data=body, **kwargs)

        async def _on_transport_retry(status_code: int) -> None:
            if active_proxy_key and should_rotate_proxy(status_code):
                rotate_proxy(active_proxy_key)

        ctx = RetryContext()
        while ctx.attempt <= ctx.max_retry:
            try:
                response = await _do_request()
                if response.status_code == 200:
                    if stream:
                        async def stream_body() -> AsyncIterator[bytes]:
                            try:
                                async for chunk in response.aiter_content():
                                    if chunk:
                                        yield chunk if isinstance(chunk, bytes) else str(chunk).encode()
                            finally:
                                await session.close()

                        return stream_body()
                    body_bytes = await ConsoleNativeReverse._read_body(response)
                    await session.close()
                    return ConsoleNativeResponse(
                        body=body_bytes,
                        status_code=response.status_code,
                        content_type=str(response.headers.get("content-type") or "application/octet-stream"),
                        headers=sanitize_console_response_headers(response.headers),
                    )
                body_bytes = await ConsoleNativeReverse._read_body(response)
                text = body_bytes.decode("utf-8", errors="ignore")
                logger.error(
                    "ConsoleNativeReverse: upstream {} {} body: {}",
                    response.status_code,
                    path,
                    text[:500],
                )
                await session.close()
                raise UpstreamException(
                    message=f"ConsoleNativeReverse: request failed, {response.status_code}",
                    details={
                        "status": response.status_code,
                        "body": text[:2000],
                        "headers": sanitize_console_response_headers(response.headers),
                    },
                )
            except UpstreamException:
                raise
            except Exception as exc:
                status_code = extract_status_for_retry(exc)
                if status_code is None:
                    await session.close()
                    raise
                ctx.record_error(status_code, exc)
                if not ctx.should_retry(status_code, exc):
                    await session.close()
                    raise
                delay = ctx.calculate_delay(status_code)
                if ctx.total_delay + delay > ctx.retry_budget:
                    await session.close()
                    raise
                ctx.record_delay(delay)
                logger.warning(
                    "ConsoleNativeReverse transport retry {}/{} for status {}, waiting {:.2f}s",
                    ctx.attempt,
                    ctx.max_retry,
                    status_code,
                    delay,
                )
                await _on_transport_retry(status_code)
                await asyncio.sleep(delay)
                continue

        await session.close()
        raise UpstreamException(
            message="ConsoleNativeReverse: request failed after transport retries",
            details={"status": ctx.last_status},
        )


__all__ = [
    "ConsoleNativeResponse",
    "ConsoleNativeReverse",
    "sanitize_console_response_headers",
]
