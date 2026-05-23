"""Shared transport helpers for console.x.ai Voice REST APIs."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, TypeVar
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger
from grok2api.core.proxy_pool import get_current_proxy_from, rotate_proxy, should_rotate_proxy
from grok2api.services.reverse.utils.retry import RetryContext, extract_status_for_retry

T = TypeVar("T")


def normalize_proxy(proxy_url: str) -> str:
    if not proxy_url:
        return proxy_url
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme == "socks5":
        return proxy_url.replace("socks5://", "socks5h://", 1)
    if scheme == "socks4":
        return proxy_url.replace("socks4://", "socks4a://", 1)
    return proxy_url


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
    active_proxy_key, base_proxy = get_current_proxy_from("proxy.base_proxy_url")
    if not base_proxy:
        return active_proxy_key, None, None
    normalized = normalize_proxy(base_proxy)
    scheme = urlparse(normalized).scheme.lower()
    if scheme.startswith("socks"):
        return active_proxy_key, None, normalized
    return active_proxy_key, {"http": normalized, "https": normalized}, None


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
            active_proxy_key, proxies, proxy = proxy_kwargs()
            response = await do_post(session, proxy, proxies, browser)
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
