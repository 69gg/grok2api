"""Proxy helpers for reverse upstream requests."""

from __future__ import annotations

from typing import NamedTuple, Optional
from urllib.parse import urlparse

from grok2api.core.proxy_pool import get_current_proxy_from


BASE_PROXY_KEYS = ("proxy.base_proxy_url",)
CONSOLE_PROXY_KEYS = ("proxy.console_proxy_url", "proxy.base_proxy_url")


class CurlCffiProxyKwargs(NamedTuple):
    """curl_cffi per-request proxy arguments plus the active config key."""

    active_proxy_key: Optional[str]
    proxy: Optional[str]
    proxies: Optional[dict[str, str]]


def normalize_curl_proxy(proxy_url: str) -> str:
    """Normalize SOCKS proxy schemes for remote DNS with curl_cffi."""
    if not proxy_url:
        return proxy_url
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme == "socks5":
        return proxy_url.replace("socks5://", "socks5h://", 1)
    if scheme == "socks4":
        return proxy_url.replace("socks4://", "socks4a://", 1)
    return proxy_url


def build_curl_cffi_proxy_kwargs(*config_keys: str) -> CurlCffiProxyKwargs:
    """Build curl_cffi proxy/proxies kwargs from the first configured proxy key."""
    active_proxy_key, proxy_url = get_current_proxy_from(*config_keys)
    if not proxy_url:
        return CurlCffiProxyKwargs(active_proxy_key, None, None)

    normalized = normalize_curl_proxy(proxy_url)
    scheme = urlparse(normalized).scheme.lower()
    if scheme.startswith("socks"):
        return CurlCffiProxyKwargs(active_proxy_key, normalized, None)
    return CurlCffiProxyKwargs(
        active_proxy_key,
        None,
        {"http": normalized, "https": normalized},
    )


def get_current_proxy_url_from(*config_keys: str) -> tuple[Optional[str], str]:
    """Return the first configured proxy URL for non-curl clients."""
    return get_current_proxy_from(*config_keys)


__all__ = [
    "BASE_PROXY_KEYS",
    "CONSOLE_PROXY_KEYS",
    "CurlCffiProxyKwargs",
    "build_curl_cffi_proxy_kwargs",
    "get_current_proxy_url_from",
    "normalize_curl_proxy",
]
