"""Native passthrough requests to console.x.ai /v1 endpoints."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Mapping, Optional

import orjson
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
_SENSITIVE_REQUEST_HEADER_NAMES = _SENSITIVE_RESPONSE_HEADER_NAMES | {
    "x-session-token",
}
_SENSITIVE_JSON_KEY_NAMES = {
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "cf_clearance",
    "cookie",
    "cookies",
    "id_token",
    "password",
    "refresh_token",
    "secret",
    "sso",
    "sso-rw",
    "token",
    "x-api-key",
}
_SENSITIVE_JSON_KEY_SUFFIXES = (
    "_api_key",
    "_cookie",
    "_password",
    "_secret",
    "_token",
)
_BODY_LOG_PREVIEW_CHARS = 4000
_STRING_LOG_PREVIEW_CHARS = 1000
_ERROR_LOG_PREVIEW_CHARS = 1000


def _truncate_for_log(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}...<truncated {omitted} chars>"


def _sanitize_headers(
    headers: Optional[Mapping[str, Any]],
    *,
    sensitive_names: set[str],
) -> Dict[str, str]:
    if not headers:
        return {}
    safe: Dict[str, str] = {}
    for key, value in headers.items():
        key_str = str(key)
        if key_str.lower() in sensitive_names:
            safe[key_str] = "<redacted>"
        else:
            safe[key_str] = str(value)
    return safe


def _is_sensitive_json_key(key: Optional[str]) -> bool:
    if not key:
        return False
    normalized = key.strip().lower().replace(" ", "_")
    return normalized in _SENSITIVE_JSON_KEY_NAMES or normalized.endswith(
        _SENSITIVE_JSON_KEY_SUFFIXES
    )


def _sanitize_json_value(value: Any, *, key: Optional[str] = None) -> Any:
    if _is_sensitive_json_key(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(child_key): _sanitize_json_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, str):
        return _truncate_for_log(value, _STRING_LOG_PREVIEW_CHARS)
    return value


def _looks_like_json_body(content_type: Optional[str], text: str) -> bool:
    if content_type and "json" in content_type.lower():
        return True
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def build_console_body_log_preview(
    body: bytes | None,
    content_type: Optional[str],
) -> str:
    """Return a bounded, credential-redacted body preview for diagnostics."""
    if not body:
        return "<empty>"
    text = body.decode("utf-8", errors="replace")
    if _looks_like_json_body(content_type, text):
        try:
            parsed = orjson.loads(body)
            sanitized = _sanitize_json_value(parsed)
            text = orjson.dumps(sanitized).decode("utf-8", errors="replace")
        except orjson.JSONDecodeError:
            pass
    return _truncate_for_log(text, _BODY_LOG_PREVIEW_CHARS)


def sanitize_console_response_headers(
    headers: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    """Return upstream response headers without credential-bearing values."""
    return _sanitize_headers(headers, sensitive_names=_SENSITIVE_RESPONSE_HEADER_NAMES)


def sanitize_console_request_headers(
    headers: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    """Return request headers without credential-bearing values."""
    return _sanitize_headers(headers, sensitive_names=_SENSITIVE_REQUEST_HEADER_NAMES)


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
        request_method = method.upper()
        request_headers_preview = sanitize_console_request_headers(headers)
        request_body_preview = build_console_body_log_preview(body, content_type)
        timeout = float(CONSOLE_TIMEOUT)
        browser = get_config("proxy.browser")
        active_proxy_key: Optional[str] = None
        session = AsyncSession()

        async def _do_request() -> Any:
            nonlocal active_proxy_key
            proxy_kwargs = build_curl_cffi_proxy_kwargs(*CONSOLE_PROXY_KEYS)
            active_proxy_key = proxy_kwargs.active_proxy_key
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
                response_headers = sanitize_console_response_headers(response.headers)
                response_body_preview = _truncate_for_log(text, _BODY_LOG_PREVIEW_CHARS)
                logger.error(
                    "ConsoleNativeReverse upstream failure: method={} path={} status={} "
                    "stream={} content_type={} proxy_key={} team_id_present={} "
                    "request_headers={} request_body={} response_headers={} response_body={}",
                    request_method,
                    path,
                    response.status_code,
                    stream,
                    content_type,
                    active_proxy_key,
                    bool(team_id),
                    request_headers_preview,
                    request_body_preview,
                    response_headers,
                    response_body_preview or "<empty>",
                )
                logger.bind(
                    console_native={
                        "method": request_method,
                        "path": path,
                        "status": response.status_code,
                        "stream": stream,
                        "content_type": content_type,
                        "proxy_key": active_proxy_key,
                        "team_id_present": bool(team_id),
                        "request_headers": request_headers_preview,
                        "request_body": request_body_preview,
                        "response_headers": response_headers,
                        "response_body": response_body_preview or "<empty>",
                    }
                ).debug(
                    "ConsoleNativeReverse upstream failure detail: {} {} {}",
                    request_method,
                    path,
                    response.status_code,
                )
                await session.close()
                raise UpstreamException(
                    message=f"ConsoleNativeReverse: request failed, {response.status_code}",
                    details={
                        "status": response.status_code,
                        "body": text[:2000],
                        "headers": response_headers,
                    },
                )
            except UpstreamException:
                raise
            except Exception as exc:
                status_code = extract_status_for_retry(exc)
                if status_code is None:
                    logger.error(
                        "ConsoleNativeReverse transport failure: method={} path={} "
                        "stream={} proxy_key={} error_type={} error={}",
                        request_method,
                        path,
                        stream,
                        active_proxy_key,
                        type(exc).__name__,
                        _truncate_for_log(str(exc), _ERROR_LOG_PREVIEW_CHARS),
                    )
                    await session.close()
                    raise
                ctx.record_error(status_code, exc)
                if not ctx.should_retry(status_code, exc):
                    logger.error(
                        "ConsoleNativeReverse transport non-retryable: method={} path={} "
                        "status={} attempt={}/{} stream={} proxy_key={} error_type={} error={}",
                        request_method,
                        path,
                        status_code,
                        ctx.attempt,
                        ctx.max_retry,
                        stream,
                        active_proxy_key,
                        type(exc).__name__,
                        _truncate_for_log(str(exc), _ERROR_LOG_PREVIEW_CHARS),
                    )
                    await session.close()
                    raise
                delay = ctx.calculate_delay(status_code)
                if ctx.total_delay + delay > ctx.retry_budget:
                    logger.error(
                        "ConsoleNativeReverse transport retry budget exhausted: method={} path={} "
                        "status={} attempt={}/{} stream={} proxy_key={} total_delay={:.2f}s "
                        "next_delay={:.2f}s budget={:.2f}s error_type={} error={}",
                        request_method,
                        path,
                        status_code,
                        ctx.attempt,
                        ctx.max_retry,
                        stream,
                        active_proxy_key,
                        ctx.total_delay,
                        delay,
                        ctx.retry_budget,
                        type(exc).__name__,
                        _truncate_for_log(str(exc), _ERROR_LOG_PREVIEW_CHARS),
                    )
                    await session.close()
                    raise
                ctx.record_delay(delay)
                logger.warning(
                    "ConsoleNativeReverse transport retry {}/{} for status {}, waiting {:.2f}s: "
                    "method={} path={} stream={} proxy_key={} error_type={} error={}",
                    ctx.attempt,
                    ctx.max_retry,
                    status_code,
                    delay,
                    request_method,
                    path,
                    stream,
                    active_proxy_key,
                    type(exc).__name__,
                    _truncate_for_log(str(exc), _ERROR_LOG_PREVIEW_CHARS),
                )
                await _on_transport_retry(status_code)
                await asyncio.sleep(delay)
                continue

        await session.close()
        logger.error(
            "ConsoleNativeReverse request failed after transport retries: method={} path={} "
            "last_status={} stream={} proxy_key={}",
            request_method,
            path,
            ctx.last_status,
            stream,
            active_proxy_key,
        )
        raise UpstreamException(
            message="ConsoleNativeReverse: request failed after transport retries",
            details={"status": ctx.last_status},
        )


__all__ = [
    "ConsoleNativeResponse",
    "ConsoleNativeReverse",
    "build_console_body_log_preview",
    "sanitize_console_request_headers",
    "sanitize_console_response_headers",
]
