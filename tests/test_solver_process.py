from __future__ import annotations

from grok2api.services.register.solver import SolverConfig, TurnstileSolverProcess


def test_build_command_without_proxy_url_skips_proxy_flags() -> None:
    process = TurnstileSolverProcess(
        SolverConfig(
            url="http://127.0.0.1:5072",
            threads=10,
            browser_type="camoufox",
            auto_start=True,
            proxy_url="",
        )
    )
    process._python_exe = "/usr/bin/python3"
    process._actual_browser_type = "camoufox"

    cmd = process._build_command("127.0.0.1", 5072)

    assert "--proxy" not in cmd
    assert "--proxy-url" not in cmd


def test_build_command_with_proxy_url_uses_explicit_proxy() -> None:
    proxy_url = "http://127.0.0.1:7890"
    process = TurnstileSolverProcess(
        SolverConfig(
            url="http://127.0.0.1:5072",
            threads=10,
            browser_type="camoufox",
            auto_start=True,
            proxy_url=proxy_url,
        )
    )
    process._python_exe = "/usr/bin/python3"
    process._actual_browser_type = "camoufox"

    cmd = process._build_command("127.0.0.1", 5072)

    assert "--proxy" not in cmd
    assert "--proxy-url" in cmd
    assert cmd[cmd.index("--proxy-url") + 1] == proxy_url


def test_stop_clears_unowned_existing_process_reference() -> None:
    process = TurnstileSolverProcess(
        SolverConfig(url="http://127.0.0.1:5072", auto_start=True)
    )
    process._process = object()  # type: ignore[assignment]
    process._started_by_us = False

    process.stop()

    assert process._process is None
    assert process._started_by_us is False
