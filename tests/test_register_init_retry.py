"""Tests for RegisterRunner action-config init retries."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grok2api.services.register.runner import RegisterRunner


class _FakeResp:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


def test_init_config_retries_then_succeeds() -> None:
    runner = RegisterRunner(target_count=1, thread_count=1)
    calls = {"n": 0}

    class _FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __enter__(self) -> "_FakeSession":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str, timeout: int = 20) -> _FakeResp:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("TLS connect error: OPENSSL_internal:invalid library")
            if "sign-up" in url:
                html = (
                    '<html><script src="/_next/static/chunks/app.js"></script>'
                    + ('x' * 600)
                    + 'sitekey":"0x4AAAAAAAhr9JGVDZbrZOo0"</html>'
                )
                return _FakeResp(html, 200)
            # JS asset with action id
            return _FakeResp("const x = '7f50061dd2f5b389a530e4a048d5fdf0c48d1d9259';")

    with (
        patch("grok2api.services.register.runner.curl_requests.Session", _FakeSession),
        patch("grok2api.services.register.runner.time.sleep", return_value=None),
        patch("grok2api.services.register.runner.get_proxies_dict", return_value=None),
    ):
        runner._init_config(max_attempts=3)

    assert runner._config_ready is True
    assert runner._config["action_id"] == "7f50061dd2f5b389a530e4a048d5fdf0c48d1d9259"
    assert calls["n"] >= 2


def test_init_config_exhausted_raises() -> None:
    runner = RegisterRunner(target_count=1, thread_count=1)

    class _AlwaysFail:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_AlwaysFail":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, *args: Any, **kwargs: Any) -> _FakeResp:
            raise RuntimeError("curl: (35) TLS connect error")

    with (
        patch("grok2api.services.register.runner.curl_requests.Session", _AlwaysFail),
        patch("grok2api.services.register.runner.time.sleep", return_value=None),
        patch("grok2api.services.register.runner.get_proxies_dict", return_value=None),
    ):
        with pytest.raises(RuntimeError, match="after 2 attempts"):
            runner._init_config(max_attempts=2)

    assert runner._config_ready is False
