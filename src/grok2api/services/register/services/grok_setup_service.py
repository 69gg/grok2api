"""Grok account setup service (birth_date + nsfw via browser solver)."""
from __future__ import annotations

import os
import time
import threading
from typing import Any, Dict, Optional

import requests

from grok2api.core.config import get_config
from grok2api.core.logger import logger


class GrokSetupService:
    """Set birth_date and nsfw via the turnstile solver browser."""

    def __init__(self, solver_url: Optional[str] = None) -> None:
        self.solver_url = (
            solver_url
            or get_config("register.solver_url", "")
            or os.getenv("TURNSTILE_SOLVER_URL", "")
            or "http://127.0.0.1:5072"
        ).strip()
        self.last_error: Optional[str] = None

    def setup(
        self,
        sso: str,
        sso_rw: str,
        verify_url: str = "",
        accounts_cookies: Optional[Dict[str, str]] = None,
        max_retries: int = 40,
        initial_delay: int = 15,
        retry_delay: int = 2,
        stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """
        Use browser to set birth_date and nsfw on grok.com.
        Returns {"ok": bool, "birth_ok": bool, "nsfw_ok": bool, "error": str|None}
        """
        self.last_error = None

        try:
            resp = requests.post(
                f"{self.solver_url}/grok_setup",
                json={
                    "sso": sso,
                    "sso_rw": sso_rw,
                    "verify_url": verify_url,
                    "accounts_cookies": accounts_cookies or {},
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.last_error = str(exc)
            return {"ok": False, "birth_ok": False, "nsfw_ok": False, "error": self.last_error}

        if data.get("errorId") != 0:
            self.last_error = data.get("errorDescription") or "grok_setup create failed"
            return {"ok": False, "birth_ok": False, "nsfw_ok": False, "error": self.last_error}

        task_id = data.get("taskId")
        if not task_id:
            self.last_error = "missing taskId"
            return {"ok": False, "birth_ok": False, "nsfw_ok": False, "error": self.last_error}

        # initial wait
        for _ in range(int(initial_delay * 10)):
            if stop_event is not None and stop_event.is_set():
                return {"ok": False, "birth_ok": False, "nsfw_ok": False, "error": "stopped"}
            time.sleep(0.1)

        for _ in range(max_retries):
            if stop_event is not None and stop_event.is_set():
                return {"ok": False, "birth_ok": False, "nsfw_ok": False, "error": "stopped"}
            try:
                resp = requests.get(
                    f"{self.solver_url}/result",
                    params={"id": task_id},
                    timeout=20,
                )
                resp.raise_for_status()
                result = resp.json()

                if result.get("errorId") not in (None, 0):
                    self.last_error = result.get("errorDescription") or "solver error"
                    return {"ok": False, "birth_ok": False, "nsfw_ok": False, "error": self.last_error}

                token = result.get("solution", {}).get("token")
                if token == "done":
                    sol = result.get("solution", {})
                    return {
                        "ok": True,
                        "birth_ok": sol.get("birth_ok", True),
                        "nsfw_ok": sol.get("nsfw_ok", True),
                        "sso": sol.get("sso", ""),
                        "sso_rw": sol.get("sso_rw", ""),
                        "error": None,
                    }
                if token == "CAPTCHA_FAIL":
                    self.last_error = "grok_setup browser failed"
                    return {"ok": False, "birth_ok": False, "nsfw_ok": False, "error": self.last_error}

                # still processing
                for _ in range(int(retry_delay * 10)):
                    if stop_event is not None and stop_event.is_set():
                        return {"ok": False, "birth_ok": False, "nsfw_ok": False, "error": "stopped"}
                    time.sleep(0.1)

            except Exception as exc:
                self.last_error = str(exc)
                logger.debug("GrokSetupService poll error: {}", exc)
                time.sleep(retry_delay)

        self.last_error = "grok_setup timeout"
        return {"ok": False, "birth_ok": False, "nsfw_ok": False, "error": self.last_error}
