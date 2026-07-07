"""Shared transport helpers for console.x.ai Voice REST APIs."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, TypeVar

from curl_cffi.requests import AsyncSession

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger
from grok2api.core.proxy_pool import rotate_proxy, should_rotate_proxy
from grok2api.services.reverse.utils.proxy import (
    CONSOLE_PROXY_KEYS,
    CurlCffiProxyKwargs,
    build_curl_cffi_proxy_kwargs,
    normalize_curl_proxy,
)
from grok2api.services.reverse.utils.retry import RetryContext, extract_status_for_retry

T = TypeVar("T")


def normalize_proxy(proxy_url: str) -> str:
    return normalize_curl_proxy(proxy_url)


async def read_response_body(response: Any) -> bytes:
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
    return b""


async def read_error_text(response: Any) -> str:
    body = await read_response_body(response)
    if not body:
        return ""
    return body.decode("utf-8", errors="ignore")


def proxy_kwargs() -> Tuple[Optional[str], Optional[Dict[str, str]], Optional[str]]:
    kwargs = build_curl_cffi_proxy_kwargs(*CONSOLE_PROXY_KEYS)
    return kwargs.active_proxy_key, kwargs.proxies, kwargs.proxy


async def execute_console_voice_request(
    *,
    label: str,
    process: Callable[[Any], Awaitable[T]],
    do_post: Callable[[AsyncSession, Optional[str], Optional[Dict[str, str]], Optional[str]], Any],
) -> T:
    browser = get_config("proxy.browser")
    session = AsyncSession()
    active_proxy_key: Optional[str] = None
    ctx = RetryContext()
    response = None

    async def _on_transport_retry(status_code: int) -> None:
        nonlocal active_proxy_key
        if active_proxy_key and should_rotate_proxy(status_code):
            rotate_proxy(active_proxy_key)

    while ctx.attempt <= ctx.max_retry:
        try:
            proxy_settings: CurlCffiProxyKwargs = build_curl_cffi_proxy_kwargs(
                *CONSOLE_PROXY_KEYS
            )
            active_proxy_key = proxy_settings.active_proxy_key
            response = await do_post(
                session,
                proxy_settings.proxy,
                proxy_settings.proxies,
                browser,
            )
            if response.status_code == 200:
                try:
                    return await process(response)
                finally:
                    await session.close()
            content = await read_error_text(response)
            logger.error(f"{label}: upstream {response.status_code} body: {content[:500]}")
            raise UpstreamException(
                message=f"{label}: request failed, {response.status_code}",
                details={"status": response.status_code, "body": content[:2000]},
            )
        except UpstreamException:
            await session.close()
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
                f"{label} transport retry {ctx.attempt}/{ctx.max_retry} "
                f"for status {status_code}, waiting {delay:.2f}s"
            )
            await _on_transport_retry(status_code)
            await asyncio.sleep(delay)
            continue

    await session.close()
    raise UpstreamException(
        message=f"{label}: request failed after transport retries",
        details={"status": ctx.last_status},
    )


__all__ = [
    "execute_console_voice_request",
    "normalize_proxy",
    "read_error_text",
    "read_response_body",
]
