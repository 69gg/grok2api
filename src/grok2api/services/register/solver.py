"""Local Turnstile solver process manager."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from grok2api.core.logger import logger
from grok2api.core.paths import DATA_DIR, PROJECT_ROOT


def _wait_for_port(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except Exception:
            time.sleep(0.5)
    return False


@dataclass
class SolverConfig:
    url: str
    threads: int = 5
    browser_type: str = "chromium"
    debug: bool = False
    auto_start: bool = True
    proxy_url: str = ""


class TurnstileSolverProcess:
    """Start/stop a local Turnstile solver."""

    def __init__(self, config: SolverConfig) -> None:
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._started_by_us = False
        self._repo_root = PROJECT_ROOT
        self._python_exe: str = sys.executable
        self._actual_browser_type: str = config.browser_type

    def _script_path(self) -> Path:
        return self._repo_root / "scripts" / "turnstile_solver" / "api_solver.py"

    def _can_import(self, python_exe: str, modules: list[str]) -> bool:
        """Check whether a python executable can import given modules."""
        code = "; ".join([f"import {m}" for m in modules])
        try:
            subprocess.check_call(
                [python_exe, "-c", code],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False

    def _windows_where_python(self) -> list[str]:
        """List python.exe candidates on Windows using `where python` (best-effort)."""
        if not sys.platform.startswith("win"):
            return []
        try:
            out = subprocess.check_output(
                ["where", "python"],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
        except Exception:
            return []

        paths: list[str] = []
        seen: set[str] = set()
        for line in out.splitlines():
            p = (line or "").strip().strip('"')
            if not p:
                continue
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            paths.append(p)
        return paths

    def _select_runtime(self) -> None:
        """Pick python executable + browser type to run solver with.

        Practical notes (Windows):
        - The API server may run in a venv (e.g. Python 3.13).
        - Many users install the solver dependencies (camoufox/patchright) into their
          system python (e.g. Python 3.12) and start the solver via a `.bat`.

        To match that workflow, we prefer an interpreter that has `patchright` when
        available (it tends to have better anti-bot compatibility). For camoufox,
        we also require `camoufox` import to succeed.
        """
        desired = (self.config.browser_type or "chromium").strip().lower()
        if desired not in {"chromium", "chrome", "msedge", "camoufox"}:
            desired = "chromium"

        # Collect python candidates.
        #
        # NOTE: When the API server runs under `uv run`, `python` on PATH usually points to
        # the venv python, not the system python. On Windows, use `where python` to discover
        # other interpreters (e.g. Python312) where users installed camoufox/patchright.
        candidates: list[str] = [sys.executable]
        for p in self._windows_where_python():
            if p.lower() != sys.executable.lower():
                candidates.append(p)
        # As a last resort, try PATH resolution.
        candidates.append("python")

        # De-duplicate while preserving order.
        dedup: list[str] = []
        seen: set[str] = set()
        for p in candidates:
            k = p.lower()
            if k in seen:
                continue
            seen.add(k)
            dedup.append(p)
        candidates = dedup

        def _pick_with(modules: list[str]) -> str | None:
            for exe in candidates:
                if self._can_import(exe, modules):
                    return exe
            return None

        self._actual_browser_type = desired

        if desired == "camoufox":
            # Prefer patchright if possible.
            exe = _pick_with(["quart", "camoufox", "patchright"])
            if exe:
                self._python_exe = exe
                return

            exe = _pick_with(["quart", "camoufox", "playwright"])
            if exe:
                self._python_exe = exe
                return

            # No camoufox in any known interpreter; fallback to chromium.
            logger.warning("Camoufox not available. Falling back solver browser to chromium.")
            self._actual_browser_type = "chromium"

        # For chromium/chrome/msedge, prefer patchright if available.
        exe = _pick_with(["quart", "patchright"])
        if exe:
            self._python_exe = exe
            return

        exe = _pick_with(["quart", "playwright"])
        if exe:
            self._python_exe = exe
            return

        # Last resort: current interpreter (may fail fast with a clear error from the solver process).
        self._python_exe = sys.executable

    def _ensure_playwright_browsers(self, python_exe: str) -> None:
        """Ensure Playwright browser binaries exist (best-effort).

        We only auto-install for bundled Chromium. Branded channels (chrome/msedge)
        rely on system-installed browsers.
        """
        if self._actual_browser_type != "chromium":
            return

        browser_paths = [
            DATA_DIR / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright",
        ]
        if any(path.exists() and any(path.iterdir()) for path in browser_paths):
            return

        try:
            logger.info("Installing Playwright Chromium (first run)...")
            args = [python_exe, "-m", "playwright", "install"]
            # On Linux (Docker), install system deps as well.
            if sys.platform.startswith("linux"):
                args.append("--with-deps")
            args.append("chromium")
            subprocess.check_call(args, cwd=str(self._repo_root))
        except Exception as exc:
            raise RuntimeError(f"Playwright browser install failed: {exc}") from exc

    def _parse_host_port(self) -> tuple[str, int]:
        parsed = urlparse(self.config.url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 5072
        return host, int(port)

    def _build_command(self, host: str, port: int) -> list[str]:
        """构造 solver 启动命令。

        自动启动场景只透传显式配置的代理地址，不启用 solver 的文件代理回退。
        """
        cmd = [
            self._python_exe,
            str(self._script_path()),
            "--browser_type",
            self._actual_browser_type,
            "--thread",
            str(self.config.threads),
        ]
        if self.config.proxy_url:
            cmd += ["--proxy-url", self.config.proxy_url]
        if self.config.debug:
            cmd.append("--debug")
        cmd += ["--host", host, "--port", str(port)]
        return cmd

    def start(self) -> None:
        if not self.config.auto_start:
            return

        host, port = self._parse_host_port()

        def _spawn() -> None:
            script = self._script_path()
            if not script.exists():
                raise RuntimeError(f"Solver script not found: {script}")

            # Ensure Playwright browsers are present before starting the solver process.
            self._ensure_playwright_browsers(self._python_exe)

            cmd = self._build_command(host, port)

            logger.info("Starting Turnstile solver: {}", " ".join(cmd))
            kwargs = {
                "cwd": str(script.parent),
            }
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            self._process = subprocess.Popen(
                cmd,
                **kwargs,
            )
            self._started_by_us = True

            if not _wait_for_port(host, port, timeout=60.0):
                exit_code = self._process.poll() if self._process else None
                self.stop()
                if exit_code is not None:
                    raise RuntimeError(
                        f"Turnstile solver exited early (code {exit_code}). "
                        "Please check solver dependencies."
                    )
                raise RuntimeError("Turnstile solver did not become ready in time")

        # Decide runtime + browser strategy before checking readiness.
        self._select_runtime()
        logger.info(
            "Turnstile solver runtime selected: python={} browser_type={}",
            self._python_exe,
            self._actual_browser_type,
        )

        if _wait_for_port(host, port, timeout=1.0):
            logger.info("Turnstile solver already running at {}:{}", host, port)
            self._started_by_us = False
            return

        try:
            _spawn()
            return
        except Exception as exc:
            # camoufox is not always stable/available across environments (notably Docker).
            # Fall back to chromium instead of failing the whole auto-register workflow.
            if self._actual_browser_type != "camoufox":
                raise
            logger.warning("Camoufox solver failed to start; falling back to chromium: {}", exc)
            try:
                self.stop()
            except Exception:
                pass
            self.config.browser_type = "chromium"
            self._actual_browser_type = "chromium"
            self._select_runtime()
            logger.info(
                "Turnstile solver runtime selected: python={} browser_type={}",
                self._python_exe,
                self._actual_browser_type,
            )
            _spawn()

    def stop(self) -> None:
        if not self._process or not self._started_by_us:
            self._process = None
            self._started_by_us = False
            return

        process = self._process
        try:
            logger.info("Stopping Turnstile solver...")
            self._terminate_process(process)
            process.wait(timeout=10)
        except Exception:
            try:
                self._kill_process(process)
                process.wait(timeout=5)
            except Exception:
                pass
        finally:
            self._process = None
            self._started_by_us = False

    def _terminate_process(self, process: subprocess.Popen) -> None:
        if sys.platform.startswith("win"):
            process.terminate()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            process.terminate()

    def _kill_process(self, process: subprocess.Popen) -> None:
        if sys.platform.startswith("win"):
            process.kill()
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            process.kill()
