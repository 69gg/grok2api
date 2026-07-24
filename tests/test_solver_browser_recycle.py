"""Tests for long-lived Turnstile solver browser recycling."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_solver_module() -> ModuleType:
    solver_dir = Path(__file__).resolve().parents[1] / "scripts" / "turnstile_solver"
    module_path = solver_dir / "api_solver.py"
    spec = importlib.util.spec_from_file_location("grok2api_test_api_solver", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load solver module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(solver_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(solver_dir))
    return module


SOLVER_MODULE = _load_solver_module()


class _FakeBrowser:
    def is_connected(self) -> bool:
        return True


def _new_server(*, recycle_seconds: int, recycle_tasks: int) -> Any:
    return SOLVER_MODULE.TurnstileAPIServer(
        headless=True,
        useragent=None,
        debug=False,
        browser_type="camoufox",
        thread=1,
        proxy_support=False,
        browser_recycle_seconds=recycle_seconds,
        browser_recycle_tasks=recycle_tasks,
    )


@pytest.mark.asyncio
async def test_browser_recycles_after_completed_task_limit() -> None:
    server = _new_server(recycle_seconds=0, recycle_tasks=2)
    browser = _FakeBrowser()
    browser_config: dict[str, object] = {}
    replacements: list[str] = []

    async def fake_replace(
        index: int,
        current_browser: object,
        current_config: dict[str, object],
        reason: str = "",
    ) -> None:
        assert index == 1
        assert current_browser is browser
        assert current_config is browser_config
        replacements.append(reason)

    server._replace_browser = fake_replace
    await server._return_browser(1, browser, browser_config)
    await server.browser_pool.get()
    await server._return_browser(1, browser, browser_config)

    assert replacements == ["recycle (completed 2 tasks)"]
    assert server.browser_pool.empty()


@pytest.mark.asyncio
async def test_browser_recycles_after_age_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _new_server(recycle_seconds=60, recycle_tasks=0)
    browser = _FakeBrowser()
    browser_config: dict[str, object] = {}
    replacements: list[str] = []
    server._browser_started_at[1] = 100.0

    async def fake_replace(
        _index: int,
        _current_browser: object,
        _current_config: dict[str, object],
        reason: str = "",
    ) -> None:
        replacements.append(reason)

    monkeypatch.setattr(SOLVER_MODULE.time, "monotonic", lambda: 161.0)
    server._replace_browser = fake_replace
    await server._return_browser(1, browser, browser_config)

    assert replacements == ["recycle (age 61s)"]
    assert server.browser_pool.empty()
