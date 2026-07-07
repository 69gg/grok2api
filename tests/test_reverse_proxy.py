"""Tests for reverse proxy helper behavior."""

from __future__ import annotations

from grok2api.services.reverse.utils import proxy as proxy_utils


def test_console_proxy_keys_prefer_console_then_base() -> None:
    assert proxy_utils.CONSOLE_PROXY_KEYS == (
        "proxy.console_proxy_url",
        "proxy.base_proxy_url",
    )


def test_build_curl_cffi_proxy_kwargs_uses_first_configured_key(monkeypatch) -> None:
    def fake_get_current_proxy_from(*keys: str) -> tuple[str | None, str]:
        assert keys == proxy_utils.CONSOLE_PROXY_KEYS
        return "proxy.console_proxy_url", "http://127.0.0.1:7890"

    monkeypatch.setattr(proxy_utils, "get_current_proxy_from", fake_get_current_proxy_from)

    kwargs = proxy_utils.build_curl_cffi_proxy_kwargs(*proxy_utils.CONSOLE_PROXY_KEYS)

    assert kwargs.active_proxy_key == "proxy.console_proxy_url"
    assert kwargs.proxy is None
    assert kwargs.proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


def test_build_curl_cffi_proxy_kwargs_normalizes_socks_proxy(monkeypatch) -> None:
    def fake_get_current_proxy_from(*_keys: str) -> tuple[str | None, str]:
        return "proxy.base_proxy_url", "socks5://127.0.0.1:1080"

    monkeypatch.setattr(proxy_utils, "get_current_proxy_from", fake_get_current_proxy_from)

    kwargs = proxy_utils.build_curl_cffi_proxy_kwargs(*proxy_utils.CONSOLE_PROXY_KEYS)

    assert kwargs.active_proxy_key == "proxy.base_proxy_url"
    assert kwargs.proxy == "socks5h://127.0.0.1:1080"
    assert kwargs.proxies is None
