"""Tests for reverse proxy helper behavior."""

from __future__ import annotations

from grok2api.services.reverse.utils.headers import build_console_headers
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


def test_console_headers_use_cookie_auth_without_anonymous_bearer(monkeypatch) -> None:
    values = {
        "proxy.user_agent": "Mozilla/5.0 Chrome/136.0.0.0",
        "proxy.browser": "chrome136",
        "proxy.cf_cookies": "cf_clearance=abc; last-team-id=old-team",
        "proxy.cf_clearance": "",
    }

    def fake_get_config(key: str, default: object = None) -> object:
        return values.get(key, default)

    monkeypatch.setattr(
        "grok2api.services.reverse.utils.headers.get_config",
        fake_get_config,
    )

    headers = build_console_headers("sso-token")

    assert "Authorization" not in headers
    assert "x-statsig-id" not in headers
    assert "x-xai-request-id" not in headers
    assert headers["Accept"] == "*/*"
    assert headers["x-user-agent"] == "connect-es/2.1.1"
    assert headers["x-cluster"] == "https://us-east-1.api.x.ai"
    assert headers["Referer"] == "https://console.x.ai/"
    assert "sentry-trace" in headers
    assert "sentry-trace_id=" in headers["baggage"]
    assert "sso=sso-token" in headers["Cookie"]
    assert "sso-rw=sso-token" in headers["Cookie"]
    assert "last-team-id=old-team" not in headers["Cookie"]


def test_console_headers_strip_browser_team_cookies_without_team_id(monkeypatch) -> None:
    values = {
        "proxy.user_agent": "Mozilla/5.0 Chrome/136.0.0.0",
        "proxy.browser": "chrome136",
        "proxy.cf_cookies": (
            "cf_clearance=abc; last-team-id=browser-team; "
            "chat-playground-n:team-browser-team=18; voice-stt-n:team-browser-team=2"
        ),
        "proxy.cf_clearance": "",
    }

    def fake_get_config(key: str, default: object = None) -> object:
        return values.get(key, default)

    monkeypatch.setattr(
        "grok2api.services.reverse.utils.headers.get_config",
        fake_get_config,
    )

    headers = build_console_headers("sso-token")

    assert headers["Referer"] == "https://console.x.ai/"
    assert "cf_clearance=abc" in headers["Cookie"]
    assert "last-team-id=" not in headers["Cookie"]
    assert "chat-playground-n:" not in headers["Cookie"]
    assert "voice-stt-n:" not in headers["Cookie"]
