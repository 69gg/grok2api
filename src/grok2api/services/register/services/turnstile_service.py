"""Turnstile solving service."""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from grok2api.core.logger import logger

import requests

from grok2api.core.config import get_config


def _token_preview(token: str | None, keep: int = 12) -> str:
    if not token:
        return "<empty>"
    token = str(token)
    if token == "CAPTCHA_FAIL":
        return "CAPTCHA_FAIL"
    if len(token) <= keep * 2:
        return token
    return f"{token[:keep]}...{token[-4:]}(len={len(token)})"


def _summarize_solver_payload(data: Any) -> str:
    if not isinstance(data, dict):
        return f"type={type(data).__name__} value={data!r}"
    keys = sorted(str(k) for k in data.keys())
    solution = data.get("solution") if isinstance(data.get("solution"), dict) else {}
    token = solution.get("token") if isinstance(solution, dict) else None
    parts = [
        f"keys={keys}",
        f"status={data.get('status')!r}",
        f"errorId={data.get('errorId')!r}",
        f"errorCode={data.get('errorCode')!r}",
        f"errorDescription={data.get('errorDescription')!r}",
    ]
    if token is not None:
        parts.append(f"token={_token_preview(str(token))}")
    if data.get("elapsed_time") is not None:
        parts.append(f"elapsed_time={data.get('elapsed_time')}")
    return " ".join(parts)


class TurnstileService:
    """Turnstile solver wrapper (local solver or YesCaptcha)."""

    def __init__(
        self,
        solver_url: Optional[str] = None,
        yescaptcha_key: Optional[str] = None,
    ) -> None:
        self.yescaptcha_key = (
            (yescaptcha_key or get_config("register.yescaptcha_key", "") or os.getenv("YESCAPTCHA_KEY", "")).strip()
        )
        self.solver_url = (
            solver_url
            or get_config("register.solver_url", "")
            or os.getenv("TURNSTILE_SOLVER_URL", "")
            or "http://127.0.0.1:5072"
        ).strip()
        self.yescaptcha_api = "https://api.yescaptcha.com"
        self.last_error: Optional[str] = None
        self._debug = bool(get_config("register.solver_debug", False))

    def create_task(self, siteurl: str, sitekey: str) -> str:
        """Create a Turnstile task and return task ID."""
        self.last_error = None
        if self.yescaptcha_key:
            url = f"{self.yescaptcha_api}/createTask"
            payload = {
                "clientKey": self.yescaptcha_key,
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": siteurl,
                    "websiteKey": sitekey,
                },
            }
            logger.debug(
                "Turnstile YesCaptcha createTask siteurl={} sitekey={}",
                siteurl,
                sitekey[:12] + "..." if len(sitekey) > 12 else sitekey,
            )
            response = requests.post(url, json=payload, timeout=20)
            response.raise_for_status()
            data = response.json()
            if data.get("errorId") != 0:
                desc = data.get("errorDescription") or "unknown"
                self.last_error = f"YesCaptcha createTask failed: {desc}"
                raise RuntimeError(self.last_error)
            task_id = data["taskId"]
            logger.debug("Turnstile YesCaptcha createTask ok task_id={}", task_id)
            return task_id

        create_url = f"{self.solver_url.rstrip('/')}/turnstile"
        logger.info(
            "Turnstile local create_task url={} siteurl={} sitekey={}",
            create_url,
            siteurl,
            sitekey[:16] + "..." if len(sitekey) > 16 else sitekey,
        )
        t0 = time.time()
        response = requests.get(
            create_url,
            params={"url": siteurl, "sitekey": sitekey},
            timeout=20,
        )
        elapsed_ms = round((time.time() - t0) * 1000, 1)
        logger.debug(
            "Turnstile local create_task http status={} elapsed_ms={} body={}",
            response.status_code,
            elapsed_ms,
            (response.text or "")[:300],
        )
        response.raise_for_status()
        data = response.json()
        task_id = data.get("taskId")
        if not task_id:
            self.last_error = data.get("errorDescription") or data.get("errorCode") or "missing taskId"
            logger.warning(
                "Turnstile local create_task failed: {} payload={}",
                self.last_error,
                _summarize_solver_payload(data),
            )
            raise RuntimeError(f"Solver create task failed: {self.last_error}")
        logger.info("Turnstile local create_task ok task_id={} elapsed_ms={}", task_id, elapsed_ms)
        return task_id

    def get_response(
        self,
        task_id: str,
        max_retries: int = 30,
        initial_delay: int = 5,
        retry_delay: int = 2,
        stop_event: object | None = None,
    ) -> Optional[str]:
        """Fetch a Turnstile solution token."""
        self.last_error = None
        backend = "yescaptcha" if self.yescaptcha_key else "local"
        logger.info(
            "Turnstile get_response start backend={} task_id={} max_retries={} initial_delay={}s retry_delay={}s",
            backend,
            task_id,
            max_retries,
            initial_delay,
            retry_delay,
        )
        # Make shutdown/cancel responsive.
        if initial_delay > 0:
            for _ in range(int(initial_delay * 10)):
                if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                    logger.debug("Turnstile get_response cancelled during initial_delay task_id={}", task_id)
                    return None
                time.sleep(0.1)

        for attempt in range(1, max_retries + 1):
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                logger.debug("Turnstile get_response cancelled task_id={} attempt={}", task_id, attempt)
                return None
            try:
                if self.yescaptcha_key:
                    url = f"{self.yescaptcha_api}/getTaskResult"
                    payload = {"clientKey": self.yescaptcha_key, "taskId": task_id}
                    response = requests.post(url, json=payload, timeout=20)
                    response.raise_for_status()
                    data = response.json()

                    if data.get("errorId") != 0:
                        self.last_error = str(data.get("errorDescription") or "unknown")
                        logger.warning("YesCaptcha getTaskResult failed: {}", self.last_error)
                        return None

                    status = data.get("status")
                    if status == "ready":
                        token = data.get("solution", {}).get("token")
                        if token:
                            logger.info(
                                "Turnstile YesCaptcha ready task_id={} token={}",
                                task_id,
                                _token_preview(token),
                            )
                            return token
                        self.last_error = "YesCaptcha returned empty token"
                        logger.warning(self.last_error)
                        return None
                    if status == "processing":
                        logger.debug(
                            "Turnstile YesCaptcha processing task_id={} attempt={}/{}",
                            task_id,
                            attempt,
                            max_retries,
                        )
                        if retry_delay > 0:
                            for _ in range(int(retry_delay * 10)):
                                if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                                    return None
                                time.sleep(0.1)
                        continue
                    self.last_error = f"YesCaptcha unexpected status: {status}"
                    logger.warning(self.last_error)
                    if retry_delay > 0:
                        for _ in range(int(retry_delay * 10)):
                            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                                return None
                            time.sleep(0.1)
                    continue

                result_url = f"{self.solver_url.rstrip('/')}/result"
                t0 = time.time()
                response = requests.get(
                    result_url,
                    params={"id": task_id},
                    timeout=20,
                )
                elapsed_ms = round((time.time() - t0) * 1000, 1)
                response.raise_for_status()
                data = response.json()

                # Solver error -> stop early (avoid polling forever on unsolvable tasks).
                error_id = data.get("errorId")
                if error_id is not None and error_id != 0:
                    self.last_error = str(
                        data.get("errorDescription") or data.get("errorCode") or "solver error"
                    )
                    logger.warning(
                        "Turnstile local result FAILED task_id={} attempt={}/{} http_ms={} {}",
                        task_id,
                        attempt,
                        max_retries,
                        elapsed_ms,
                        _summarize_solver_payload(data),
                    )
                    return None

                token = data.get("solution", {}).get("token") if isinstance(data.get("solution"), dict) else None
                if token:
                    if token != "CAPTCHA_FAIL":
                        logger.info(
                            "Turnstile local result READY task_id={} attempt={}/{} token={} http_ms={}",
                            task_id,
                            attempt,
                            max_retries,
                            _token_preview(token),
                            elapsed_ms,
                        )
                        return token
                    self.last_error = "CAPTCHA_FAIL"
                    logger.warning(
                        "Turnstile local result CAPTCHA_FAIL task_id={} attempt={}/{} http_ms={} {}",
                        task_id,
                        attempt,
                        max_retries,
                        elapsed_ms,
                        _summarize_solver_payload(data),
                    )
                    return None

                # processing / empty solution
                if self._debug or attempt == 1 or attempt % 5 == 0 or attempt == max_retries:
                    logger.debug(
                        "Turnstile local result poll task_id={} attempt={}/{} http_ms={} {}",
                        task_id,
                        attempt,
                        max_retries,
                        elapsed_ms,
                        _summarize_solver_payload(data),
                    )
                if retry_delay > 0:
                    for _ in range(int(retry_delay * 10)):
                        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                            return None
                        time.sleep(0.1)
            except Exception as exc:  # pragma: no cover - network/remote errors
                self.last_error = str(exc)
                logger.warning(
                    "Turnstile get_response network error task_id={} attempt={}/{}: {}",
                    task_id,
                    attempt,
                    max_retries,
                    exc,
                )
                if retry_delay > 0:
                    for _ in range(int(retry_delay * 10)):
                        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                            return None
                        time.sleep(0.1)

        self.last_error = self.last_error or f"timeout after {max_retries} polls"
        logger.warning(
            "Turnstile get_response timeout backend={} task_id={} last_error={}",
            backend,
            task_id,
            self.last_error,
        )
        return None
