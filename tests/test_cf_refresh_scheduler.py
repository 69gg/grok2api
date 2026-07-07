from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grok2api.services.token import scheduler as scheduler_module


class _FakeStorage:
    def acquire_lock(self, name: str, timeout: int = 10) -> "_FakeLock":
        return _FakeLock()


class _FakeLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _FakeSolver:
    instances: list["_FakeSolver"] = []

    def __init__(self, config: scheduler_module.SolverConfig) -> None:
        self.config = config
        self.started = False
        self.stopped = False
        _FakeSolver.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_cf_refresh_scheduler_stops_solver_and_signals_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSolver.instances.clear()
    refresh_called = asyncio.Event()
    stop_seen = asyncio.Event()

    def fake_get_config(key: str, default: Any = None) -> Any:
        values: dict[str, Any] = {
            "proxy.refresh_interval": 60,
            "proxy.cf_solver_threads": 1,
            "register.solver_url": "http://127.0.0.1:5072",
            "register.solver_browser_type": "chromium",
            "register.auto_start_solver": True,
            "register.solver_debug": False,
        }
        return values.get(key, default)

    async def fake_refresh(stop_event: object | None = None, solver_url: str | None = None) -> bool:
        assert solver_url == "http://127.0.0.1:5072"
        refresh_called.set()
        while not getattr(stop_event, "is_set", lambda: False)():
            await asyncio.sleep(0.01)
        stop_seen.set()
        return False

    monkeypatch.setattr(scheduler_module, "get_config", fake_get_config)
    monkeypatch.setattr(scheduler_module, "get_storage", lambda: _FakeStorage())
    monkeypatch.setattr(scheduler_module, "TurnstileSolverProcess", _FakeSolver)
    monkeypatch.setattr(scheduler_module, "_refresh_cf_clearance", fake_refresh)

    scheduler = scheduler_module.TokenRefreshScheduler(interval_hours=8)
    scheduler.start(
        token_refresh_enabled=False,
        cf_refresh_enabled=True,
        console_team_init_enabled=False,
    )

    await asyncio.wait_for(refresh_called.wait(), timeout=1)
    await scheduler.stop()
    await asyncio.wait_for(stop_seen.wait(), timeout=1)

    assert _FakeSolver.instances
    assert _FakeSolver.instances[0].started is True
    assert _FakeSolver.instances[0].stopped is True
    assert scheduler._cf_task is None
    assert scheduler._cf_stop_event is None


@pytest.mark.asyncio
async def test_cf_refresh_not_started_when_disabled() -> None:
    scheduler = scheduler_module.TokenRefreshScheduler(interval_hours=8)
    scheduler.start(
        token_refresh_enabled=False,
        cf_refresh_enabled=False,
        console_team_init_enabled=False,
    )

    assert scheduler._cf_task is None
    assert scheduler._console_team_task is None
    await scheduler.stop()


@pytest.mark.asyncio
async def test_console_team_init_scheduler_runs_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_called = asyncio.Event()

    def fake_get_config(key: str, default: Any = None) -> Any:
        values: dict[str, Any] = {
            "token.console_team_auto_init_interval_sec": 10,
            "token.console_team_auto_init_batch_size": 100,
            "token.console_team_auto_init_concurrency": 5,
        }
        return values.get(key, default)

    class FakeManager:
        async def reload_if_stale(self) -> None:
            return None

        async def init_missing_console_team_ids(
            self,
            *,
            trigger: str = "scheduler",
            max_tokens: int | None = None,
            concurrency: int = 5,
        ) -> dict[str, int]:
            assert trigger == "scheduler"
            assert max_tokens == 100
            assert concurrency == 5
            init_called.set()
            return {"checked": 0, "created": 0, "failed": 0, "skipped": 0}

    async def fake_get_token_manager() -> FakeManager:
        return FakeManager()

    monkeypatch.setattr(scheduler_module, "get_config", fake_get_config)
    monkeypatch.setattr(scheduler_module, "get_storage", lambda: _FakeStorage())
    monkeypatch.setattr(scheduler_module, "get_token_manager", fake_get_token_manager)

    scheduler = scheduler_module.TokenRefreshScheduler(interval_hours=8)
    scheduler.start(
        token_refresh_enabled=False,
        cf_refresh_enabled=False,
        console_team_init_enabled=True,
    )

    await asyncio.wait_for(init_called.wait(), timeout=1)
    await scheduler.stop()

    assert scheduler._console_team_task is None
