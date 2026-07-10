"""Browser device-consent for CLI OIDC mint (last resort).

Uses SSO cookie injection; on Turnstile failure, reloads the page and retries.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from grok2api.core.logger import logger

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:
    return None


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

    If Cloudflare Turnstile fails, refresh the page and retry (user requirement).
    """
    log = log or _noop
    try:
        from DrissionPage import Chromium, ChromiumOptions
    except ImportError as exc:
        raise RuntimeError(
            "DrissionPage required for browser mint; install or disable mint_allow_browser"
        ) from exc

    opts = ChromiumOptions()
    opts.auto_port()
    for flag in (
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--mute-audio",
        "--no-first-run",
        "--window-size=1280,900",
    ):
        opts.set_argument(flag)
    if headless:
        try:
            opts.headless(True)
        except Exception:
            opts.set_argument("--headless=new")
    if proxy:
        # strip user:pass for chromium --proxy-server
        chrome_proxy = proxy
        if "@" in proxy:
            # http://user:pass@host:port -> host:port
            try:
                from urllib.parse import urlparse

                p = urlparse(proxy)
                if p.hostname and p.port:
                    chrome_proxy = f"{p.scheme}://{p.hostname}:{p.port}"
            except Exception:
                pass
        opts.set_argument(f"--proxy-server={chrome_proxy}")

    browser = Chromium(opts)
    page = browser.latest_tab
    deadline = time.time() + timeout_sec
    reloads = 0

    def _inject_sso() -> None:
        cookies = [
            {"name": "sso", "value": sso_token, "domain": ".x.ai", "path": "/"},
            {"name": "sso-rw", "value": sso_token, "domain": ".x.ai", "path": "/"},
            {"name": "sso", "value": sso_token, "domain": "accounts.x.ai", "path": "/"},
            {"name": "sso-rw", "value": sso_token, "domain": "accounts.x.ai", "path": "/"},
        ]
        try:
            page.get("https://accounts.x.ai/")
            time.sleep(0.5)
            page.set.cookies(cookies)
            log(f"injected sso cookies n={len(cookies)}")
        except Exception as exc:
            log(f"cookie inject failed: {exc}")

    def _visible_text() -> str:
        try:
            return str(page.run_js("return document.body ? document.body.innerText : ''") or "")
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
            # prefer exact text match buttons
            els = page.eles("tag:button", timeout=1) or []
            for el in els:
                try:
                    if (el.text or "").strip() == label:
                        el.click(by_js=False)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    try:
        _inject_sso()
        page.get(verification_uri_complete)
        log(f"opened {verification_uri_complete}")

        while time.time() < deadline:
            url = ""
            try:
                url = str(page.url or "")
            except Exception:
                pass
            text = _visible_text()

            if "device/done" in url or "设备已授权" in text or "device authorized" in text.lower():
                log("device authorized")
                return

            if _is_turnstile_fail(text) or "failure_retry" in url:
                reloads += 1
                if reloads > max_turnstile_reloads:
                    raise RuntimeError(
                        f"turnstile failed after {max_turnstile_reloads} reloads"
                    )
                log(f"turnstile failed — reload page retry {reloads}/{max_turnstile_reloads}")
                try:
                    page.refresh()
                except Exception:
                    page.get(verification_uri_complete)
                time.sleep(2.0)
                continue

            # dismiss cookie banner if present
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
            browser.quit()
        except Exception:
            pass


__all__ = ["approve_device_with_sso"]
