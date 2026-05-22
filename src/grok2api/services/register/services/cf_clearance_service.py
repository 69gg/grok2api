"""CF clearance cookie refresh service."""
from __future__ import annotations

import os
import time
import threading
from typing import Any, Dict, Optional

import requests

from grok2api.core.config import get_config
from grok2api.core.logger import logger


class CfClearanceService:
    """Obtain cf_clearance cookie via the turnstile solver browser."""

    def __init__(self, solver_url: Optional[str] = None) -> None:
        self.solver_url = (
            solver_url
            or get_config("register.solver_url", "")
            or os.getenv("TURNSTILE_SOLVER_URL", "")
            or "http://127.0.0.1:5072"
        ).strip()
        self.last_error: Optional[str] = None

    @staticmethod
    def _sleep_with_stop(seconds: int | float, stop_event: Optional[threading.Event]) -> bool:
        for _ in range(int(max(0.0, float(seconds)) * 10)):
            if stop_event is not None and stop_event.is_set():
                return False
            time.sleep(0.1)
        return True

    def refresh(
        self,
        stop_event: Optional[threading.Event] = None,
        max_retries: int = 60,
        initial_delay: int = 10,
        retry_delay: int = 2,
    ) -> Dict[str, Any]:
        """
        Call solver's /cf_clearance POST, poll /result for the cookie.

        Returns {"ok": bool, "cf_clearance": str, "error": str|None}
        """
        self.last_error = None

        try:
            resp = requests.post(
                f"{self.solver_url}/cf_clearance",
                json={},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.last_error = str(exc)
            return {"ok": False, "cf_clearance": "", "error": self.last_error}

        if data.get("errorId") != 0:
            self.last_error = data.get("errorDescription") or "cf_clearance create failed"
            return {"ok": False, "cf_clearance": "", "error": self.last_error}

        task_id = data.get("taskId")
        if not task_id:
            self.last_error = "missing taskId"
            return {"ok": False, "cf_clearance": "", "error": self.last_error}

        # initial wait for CF challenge to resolve
        if not self._sleep_with_stop(initial_delay, stop_event):
            return {"ok": False, "cf_clearance": "", "error": "stopped"}

        for _ in range(max_retries):
            if stop_event is not None and stop_event.is_set():
                return {"ok": False, "cf_clearance": "", "error": "stopped"}
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
                    return {"ok": False, "cf_clearance": "", "error": self.last_error}

                token = result.get("solution", {}).get("token")
                if token == "done":
                    cf_clearance = result.get("solution", {}).get("cf_clearance", "")
                    user_agent = result.get("solution", {}).get("user_agent", "")
                    return {"ok": True, "cf_clearance": cf_clearance, "user_agent": user_agent, "error": None}
                if token == "CAPTCHA_FAIL":
                    self.last_error = "cf_clearance browser challenge failed"
                    return {"ok": False, "cf_clearance": "", "error": self.last_error}

                # still processing
                if not self._sleep_with_stop(retry_delay, stop_event):
                    return {"ok": False, "cf_clearance": "", "error": "stopped"}

            except Exception as exc:
                self.last_error = str(exc)
                logger.debug("CfClearanceService poll error: {}", exc)
                if not self._sleep_with_stop(retry_delay, stop_event):
                    return {"ok": False, "cf_clearance": "", "error": "stopped"}

        self.last_error = "cf_clearance timeout"
        return {"ok": False, "cf_clearance": "", "error": self.last_error}
