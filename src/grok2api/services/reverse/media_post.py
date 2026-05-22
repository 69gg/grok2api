"""
Reverse interface: media post create.
"""

import hashlib
import orjson
from typing import Any, Dict
from urllib.parse import urlparse
from curl_cffi.requests import AsyncSession

from grok2api.core.logger import logger
from grok2api.core.config import get_config
from grok2api.core.proxy_pool import (
    build_http_proxies,
    get_current_proxy_from,
    rotate_proxy,
    should_rotate_proxy,
)
from grok2api.core.exceptions import UpstreamException
from grok2api.services.token.service import TokenService
from grok2api.services.reverse.utils.headers import build_headers
from grok2api.services.reverse.utils.retry import retry_on_status

MEDIA_POST_API = "https://grok.com/rest/media/post/create"


def _token_hint(token: str) -> str:
    raw = str(token or "").strip()
    if raw.startswith("sso="):
        raw = raw[4:]
    if not raw:
        return "empty"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _redact_proxy_url(proxy_url: str) -> str:
    raw = str(proxy_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return raw
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _sanitize_headers(headers: Any) -> Dict[str, str]:
    safe: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        key_text = str(key)
        key_lower = key_text.lower()
        if key_lower in {"cookie", "set-cookie", "authorization", "proxy-authorization"}:
            safe[key_text] = "<redacted>"
        else:
            safe[key_text] = str(value)
    return safe


def _payload_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(payload.get("prompt") or "")
    media_url = str(payload.get("mediaUrl") or "")
    return {
        "mediaType": payload.get("mediaType"),
        "hasPrompt": bool(prompt),
        "promptLen": len(prompt),
        "promptPreview": prompt[:160],
        "hasMediaUrl": bool(media_url),
        "mediaUrlPreview": media_url[:160],
    }


class MediaPostReverse:
    """/rest/media/post/create reverse interface."""

    @staticmethod
    async def request(
        session: AsyncSession,
        token: str,
        mediaType: str,
        mediaUrl: str,
        prompt: str = "",
    ) -> Any:
        """Create media post in Grok.

        Args:
            session: AsyncSession, the session to use for the request.
            token: str, the SSO token.
            mediaType: str, the media type.
            mediaUrl: str, the media URL.

        Returns:
            Any: The response from the request.
        """
        try:
            # Build headers
            headers = build_headers(
                cookie_token=token,
                content_type="application/json",
                origin="https://grok.com",
                referer=(
                    "https://grok.com/imagine"
                    if mediaType == "MEDIA_POST_TYPE_VIDEO"
                    else "https://grok.com"
                ),
            )

            # Build payload
            payload = {"mediaType": mediaType}
            if mediaUrl:
                payload["mediaUrl"] = mediaUrl
            if prompt:
                payload["prompt"] = prompt

            # Curl Config
            timeout = get_config("video.timeout")
            browser = get_config("proxy.browser")
            active_proxy_key = None
            active_proxy_url = ""
            token_hint = _token_hint(token)
            payload_info = _payload_summary(payload)
            request_id = str(headers.get("x-xai-request-id", ""))
            cookie_value = str(headers.get("Cookie", ""))
            cookie_flags = {
                "hasSso": "sso=" in cookie_value,
                "hasSsoRw": "sso-rw=" in cookie_value,
                "hasCfClearance": "cf_clearance=" in cookie_value,
                "cookieLen": len(cookie_value),
            }

            async def _do_request():
                nonlocal active_proxy_key, active_proxy_url
                active_proxy_key, proxy_url = get_current_proxy_from("proxy.base_proxy_url")
                active_proxy_url = proxy_url
                proxies = build_http_proxies(proxy_url)
                proxy_display = _redact_proxy_url(proxy_url)
                logger.debug(
                    "MediaPostReverse request: token_hint={}, request_id={}, proxy_key={}, proxy_target={}, cookie_flags={}, payload={}",
                    token_hint,
                    request_id,
                    active_proxy_key,
                    proxy_display,
                    cookie_flags,
                    payload_info,
                )
                response = await session.post(
                    MEDIA_POST_API,
                    headers=headers,
                    data=orjson.dumps(payload),
                    timeout=timeout,
                    proxies=proxies,
                    impersonate=browser,
                )

                if response.status_code != 200:
                    content = ""
                    try:
                        content = await response.text()
                    except Exception:
                        pass
                    response_headers = _sanitize_headers(getattr(response, "headers", None))
                    content_type = str(response_headers.get("content-type", ""))
                    retry_after = (
                        response_headers.get("Retry-After")
                        or response_headers.get("retry-after")
                    )
                    logger.error(
                        "MediaPostReverse: Media post create failed, status={}, token_hint={}, request_id={}, proxy_key={}, proxy_target={}, content_type={}, retry_after={}, payload={}, headers={}, body={}",
                        response.status_code,
                        token_hint,
                        request_id,
                        active_proxy_key,
                        proxy_display,
                        content_type,
                        retry_after,
                        payload_info,
                        response_headers,
                        content[:1000],
                        extra={"error_type": "UpstreamException"},
                    )
                    raise UpstreamException(
                        message=f"MediaPostReverse: Media post create failed, {response.status_code}",
                        details={
                            "status": response.status_code,
                            "body": content,
                            "headers": response_headers,
                            "content_type": content_type,
                            "retry_after": retry_after,
                            "proxy_key": active_proxy_key,
                            "proxy_target": proxy_display,
                            "request_id": request_id,
                            "token_hint": token_hint,
                            "payload": payload_info,
                            "cookie_flags": cookie_flags,
                        },
                    )

                return response

            async def _on_retry(attempt: int, status_code: int, error: Exception, delay: float):
                rotated_to = ""
                if active_proxy_key and should_rotate_proxy(status_code):
                    rotated_to = _redact_proxy_url(rotate_proxy(active_proxy_key))
                logger.warning(
                    "MediaPostReverse retry: attempt={}, status={}, delay={:.2f}s, token_hint={}, request_id={}, proxy_key={}, proxy_target={}, rotated_to={}",
                    attempt,
                    status_code,
                    delay,
                    token_hint,
                    request_id,
                    active_proxy_key,
                    _redact_proxy_url(active_proxy_url),
                    rotated_to,
                )

            return await retry_on_status(_do_request, on_retry=_on_retry)

        except Exception as e:
            # Handle upstream exception
            if isinstance(e, UpstreamException):
                status = None
                if e.details and "status" in e.details:
                    status = e.details["status"]
                else:
                    status = getattr(e, "status_code", None)
                if status == 401:
                    try:
                        await TokenService.record_fail(token, status, "media_post_auth_failed")
                    except Exception:
                        pass
                raise

            # Handle other non-upstream exceptions
            logger.error(
                f"MediaPostReverse: Media post create failed, {str(e)}",
                extra={"error_type": type(e).__name__},
            )
            raise UpstreamException(
                message=f"MediaPostReverse: Media post create failed, {str(e)}",
                details={"status": 502, "error": str(e)},
            )


__all__ = ["MediaPostReverse"]
