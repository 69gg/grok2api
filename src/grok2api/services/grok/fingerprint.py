"""Shared browser fingerprint helpers for Grok API requests."""

from grok2api.core.config import get_config

BROWSER_CHROME = "chrome136"
BROWSER_FIREFOX = "firefox135"

_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def get_impersonate() -> str:
    """返回与 cf_clearance cookie 匹配的 curl_cffi impersonate 值。"""
    cf_ua = get_config("grok.cf_clearance_ua", "")
    if cf_ua and "Firefox" in cf_ua:
        return BROWSER_FIREFOX
    return BROWSER_CHROME


def get_user_agent() -> str:
    """返回与 cf_clearance cookie 匹配的 User-Agent。"""
    cf = get_config("grok.cf_clearance", "")
    cf_ua = get_config("grok.cf_clearance_ua", "")
    if cf and cf_ua:
        return cf_ua
    return _CHROME_UA


def is_firefox_ua() -> bool:
    """当前是否使用 Firefox 指纹。"""
    return "Firefox" in get_user_agent()
