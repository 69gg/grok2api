"""Token 刷新调度器"""

import asyncio
import threading
from typing import Any, Optional
from urllib.parse import urlparse

from grok2api.core.config import get_config
from grok2api.core.logger import logger
from grok2api.core.storage import get_storage, StorageError, RedisStorage
from grok2api.core.proxy import get_proxy_url
from grok2api.services.register.solver import SolverConfig, TurnstileSolverProcess
from grok2api.services.token.manager import get_token_manager

CF_CLEARANCE_DEFAULT_INTERVAL_SECONDS = 25 * 60
CF_CLEARANCE_LOCK_SECONDS = 35 * 60
CF_CLEARANCE_STOP_TIMEOUT_SECONDS = 25.0
CONSOLE_TEAM_DEFAULT_INTERVAL_SECONDS = 60
CONSOLE_TEAM_LOCK_SECONDS = 10 * 60
CLI_OIDC_DEFAULT_INTERVAL_SECONDS = 60
CLI_OIDC_LOCK_SECONDS = 10 * 60
CLI_OIDC_REFRESH_DEFAULT_INTERVAL_SECONDS = 120
LOCAL_SOLVER_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except Exception:
        return default


def _cf_refresh_interval_seconds() -> int:
    return _as_int(
        get_config("proxy.refresh_interval", CF_CLEARANCE_DEFAULT_INTERVAL_SECONDS),
        CF_CLEARANCE_DEFAULT_INTERVAL_SECONDS,
        minimum=60,
    )


def _console_team_init_interval_seconds() -> int:
    return _as_int(
        get_config(
            "token.console_team_auto_init_interval_sec",
            CONSOLE_TEAM_DEFAULT_INTERVAL_SECONDS,
        ),
        CONSOLE_TEAM_DEFAULT_INTERVAL_SECONDS,
        minimum=10,
    )


def _cli_oidc_init_interval_seconds() -> int:
    return _as_int(
        get_config("build.auto_init_interval_sec", CLI_OIDC_DEFAULT_INTERVAL_SECONDS),
        CLI_OIDC_DEFAULT_INTERVAL_SECONDS,
        minimum=10,
    )


def _cli_oidc_refresh_interval_seconds() -> int:
    return _as_int(
        get_config(
            "build.auto_refresh_interval_sec",
            CLI_OIDC_REFRESH_DEFAULT_INTERVAL_SECONDS,
        ),
        CLI_OIDC_REFRESH_DEFAULT_INTERVAL_SECONDS,
        minimum=30,
    )


def _is_local_solver_url(solver_url: str) -> bool:
    try:
        host = urlparse(str(solver_url)).hostname or ""
    except Exception:
        return False
    return host in LOCAL_SOLVER_HOSTS


def _build_cf_solver_config() -> SolverConfig:
    solver_url = str(get_config("register.solver_url", "") or "http://127.0.0.1:5072").strip()
    browser_type = str(get_config("register.solver_browser_type", "camoufox") or "camoufox").strip().lower()
    if browser_type not in {"chromium", "chrome", "msedge", "camoufox"}:
        browser_type = "camoufox"

    auto_start = _as_bool(get_config("register.auto_start_solver", True), True)
    if not _is_local_solver_url(solver_url):
        auto_start = False

    return SolverConfig(
        url=solver_url or "http://127.0.0.1:5072",
        threads=_as_int(get_config("proxy.cf_solver_threads", 1), 1),
        browser_type=browser_type,
        debug=_as_bool(get_config("register.solver_debug", False), False),
        auto_start=auto_start,
        proxy_url=get_proxy_url(),
    )


async def _refresh_cf_clearance(
    stop_event: threading.Event | None = None,
    solver_url: str | None = None,
) -> bool:
    """调用本地 solver 刷新 cf_clearance 并更新运行时配置。"""
    from grok2api.core.config import config
    from grok2api.services.register.services.cf_clearance_service import CfClearanceService
    from grok2api.services.reverse.utils.headers import (
        merge_cf_clearance_cookie,
        resolve_proxy_browser,
    )

    service = CfClearanceService(solver_url=solver_url)
    result = await asyncio.to_thread(service.refresh, stop_event)
    if result.get("ok") and result.get("cf_clearance"):
        current_cf_cookies = str(config.get("proxy.cf_cookies", "") or "")
        merged_cf_cookies = merge_cf_clearance_cookie(
            current_cf_cookies,
            result["cf_clearance"],
        )
        update_data: dict[str, dict[str, str]] = {
            "proxy": {
                "cf_clearance": result["cf_clearance"],
                "cf_cookies": merged_cf_cookies,
            }
        }
        if result.get("user_agent"):
            update_data["proxy"]["user_agent"] = result["user_agent"]
            update_data["proxy"]["browser"] = resolve_proxy_browser(
                config.get("proxy.browser", ""),
                result["user_agent"],
            )
        await config.update(update_data)
        logger.info(f"cf_clearance: refreshed ({result['cf_clearance'][:16]}...)")
        return True
    else:
        logger.warning(f"cf_clearance: refresh failed - {result.get('error')}")
        return False


class TokenRefreshScheduler:
    """Token 自动刷新调度器"""

    def __init__(self, interval_hours: int = 8):
        self.interval_hours = interval_hours
        self.interval_seconds = interval_hours * 3600
        self._task: Optional[asyncio.Task] = None
        self._cf_task: Optional[asyncio.Task] = None
        self._console_team_task: Optional[asyncio.Task] = None
        self._cli_oidc_task: Optional[asyncio.Task] = None
        self._cli_refresh_task: Optional[asyncio.Task] = None
        self._cf_solver: TurnstileSolverProcess | None = None
        self._cf_stop_event: threading.Event | None = None
        self._running = False

    async def _refresh_loop(self):
        """刷新循环"""
        logger.info(f"Scheduler: started (interval: {self.interval_hours}h)")

        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                if not self._running:
                    break

                storage = get_storage()
                lock_acquired = False
                lock = None

                if isinstance(storage, RedisStorage):
                    # Redis: non-blocking lock to avoid multi-worker duplication
                    lock_key = "grok2api:lock:token_refresh"
                    lock = storage.redis.lock(
                        lock_key, timeout=self.interval_seconds + 60, blocking_timeout=0
                    )
                    lock_acquired = await lock.acquire(blocking=False)
                else:
                    try:
                        async with storage.acquire_lock("token_refresh", timeout=1):
                            lock_acquired = True
                    except StorageError:
                        lock_acquired = False

                if not lock_acquired:
                    logger.info("Scheduler: skipped (lock not acquired)")
                    await asyncio.sleep(self.interval_seconds)
                    continue

                try:
                    logger.info("Scheduler: starting token refresh...")
                    manager = await get_token_manager()
                    result = await manager.refresh_cooling_tokens()

                    logger.info(
                        f"Scheduler: refresh completed - "
                        f"checked={result['checked']}, "
                        f"refreshed={result['refreshed']}, "
                        f"recovered={result['recovered']}, "
                        f"expired={result['expired']}"
                    )
                finally:
                    if lock is not None and lock_acquired:
                        try:
                            await lock.release()
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler: refresh error - {e}")
                await asyncio.sleep(self.interval_seconds)

    async def _ensure_cf_solver(self) -> SolverConfig:
        solver_cfg = _build_cf_solver_config()
        if not solver_cfg.auto_start:
            return solver_cfg
        if self._cf_solver is None:
            solver = TurnstileSolverProcess(solver_cfg)
            await asyncio.to_thread(solver.start)
            self._cf_solver = solver
        return solver_cfg

    async def _stop_cf_solver(self) -> None:
        solver = self._cf_solver
        self._cf_solver = None
        if solver is None:
            return
        try:
            await asyncio.to_thread(solver.stop)
        except Exception as exc:
            logger.warning(f"Scheduler: failed to stop cf_clearance solver - {exc}")

    async def _run_cf_refresh_with_lock(self) -> None:
        storage = get_storage()
        lock_acquired = False
        lock = None

        try:
            if isinstance(storage, RedisStorage):
                lock = storage.redis.lock(
                    "grok2api:lock:cf_clearance_refresh",
                    timeout=CF_CLEARANCE_LOCK_SECONDS,
                    blocking_timeout=0,
                )
                lock_acquired = await lock.acquire(blocking=False)
                if not lock_acquired:
                    logger.info("cf_clearance: skipped (lock not acquired)")
                    return

                solver_cfg = await self._ensure_cf_solver()
                await _refresh_cf_clearance(self._cf_stop_event, solver_cfg.url)
                return

            try:
                async with storage.acquire_lock("cf_clearance_refresh", timeout=1):
                    solver_cfg = await self._ensure_cf_solver()
                    await _refresh_cf_clearance(self._cf_stop_event, solver_cfg.url)
            except StorageError:
                logger.info("cf_clearance: skipped (lock not acquired)")
        finally:
            if lock is not None and lock_acquired:
                try:
                    await lock.release()
                except Exception:
                    pass

    async def _sleep_until_next_cf_refresh(self, interval_seconds: int) -> bool:
        stop_event = self._cf_stop_event
        deadline = asyncio.get_running_loop().time() + interval_seconds
        while self._running and not (stop_event is not None and stop_event.is_set()):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(1.0, remaining))
        return False

    async def _cf_clearance_loop(self) -> None:
        """独立的 cf_clearance 定时刷新循环。"""
        interval_seconds = _cf_refresh_interval_seconds()
        logger.info(
            f"Scheduler: cf_clearance refresh loop started (interval: {interval_seconds // 60}min)"
        )

        try:
            while self._running:
                try:
                    await self._run_cf_refresh_with_lock()
                except Exception as e:
                    logger.warning(f"Scheduler: cf_clearance refresh error - {e}")
                if not await self._sleep_until_next_cf_refresh(interval_seconds):
                    break
        except asyncio.CancelledError:
            raise
        finally:
            await self._stop_cf_solver()

    async def _run_console_team_init_with_lock(self) -> None:
        storage = get_storage()
        lock_acquired = False
        lock = None

        try:
            if isinstance(storage, RedisStorage):
                lock = storage.redis.lock(
                    "grok2api:lock:console_team_init",
                    timeout=CONSOLE_TEAM_LOCK_SECONDS,
                    blocking_timeout=0,
                )
                lock_acquired = await lock.acquire(blocking=False)
                if not lock_acquired:
                    logger.info("Console team init: skipped (lock not acquired)")
                    return
                manager = await get_token_manager()
                await manager.reload_if_stale()
                await manager.init_missing_console_team_ids(
                    trigger="scheduler",
                    max_tokens=_as_int(
                        get_config("token.console_team_auto_init_batch_size", 100),
                        100,
                    ),
                    concurrency=_as_int(
                        get_config("token.console_team_auto_init_concurrency", 5),
                        5,
                    ),
                )
                return

            try:
                async with storage.acquire_lock("console_team_init", timeout=1):
                    manager = await get_token_manager()
                    await manager.reload_if_stale()
                    await manager.init_missing_console_team_ids(
                        trigger="scheduler",
                        max_tokens=_as_int(
                            get_config("token.console_team_auto_init_batch_size", 100),
                            100,
                        ),
                        concurrency=_as_int(
                            get_config("token.console_team_auto_init_concurrency", 5),
                            5,
                        ),
                    )
            except StorageError:
                logger.info("Console team init: skipped (lock not acquired)")
        finally:
            if lock is not None and lock_acquired:
                try:
                    await lock.release()
                except Exception:
                    pass

    async def _console_team_init_loop(self) -> None:
        """主动补全 Console team id 的后台循环。"""
        interval_seconds = _console_team_init_interval_seconds()
        logger.info(
            "Scheduler: console team init loop started (interval: {}s)",
            interval_seconds,
        )

        try:
            while self._running:
                try:
                    await self._run_console_team_init_with_lock()
                except Exception as e:
                    logger.warning(f"Scheduler: console team init error - {e}")

                deadline = asyncio.get_running_loop().time() + interval_seconds
                while self._running:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(1.0, remaining))
        except asyncio.CancelledError:
            raise

    async def _run_cli_oidc_init_with_lock(self) -> None:
        """Silently mint OIDC for existing SSO accounts (console-team style)."""
        storage = get_storage()
        try:
            async with storage.acquire_lock("cli_oidc_init", timeout=1):
                from grok2api.services.grok.services.build_auth import BuildAuthService

                await BuildAuthService.init_missing_cli_oidc(
                    trigger="scheduler",
                    max_tokens=_as_int(
                        get_config("build.auto_init_batch_size", 20), 20
                    ),
                    concurrency=_as_int(
                        get_config("build.auto_init_concurrency", 2), 2
                    ),
                )
        except StorageError:
            logger.info("CLI OIDC init: skipped (lock not acquired)")

    async def _cli_oidc_init_loop(self) -> None:
        interval_seconds = _cli_oidc_init_interval_seconds()
        logger.info(
            "Scheduler: CLI OIDC auto-init loop started (interval: {}s)",
            interval_seconds,
        )
        try:
            while self._running:
                try:
                    await self._run_cli_oidc_init_with_lock()
                except Exception as e:
                    logger.warning(f"Scheduler: CLI OIDC init error - {e}")
                deadline = asyncio.get_running_loop().time() + interval_seconds
                while self._running:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(1.0, remaining))
        except asyncio.CancelledError:
            raise

    async def _cli_oidc_refresh_loop(self) -> None:
        interval_seconds = _cli_oidc_refresh_interval_seconds()
        logger.info(
            "Scheduler: CLI OIDC refresh loop started (interval: {}s)",
            interval_seconds,
        )
        try:
            while self._running:
                try:
                    if _as_bool(get_config("build.auto_refresh_enabled", True), True):
                        from grok2api.services.grok.services.build_auth import (
                            BuildAuthService,
                        )

                        await BuildAuthService.refresh_expiring_cli_tokens(
                            max_tokens=_as_int(
                                get_config("build.auto_refresh_max_tokens", 50), 50
                            )
                        )
                except Exception as e:
                    logger.warning(f"Scheduler: CLI OIDC refresh error - {e}")
                deadline = asyncio.get_running_loop().time() + interval_seconds
                while self._running:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(1.0, remaining))
        except asyncio.CancelledError:
            raise

    def start(
        self,
        *,
        token_refresh_enabled: bool = True,
        cf_refresh_enabled: bool | None = None,
        console_team_init_enabled: bool | None = None,
        cli_oidc_init_enabled: bool | None = None,
        cli_oidc_refresh_enabled: bool | None = None,
    ) -> None:
        """启动调度器"""
        if self._running:
            logger.warning("Scheduler: already running")
            return

        if cf_refresh_enabled is None:
            cf_refresh_enabled = _as_bool(get_config("proxy.enabled", False), False)
        if console_team_init_enabled is None:
            console_team_init_enabled = _as_bool(
                get_config("token.console_team_auto_init_enabled", True),
                True,
            )
        if cli_oidc_init_enabled is None:
            cli_oidc_init_enabled = _as_bool(
                get_config("build.enabled", True), True
            ) and _as_bool(get_config("build.auto_init_from_sso_enabled", True), True)
        if cli_oidc_refresh_enabled is None:
            cli_oidc_refresh_enabled = _as_bool(
                get_config("build.enabled", True), True
            ) and _as_bool(get_config("build.auto_refresh_enabled", True), True)

        self._running = True
        if token_refresh_enabled:
            self._task = asyncio.create_task(self._refresh_loop())
        if cf_refresh_enabled:
            self._cf_stop_event = threading.Event()
            self._cf_task = asyncio.create_task(self._cf_clearance_loop())
        if console_team_init_enabled:
            self._console_team_task = asyncio.create_task(
                self._console_team_init_loop()
            )
        if cli_oidc_init_enabled:
            self._cli_oidc_task = asyncio.create_task(self._cli_oidc_init_loop())
        if cli_oidc_refresh_enabled:
            self._cli_refresh_task = asyncio.create_task(self._cli_oidc_refresh_loop())
        logger.info("Scheduler: enabled")

    async def stop(self) -> None:
        """停止调度器"""
        if not self._running:
            return

        self._running = False
        if self._cf_stop_event is not None:
            self._cf_stop_event.set()

        tasks = []
        if self._task:
            self._task.cancel()
            tasks.append(self._task)
        if self._console_team_task:
            self._console_team_task.cancel()
            tasks.append(self._console_team_task)
        if self._cli_oidc_task:
            self._cli_oidc_task.cancel()
            tasks.append(self._cli_oidc_task)
        if self._cli_refresh_task:
            self._cli_refresh_task.cancel()
            tasks.append(self._cli_refresh_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._cf_task:
            try:
                await asyncio.wait_for(
                    self._cf_task,
                    timeout=CF_CLEARANCE_STOP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                self._cf_task.cancel()
                await asyncio.gather(self._cf_task, return_exceptions=True)

        await self._stop_cf_solver()
        self._task = None
        self._console_team_task = None
        self._cli_oidc_task = None
        self._cli_refresh_task = None
        self._cf_task = None
        self._cf_stop_event = None
        logger.info("Scheduler: stopped")


# 全局单例
_scheduler: Optional[TokenRefreshScheduler] = None


def get_scheduler(interval_hours: int = 8) -> TokenRefreshScheduler:
    """获取调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TokenRefreshScheduler(interval_hours)
    return _scheduler


__all__ = ["TokenRefreshScheduler", "get_scheduler"]
