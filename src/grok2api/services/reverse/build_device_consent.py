"""Pure-protocol xAI device consent using SSO cookies (no browser).

Captured 2026-07-10 (empty Chrome profile → login → device-auth):

1. POST https://auth.x.ai/oauth2/device/code  (client_id + scope)
2. Session Cookie: sso=<jwt>; sso-rw=<jwt> on .x.ai / accounts.x.ai / auth.x.ai
3. POST https://auth.x.ai/oauth2/device/verify
     body: user_code=XXXX-XXXX
     → 303 Location: .../oauth2/device/consent?user_code=...
4. POST https://auth.x.ai/oauth2/device/approve
     body: user_code=...&action=allow&principal_type=User&principal_id=
     Cookie: sso / sso-rw
     → 303 Location: .../oauth2/device/done
5. Poll https://auth.x.ai/oauth2/token (device_code grant)
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from urllib.parse import urlparse

from grok2api.core.logger import logger

LogFn = Callable[[str], None]

DEVICE_VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
DEVICE_APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"


def _noop(_: str) -> None:
    return None


def _normalize_proxy(proxy: str | None) -> str | None:
    p = (proxy or "").strip()
    if not p:
        return None
    if p.startswith("socks5://"):
        return "socks5h://" + p[len("socks5://") :]
    if p.startswith("socks4://"):
        return "socks4a://" + p[len("socks4://") :]
    return p


def _normalize_sso(sso_token: str) -> str:
    s = (sso_token or "").strip()
    if s.startswith("sso="):
        s = s[4:]
    return s


def _session(proxy: str | None = None) -> Any:
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("curl_cffi required for protocol device consent") from exc

    sess = curl_requests.Session(impersonate="chrome")
    p = _normalize_proxy(proxy)
    if p:
        sess.proxies = {"http": p, "https": p}
    return sess


def _set_sso_cookies(sess: Any, sso_token: str) -> None:
    sso = _normalize_sso(sso_token)
    for domain in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai", ".auth.x.ai"):
        try:
            sess.cookies.set("sso", sso, domain=domain, path="/")
            sess.cookies.set("sso-rw", sso, domain=domain, path="/")
        except Exception:
            # cookie jar APIs vary; fall back to Cookie header later
            pass


def approve_device_with_sso_protocol(
    *,
    user_code: str,
    sso_token: str,
    proxy: str | None = None,
    timeout: float = 30.0,
    log: LogFn | None = None,
    retries: int = 3,
) -> None:
    """Complete device consent via HTTP only (SSO cookie). Raises on failure."""
    log = log or _noop
    user_code = (user_code or "").strip()
    sso = _normalize_sso(sso_token)
    if not user_code or not sso:
        raise RuntimeError("user_code and sso_token required")

    last_err: BaseException | None = None
    for attempt in range(max(retries, 1)):
        try:
            sess = _session(proxy)
            _set_sso_cookies(sess, sso)
            # Warm session / validate SSO
            warm = sess.get(
                "https://accounts.x.ai/account",
                timeout=timeout,
                allow_redirects=True,
            )
            if "sign-in" in str(getattr(warm, "url", "")):
                raise RuntimeError("SSO cookie rejected (redirected to sign-in)")

            headers = {
                "Origin": "https://accounts.x.ai",
                "Referer": "https://accounts.x.ai/",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                # ensure cookies even if jar domain matching fails
                "Cookie": f"sso={sso}; sso-rw={sso}",
            }

            rv = sess.post(
                DEVICE_VERIFY_URL,
                data={"user_code": user_code},
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
            loc = str(rv.headers.get("location") or "")
            log(f"device verify HTTP {rv.status_code} loc={loc[:120]}")
            if rv.status_code not in (200, 302, 303, 307):
                raise RuntimeError(f"device verify failed HTTP {rv.status_code}: {rv.text[:200]}")
            if "sign-in" in loc:
                raise RuntimeError("device verify requires login (SSO invalid)")

            # Optional: follow to consent page (not strictly required for approve)
            if loc and rv.status_code in (302, 303, 307):
                if loc.startswith("/"):
                    loc = "https://accounts.x.ai" + loc
                try:
                    sess.get(loc, timeout=timeout, allow_redirects=True, headers=headers)
                except Exception as exc:
                    log(f"consent page fetch soft fail: {exc}")

            ra = sess.post(
                DEVICE_APPROVE_URL,
                data={
                    "user_code": user_code,
                    "action": "allow",
                    "principal_type": "User",
                    "principal_id": "",
                },
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
            aloc = str(ra.headers.get("location") or "")
            log(f"device approve HTTP {ra.status_code} loc={aloc[:120]}")
            if ra.status_code not in (200, 302, 303, 307):
                raise RuntimeError(
                    f"device approve failed HTTP {ra.status_code}: {ra.text[:200]}"
                )
            if "sign-in" in aloc:
                raise RuntimeError("device approve requires login (SSO invalid)")
            if "done" not in aloc and ra.status_code not in (200,):
                # still may have succeeded; token poll is source of truth
                log(f"approve unexpected location (will rely on token poll): {aloc}")
            log("device consent protocol OK")
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log(f"protocol consent attempt {attempt + 1}/{retries} failed: {exc}")
            if attempt + 1 >= retries:
                break
            import time

            time.sleep(1.0 * (attempt + 1))
    assert last_err is not None
    raise RuntimeError(f"protocol device consent failed: {last_err}") from last_err


__all__ = [
    "DEVICE_APPROVE_URL",
    "DEVICE_VERIFY_URL",
    "approve_device_with_sso_protocol",
]
