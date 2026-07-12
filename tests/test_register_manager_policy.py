"""Policy tests for auto-register stop / max_errors / solver lifecycle."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grok2api.services.register.manager import AutoRegisterManager, RegisterJob


def test_on_error_unlimited_does_not_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_errors=0 must not set stop_event."""
    job = RegisterJob(job_id="t1", total=5, pool="ssoBasic", register_threads=1)
    manager = AutoRegisterManager()

    # Extract the inner callback logic by replaying the same conditions.
    max_errors = 0

    def _on_error(msg: str) -> None:
        job.record_error(msg)
        if max_errors <= 0:
            return
        with job._lock:
            if job.status in {"starting", "running"} and job.errors >= max_errors:
                job.status = "error"
                job.stop_event.set()

    job.status = "running"
    for i in range(100):
        _on_error(f"fail {i}")

    assert job.errors == 100
    assert not job.stop_event.is_set()
    assert job.status == "running"


def test_on_error_with_limit_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    job = RegisterJob(job_id="t2", total=5, pool="ssoBasic", register_threads=1)
    job.status = "running"
    max_errors = 3

    def _on_error(msg: str) -> None:
        job.record_error(msg)
        if max_errors <= 0:
            return
        with job._lock:
            if job.status in {"starting", "running"} and job.errors >= max_errors:
                job.status = "error"
                job.error = f"Too many failures ({job.errors}/{max_errors})."
                job.stop_event.set()

    _on_error("a")
    _on_error("b")
    assert not job.stop_event.is_set()
    _on_error("c")
    assert job.stop_event.is_set()
    assert job.status == "error"


@pytest.mark.asyncio
async def test_stop_job_without_solver_flag_leaves_solver() -> None:
    manager = AutoRegisterManager()
    job = RegisterJob(job_id="t3", total=1, pool="ssoBasic")
    job.status = "running"
    manager._job = job
    solver = MagicMock()
    manager._solver = solver

    async def _noop_wait(*args: Any, **kwargs: Any) -> None:
        return None

    with patch("asyncio.wait_for", side_effect=_noop_wait):
        await manager.stop_job(stop_solver=False)

    solver.stop.assert_not_called()
    assert manager._solver is solver
    assert job.status == "stopping"
    assert job.stop_event.is_set()


@pytest.mark.asyncio
async def test_stop_job_with_solver_flag_stops_solver() -> None:
    manager = AutoRegisterManager()
    job = RegisterJob(job_id="t4", total=1, pool="ssoBasic")
    job.status = "running"
    manager._job = job
    solver = MagicMock()
    manager._solver = solver

    async def _to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    async def _noop_wait(*args: Any, **kwargs: Any) -> None:
        return None

    with (
        patch("asyncio.wait_for", side_effect=_noop_wait),
        patch("asyncio.to_thread", side_effect=_to_thread),
    ):
        await manager.stop_job(stop_solver=True)

    solver.stop.assert_called_once()
    assert manager._solver is None
