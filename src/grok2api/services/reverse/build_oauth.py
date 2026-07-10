"""OIDC device-code + refresh for free Grok Build (cli-chat-proxy)."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import requests

from grok2api.core.logger import logger
from grok2api.services.reverse.build_constants import (
    CLIENT_ID,
    DEFAULT_EXPIRES_IN,
    DEVICE_CODE_URL,
    SCOPE,
    TOKEN_ENDPOINT,
)

LogFn = Callable[[str], None]


def _noop_log(_: str) -> None:
    return None


def jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    pad = "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(parts[1] + pad))


def expired_ms_from_access_token(access_token: str) -> int:
    """Return access token exp as unix milliseconds."""
    try:
        pl = jwt_payload(access_token)
        exp = int(pl["exp"])
        return exp * 1000
    except Exception:
        return int(time.time() * 1000) + DEFAULT_EXPIRES_IN * 1000


def sub_from_access_token(access_token: str) -> str:
    try:
        pl = jwt_payload(access_token)
        return str(pl.get("sub") or pl.get("principal_id") or "").strip()
    except Exception:
        return ""


@dataclass
class DeviceCodeSession:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    id_token: str | None
    token_type: str
    expires_in: int
    expired_at_ms: int
    sub: str
    raw: dict[str, Any]


class OAuthDeviceError(RuntimeError):
    pass


class OAuthRefreshError(RuntimeError):
    pass


def _normalize_proxy(proxy: str | None) -> str | None:
    """Normalize proxy URL for curl_cffi / requests (socks5 → socks5h)."""
    p = (proxy or "").strip()
    if not p:
        return None
    if p.startswith("socks5://"):
        return "socks5h://" + p[len("socks5://") :]
    if p.startswith("socks4://"):
        return "socks4a://" + p[len("socks4://") :]
    return p


def _post_form(
    url: str,
    form: dict[str, str],
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
    retries: int = 5,
) -> tuple[int, dict[str, Any] | str]:
    """POST application/x-www-form-urlencoded; prefer curl_cffi; multi-retry on SSL/net."""
    proxy_url = _normalize_proxy(proxy)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "grok2api-cli-oauth/1.0",
    }
    last_err: BaseException | None = None
    attempts = max(int(retries), 0) + 1
    for i in range(attempts):
        try:
            # Prefer curl_cffi — same stack as reverse layer.
            try:
                from curl_cffi import requests as curl_requests

                kwargs: dict[str, Any] = {
                    "data": form,
                    "headers": headers,
                    "timeout": timeout,
                    "impersonate": "chrome",
                }
                if proxy_url:
                    kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
                resp = curl_requests.post(url, **kwargs)
            except ImportError:
                proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
                resp = requests.post(
                    url,
                    data=form,
                    headers=headers,
                    proxies=proxies,
                    timeout=timeout,
                )
            try:
                body: dict[str, Any] | str = resp.json()
            except Exception:
                body = getattr(resp, "text", "") or ""
            # Retry soft upstream rate limits on device code endpoint
            if int(resp.status_code) == 429 and i + 1 < attempts:
                wait = 2.0 * (i + 1)
                logger.warning(
                    "OIDC HTTP 429 on {} — retry {}/{} in {}s",
                    url,
                    i + 1,
                    attempts,
                    wait,
                )
                time.sleep(wait)
                continue
            return int(resp.status_code), body
        except Exception as exc:  # noqa: BLE001 — network/TLS blips
            last_err = exc
            wait = min(1.2 * (i + 1), 8.0)
            logger.warning(
                "OIDC request error on {} attempt {}/{}: {} — retry in {}s",
                url,
                i + 1,
                attempts,
                exc,
                wait,
            )
            if i + 1 >= attempts:
                break
            time.sleep(wait)
    raise OAuthRefreshError(f"token request failed: {last_err}") from last_err


def request_device_code(
    *,
    client_id: str = CLIENT_ID,
    scope: str = SCOPE,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> DeviceCodeSession:
    status, body = _post_form(
        DEVICE_CODE_URL,
        {"client_id": client_id, "scope": scope},
        proxy=proxy,
        timeout=timeout,
    )
    if status != 200 or not isinstance(body, dict):
        raise OAuthDeviceError(f"device code request failed HTTP {status}: {body!r}")
    device_code = str(body.get("device_code") or "").strip()
    user_code = str(body.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise OAuthDeviceError(f"device code response missing fields: {body}")
    vuri = str(body.get("verification_uri") or "https://accounts.x.ai/oauth2/device").strip()
    vcomplete = str(
        body.get("verification_uri_complete") or f"{vuri}?user_code={user_code}"
    ).strip()
    return DeviceCodeSession(
        device_code=device_code,
        user_code=user_code,
        verification_uri=vuri,
        verification_uri_complete=vcomplete,
        expires_in=int(body.get("expires_in") or 1800),
        interval=max(int(body.get("interval") or 5), 1),
    )


def _bundle_from_token_response(body: dict[str, Any]) -> TokenBundle:
    access = str(body.get("access_token") or "").strip()
    refresh = str(body.get("refresh_token") or "").strip()
    if not access:
        raise OAuthRefreshError("token response missing access_token")
    if not refresh:
        raise OAuthRefreshError("token response missing refresh_token")
    expires_in = int(body.get("expires_in") or DEFAULT_EXPIRES_IN)
    try:
        expired_at_ms = expired_ms_from_access_token(access)
    except Exception:
        expired_at_ms = int(time.time() * 1000) + expires_in * 1000
    return TokenBundle(
        access_token=access,
        refresh_token=refresh,
        id_token=(str(body["id_token"]).strip() if body.get("id_token") else None),
        token_type=str(body.get("token_type") or "Bearer"),
        expires_in=expires_in,
        expired_at_ms=expired_at_ms,
        sub=sub_from_access_token(access),
        raw=body,
    )


def poll_device_token(
    device_code: str,
    *,
    client_id: str = CLIENT_ID,
    interval: int = 5,
    expires_in: int = 1800,
    proxy: str | None = None,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> TokenBundle:
    """Poll until device authorized or expired."""
    log = log or _noop_log
    deadline = time.time() + max(expires_in - 5, 30)
    sleep_for = max(interval, 1)
    while time.time() < deadline:
        if cancel and cancel():
            raise OAuthDeviceError("cancelled")
        status, body = _post_form(
            TOKEN_ENDPOINT,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            },
            proxy=proxy,
        )
        if status == 200 and isinstance(body, dict) and body.get("access_token"):
            return _bundle_from_token_response(body)
        err = ""
        if isinstance(body, dict):
            err = str(body.get("error") or "")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                sleep_for = min(sleep_for + 5, 30)
            log(f"oauth poll: {err} (sleep {sleep_for}s)")
            time.sleep(sleep_for)
            continue
        if err in ("expired_token", "access_denied"):
            raise OAuthDeviceError(f"device auth failed: {err}")
        if status == 400 and err:
            raise OAuthDeviceError(f"device auth token error: {err}: {body}")
        log(f"oauth poll unexpected HTTP {status}: {body!r}")
        time.sleep(sleep_for)
    raise OAuthDeviceError("device auth timed out waiting for user approval")


def refresh_tokens(
    refresh_token: str,
    *,
    client_id: str = CLIENT_ID,
    token_endpoint: str = TOKEN_ENDPOINT,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> TokenBundle:
    refresh_token = (refresh_token or "").strip()
    if not refresh_token:
        raise OAuthRefreshError("refresh_token is required")
    status, body = _post_form(
        token_endpoint or TOKEN_ENDPOINT,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        proxy=proxy,
        timeout=timeout,
    )
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        raise OAuthRefreshError(f"refresh failed HTTP {status}: {body!r}")
    # Some responses omit rotated refresh_token
    if not body.get("refresh_token"):
        body = {**body, "refresh_token": refresh_token}
    return _bundle_from_token_response(body)


_refresh_locks: dict[str, asyncio.Lock] = {}
_refresh_locks_guard = asyncio.Lock()


async def _lock_for(refresh_token: str) -> asyncio.Lock:
    async with _refresh_locks_guard:
        lock = _refresh_locks.get(refresh_token)
        if lock is None:
            lock = asyncio.Lock()
            _refresh_locks[refresh_token] = lock
        return lock


async def refresh_tokens_async(
    refresh_token: str,
    *,
    client_id: str = CLIENT_ID,
    token_endpoint: str = TOKEN_ENDPOINT,
    proxy: str | None = None,
) -> TokenBundle:
    """Singleflight refresh per refresh_token."""
    lock = await _lock_for(refresh_token)
    async with lock:
        return await asyncio.to_thread(
            refresh_tokens,
            refresh_token,
            client_id=client_id,
            token_endpoint=token_endpoint,
            proxy=proxy,
        )


def rfc3339_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "DeviceCodeSession",
    "OAuthDeviceError",
    "OAuthRefreshError",
    "TokenBundle",
    "expired_ms_from_access_token",
    "jwt_payload",
    "poll_device_token",
    "refresh_tokens",
    "refresh_tokens_async",
    "request_device_code",
    "rfc3339_now",
    "sub_from_access_token",
]
