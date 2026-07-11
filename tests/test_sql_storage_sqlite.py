"""SQLite SQLStorage concurrency / WAL / lock behaviour."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from grok2api.core.storage import SQLStorage


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.mark.asyncio
async def test_sqlite_wal_and_busy_timeout(sqlite_url: str) -> None:
    storage = SQLStorage(sqlite_url)
    try:
        await storage._ensure_schema()
        async with storage.async_session() as session:
            from sqlalchemy import text

            mode = (await session.execute(text("PRAGMA journal_mode"))).scalar()
            busy = (await session.execute(text("PRAGMA busy_timeout"))).scalar()
        assert str(mode).lower() == "wal"
        assert int(busy) >= 30000
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sqlite_load_save_roundtrip(sqlite_url: str) -> None:
    storage = SQLStorage(sqlite_url)
    try:
        await storage.save_tokens_delta(
            [
                {
                    "token": "tok-1",
                    "pool_name": "ssoBasic",
                    "status": "active",
                    "quota": 3,
                    "_update_kind": "state",
                }
            ]
        )
        data = await storage.load_tokens()
        assert data is not None
        assert "ssoBasic" in data
        assert data["ssoBasic"][0]["token"] == "tok-1"
        assert data["ssoBasic"][0]["quota"] == 3
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sqlite_concurrent_save_and_load(sqlite_url: str) -> None:
    storage = SQLStorage(sqlite_url)
    try:
        await storage.save_tokens_delta(
            [
                {
                    "token": f"tok-{i}",
                    "pool_name": "ssoBasic",
                    "status": "active",
                    "quota": i,
                    "_update_kind": "state",
                }
                for i in range(5)
            ]
        )

        async def writer(n: int) -> None:
            async with storage.acquire_lock("tokens_save", timeout=10):
                await storage.save_tokens_delta(
                    [
                        {
                            "token": f"tok-{n}",
                            "pool_name": "ssoBasic",
                            "status": "active",
                            "quota": n * 10,
                            "_update_kind": "state",
                        }
                    ]
                )

        async def reader() -> None:
            for _ in range(10):
                data = await storage.load_tokens()
                assert data is not None
                await asyncio.sleep(0.01)

        await asyncio.gather(
            writer(1),
            writer(2),
            writer(3),
            reader(),
            reader(),
        )
        data = await storage.load_tokens()
        assert data is not None
        tokens = {t["token"]: t for t in data["ssoBasic"]}
        assert tokens["tok-1"]["quota"] == 10
        assert tokens["tok-2"]["quota"] == 20
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_retry_on_sqlite_lock_retries(sqlite_url: str) -> None:
    storage = SQLStorage(sqlite_url)
    try:
        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("(sqlite3.OperationalError) database is locked")
            return "ok"

        result = await storage._retry_on_sqlite_lock("test", flaky, attempts=5)
        assert result == "ok"
        assert calls["n"] == 3
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sqlite_lock_hold_not_bound_by_acquire_timeout(sqlite_url: str) -> None:
    """Holding a lock longer than timeout must not cancel the critical section."""
    from grok2api.core.storage import StorageError

    storage = SQLStorage(sqlite_url)
    try:
        async with storage.acquire_lock("long_job", timeout=1):
            await asyncio.sleep(1.2)
            done = True
        assert done is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sqlite_different_lock_names_do_not_block(sqlite_url: str) -> None:
    """Unrelated named locks must not serialize on a global process lock."""
    storage = SQLStorage(sqlite_url)
    try:
        order: list[str] = []
        started = asyncio.Event()

        async def hold_a() -> None:
            async with storage.acquire_lock("cf_clearance_refresh", timeout=1):
                order.append("a_enter")
                started.set()
                await asyncio.sleep(0.3)
                order.append("a_exit")

        async def take_b() -> None:
            await started.wait()
            async with storage.acquire_lock("cli_oidc_init", timeout=1):
                order.append("b")

        await asyncio.wait_for(asyncio.gather(hold_a(), take_b()), timeout=2.0)
        assert "b" in order
        # b should interleave before a finishes (not wait for a)
        assert order.index("b") < order.index("a_exit")
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sqlite_same_lock_name_times_out(sqlite_url: str) -> None:
    from grok2api.core.storage import StorageError

    storage = SQLStorage(sqlite_url)
    try:
        started = asyncio.Event()

        async def holder() -> None:
            async with storage.acquire_lock("cli_oidc_init", timeout=5):
                started.set()
                await asyncio.sleep(0.5)

        async def waiter() -> None:
            await started.wait()
            with pytest.raises(StorageError, match="cli_oidc_init"):
                async with storage.acquire_lock("cli_oidc_init", timeout=0.2):
                    pass

        await asyncio.gather(holder(), waiter())
    finally:
        await storage.close()
