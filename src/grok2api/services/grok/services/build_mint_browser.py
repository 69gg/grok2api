"""Browser device-consent for CLI OIDC mint (Playwright).

Uses SSO cookie injection; on Turnstile failure, reloads the page and retries.
"""

from __future__ import annotations

import time
from typing import Callable, Optional
from urllib.parse import urlparse

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:
    return None


def _chrome_proxy_server(proxy: str) -> str:
    """Chromium --proxy-server cannot include user:pass."""
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    try:
        p = urlparse(proxy)
        if p.hostname and p.port:
            scheme = p.scheme or "http"
            return f"{scheme}://{p.hostname}:{p.port}"
        if p.hostname:
            return f"{p.scheme or 'http'}://{p.hostname}"
    except Exception:
        pass
    if "@" in proxy:
        # strip credentials best-effort
        return proxy.split("@", 1)[-1]
    return proxy


def approve_device_with_sso(
    *,
    verification_uri_complete: str,
    sso_token: str,
    email: str = "",
    proxy: Optional[str] = None,
    headless: bool = False,
    timeout_sec: float = 240.0,
    max_turnstile_reloads: int = 3,
    log: Optional[LogFn] = None,
) -> None:
    """Open device URL with SSO cookies and complete consent.

    If Cloudflare Turnstile fails, refresh the page and retry.
    """
    log = log or _noop
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright required for browser mint (already a project dependency); "
            "run: uv run playwright install chromium"
        ) from exc

    sso_token = (sso_token or "").strip()
    if sso_token.startswith("sso="):
        sso_token = sso_token[4:]
    if not sso_token:
        raise RuntimeError("empty sso token for device consent")

    deadline = time.time() + timeout_sec
    reloads = 0
    chrome_proxy = _chrome_proxy_server(proxy or "")

    launch_args = [
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--mute-audio",
        "--no-first-run",
        "--window-size=1280,900",
    ]
    if chrome_proxy:
        launch_args.append(f"--proxy-server={chrome_proxy}")
        log(f"browser proxy={chrome_proxy}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=launch_args)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        # HttpOnly SSO cookies for accounts.x.ai / .x.ai
        cookies = []
        for domain in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai"):
            for name in ("sso", "sso-rw"):
                cookies.append(
                    {
                        "name": name,
                        "value": sso_token,
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "None",
                    }
                )
        try:
            context.add_cookies(cookies)
            log(f"injected sso cookies n={len(cookies)}")
        except Exception as exc:
            log(f"cookie inject failed: {exc}")

        page = context.new_page()

        def _visible_text() -> str:
            try:
                return page.inner_text("body", timeout=2000)
            except Exception:
                try:
                    return page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
                except Exception:
                    return ""

        def _is_turnstile_fail(text: str) -> bool:
            t = text or ""
            return any(
                x in t
                for x in (
                    "验证失败",
                    "Verification failed",
                    "failure_retry",
                    "故障排除",
                )
            )

        def _click_exact(label: str) -> bool:
            try:
                loc = page.get_by_role("button", name=label, exact=True)
                if loc.count() > 0:
                    loc.first.click(timeout=2000)
                    return True
            except Exception:
                pass
            try:
                loc = page.locator(f"button:text-is('{label}')")
                if loc.count() > 0:
                    loc.first.click(timeout=2000)
                    return True
            except Exception:
                pass
            return False

        try:
            page.goto(
                verification_uri_complete,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            log(f"opened {verification_uri_complete}")

            while time.time() < deadline:
                url = page.url or ""
                text = _visible_text()

                if (
                    "device/done" in url
                    or "设备已授权" in text
                    or "device authorized" in text.lower()
                ):
                    log("device authorized")
                    return

                if _is_turnstile_fail(text) or "failure_retry" in url:
                    reloads += 1
                    if reloads > max_turnstile_reloads:
                        raise RuntimeError(
                            f"turnstile failed after {max_turnstile_reloads} reloads"
                        )
                    log(
                        f"turnstile failed — reload page retry "
                        f"{reloads}/{max_turnstile_reloads}"
                    )
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        page.goto(
                            verification_uri_complete,
                            wait_until="domcontentloaded",
                            timeout=60000,
                        )
                    time.sleep(2.0)
                    continue

                for label in ("全部允许", "Accept all", "Allow all"):
                    if _click_exact(label):
                        log(f"clicked cookie banner {label}")
                        time.sleep(0.5)
                        break

                if _click_exact("继续") or _click_exact("Continue"):
                    log("clicked continue")
                    time.sleep(1.0)
                    continue

                if _click_exact("允许") or _click_exact("Allow"):
                    log("clicked allow")
                    time.sleep(1.5)
                    continue

                time.sleep(0.8)

            raise RuntimeError("device consent timed out")
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


__all__ = ["approve_device_with_sso"]
