"""Native passthrough to cli-chat-proxy.grok.com (header rewrite only)."""

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
from grok2api.services.reverse.build_constants import (
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_HEADERS,
)
from grok2api.services.reverse.utils.proxy import (
    CONSOLE_PROXY_KEYS,
    build_curl_cffi_proxy_kwargs,
)
from grok2api.services.reverse.utils.retry import extract_status_for_retry

# CLI 与 Console 共用代理：console_proxy_url → base_proxy_url
BUILD_PROXY_KEYS = CONSOLE_PROXY_KEYS
BUILD_TIMEOUT = 300.0
BUILD_TRANSPORT_MAX_RETRY = 3
_BODY_LOG_PREVIEW_CHARS = 6000
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
}


@dataclass(frozen=True)
class BuildNativeResponse:
    body: bytes
    status_code: int
    content_type: str
    headers: Dict[str, str]


_SENSITIVE_RESPONSE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authenticate",
    "proxy-authorization",
    "www-authenticate",
    "x-api-key",
}


def _sanitize_headers(headers: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    if not headers:
        return {}
    safe: Dict[str, str] = {}
    for key, value in headers.items():
        key_str = str(key)
        if key_str.lower() in _SENSITIVE_RESPONSE_HEADER_NAMES | _SENSITIVE_HEADER_NAMES:
            # Keep scheme only for Authorization so 400 debugging can still see auth mode
            if key_str.lower() == "authorization":
                raw = str(value)
                if raw.lower().startswith("bearer "):
                    token = raw[7:].strip()
                    safe[key_str] = f"Bearer <redacted len={len(token)} prefix={token[:12]}...>"
                else:
                    safe[key_str] = "<redacted>"
            else:
                safe[key_str] = "<redacted>"
        else:
            safe[key_str] = str(value)
    return safe


def _truncate_for_log(value: str, limit: int = _BODY_LOG_PREVIEW_CHARS) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated {len(value) - limit} chars>"


def _body_log_preview(body: bytes | None, content_type: Optional[str] = None) -> str:
    if not body:
        return "<empty>"
    text = body.decode("utf-8", errors="replace")
    ct = (content_type or "").lower()
    if "json" in ct or text.lstrip()[:1] in {"{", "["}:
        try:
            parsed = orjson.loads(body)
            text = orjson.dumps(parsed, option=orjson.OPT_INDENT_2).decode("utf-8")
        except Exception:
            pass
    return _truncate_for_log(text)


def _log_upstream_400(
    *,
    method: str,
    url: str,
    path: str,
    request_headers: Dict[str, str],
    request_body: bytes | None,
    content_type: Optional[str],
    status: int,
    response_headers: Mapping[str, Any] | None,
    response_body: bytes | None,
    proxy_key: Optional[str],
    stream: bool,
) -> None:
    """Verbose request/response dump for upstream 400 diagnostics."""
    req_headers = _sanitize_headers(request_headers)
    resp_headers = _sanitize_headers(response_headers)
    logger.error(
        "BuildNativeReverse HTTP {} detail: method={} url={} path={} stream={} "
        "proxy_key={} request_headers={} request_body={} response_headers={} response_body={}",
        status,
        method,
        url,
        path,
        stream,
        proxy_key,
        req_headers,
        _body_log_preview(request_body, content_type),
        resp_headers,
        _body_log_preview(
            response_body,
            str((response_headers or {}).get("content-type") or ""),
        ),
    )


def resolve_build_base_url() -> str:
    base = str(get_config("build.base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).strip()
    base = base.rstrip("/")
    if base.endswith("cli-chat-proxy.grok.com"):
        base = base + "/v1"
    if "api.x.ai" in base:
        logger.warning(
            "build.base_url points at api.x.ai; free Grok 4.5 requires cli-chat-proxy.grok.com"
        )
    return base


def build_cli_headers(
    access_token: str,
    *,
    content_type: Optional[str] = "application/json",
    stream: bool = False,
    conv_id: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    headers: Dict[str, str] = {
        **DEFAULT_CLIENT_HEADERS,
        "Authorization": f"Bearer {access_token.strip()}",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if content_type:
        headers["Content-Type"] = content_type
    if conv_id:
        headers["x-grok-conv-id"] = conv_id
    for key, value in (extra_headers or {}).items():
        lowered = key.lower()
        if lowered in {"authorization", "cookie", "host", "content-length"}:
            continue
        headers[key] = value
    return headers


class BuildNativeReverse:
    """Make native CLI proxy requests with only auth/header rewriting."""

    @staticmethod
    async def _read_body(response: Any) -> bytes:
        # Prefer streaming iterators first: curl_cffi stream=True responses often
        # leave .content/.text empty until chunks are consumed.
        aiter = getattr(response, "aiter_content", None)
        if aiter is not None:
            try:
                chunks: list[bytes] = []
                async for chunk in aiter():
                    if not chunk:
                        continue
                    chunks.append(
                        chunk if isinstance(chunk, bytes) else str(chunk).encode()
                    )
                if chunks:
                    return b"".join(chunks)
            except Exception:
                pass
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
    async def request(
        *,
        access_token: str,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: Optional[str] = "application/json",
        stream: bool = False,
        conv_id: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        base_url: Optional[str] = None,
    ) -> BuildNativeResponse | AsyncIterator[bytes]:
        base = (base_url or resolve_build_base_url()).rstrip("/")
        # base already ends with /v1; accept either "chat/completions" or "/v1/chat/completions"
        rel = path.lstrip("/")
        if rel.startswith("v1/"):
            rel = rel[3:]
        url = f"{base}/{rel}"
        headers = build_cli_headers(
            access_token,
            content_type=content_type,
            stream=stream,
            conv_id=conv_id,
            extra_headers=extra_headers,
        )
        request_method = method.upper()
        timeout = float(get_config("build.timeout_sec", BUILD_TIMEOUT) or BUILD_TIMEOUT)
        browser = get_config("proxy.browser")
        active_proxy_key: Optional[str] = None
        session = AsyncSession()

        async def _do_request() -> Any:
            nonlocal active_proxy_key
            proxy_kwargs = build_curl_cffi_proxy_kwargs(*BUILD_PROXY_KEYS)
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

        configured_max_retry = get_config(
            "build.transport_max_retry", BUILD_TRANSPORT_MAX_RETRY
        )
        try:
            max_retry = max(0, int(configured_max_retry))
        except (TypeError, ValueError):
            max_retry = BUILD_TRANSPORT_MAX_RETRY
        attempt = 0
        while attempt <= max_retry:
            try:
                response = await _do_request()
                status = int(response.status_code)
                if 200 <= status < 300:
                    if stream:

                        async def stream_body() -> AsyncIterator[bytes]:
                            try:
                                async for chunk in response.aiter_content():
                                    if chunk:
                                        yield (
                                            chunk
                                            if isinstance(chunk, bytes)
                                            else str(chunk).encode()
                                        )
                            finally:
                                await session.close()

                        return stream_body()
                    body_bytes = await BuildNativeReverse._read_body(response)
                    await session.close()
                    return BuildNativeResponse(
                        body=body_bytes,
                        status_code=status,
                        content_type=str(
                            response.headers.get("content-type") or "application/octet-stream"
                        ),
                        headers=_sanitize_headers(response.headers),
                    )

                body_bytes = await BuildNativeReverse._read_body(response)
                text = body_bytes.decode("utf-8", errors="ignore")
                await session.close()
                if status == 400:
                    _log_upstream_400(
                        method=request_method,
                        url=url,
                        path=path,
                        request_headers=headers,
                        request_body=body,
                        content_type=content_type,
                        status=status,
                        response_headers=response.headers,
                        response_body=body_bytes,
                        proxy_key=active_proxy_key,
                        stream=stream,
                    )
                else:
                    logger.error(
                        "BuildNativeReverse upstream failure: method={} path={} status={} body={}",
                        request_method,
                        path,
                        status,
                        text[:1000],
                    )
                raise UpstreamException(
                    message=f"BuildNativeReverse: request failed, {status}",
                    details={
                        "status": status,
                        "body": text[:2000],
                        "headers": _sanitize_headers(response.headers),
                    },
                )
            except asyncio.CancelledError:
                await session.close()
                raise
            except UpstreamException:
                raise
            except Exception as exc:
                status_code = extract_status_for_retry(exc)
                if status_code is None or attempt >= max_retry:
                    await session.close()
                    raise
                await session.close()
                if active_proxy_key and should_rotate_proxy(status_code):
                    rotate_proxy(active_proxy_key)
                await asyncio.sleep(min(0.5 * (2**attempt), 5.0))
                attempt += 1
                session = AsyncSession()
        await session.close()
        raise UpstreamException(message="BuildNativeReverse: exhausted retries")


__all__ = [
    "BUILD_PROXY_KEYS",
    "BuildNativeResponse",
    "BuildNativeReverse",
    "build_cli_headers",
    "resolve_build_base_url",
]
