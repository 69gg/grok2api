"""Grok account registration runner."""
from __future__ import annotations

import concurrent.futures
import random
import re
import string
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from grok2api.core.logger import logger
from grok2api.core.proxy import get_proxies_dict
from grok2api.services.register.services import (
    EmailService,
    TurnstileService,
    UserAgreementService,
    GrokSetupService,
)


SITE_URL = "https://accounts.x.ai"
DEFAULT_IMPERSONATE = "chrome120"

CHROME_PROFILES = [
    {"impersonate": "chrome110", "version": "110.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome119", "version": "119.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome120", "version": "120.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome131", "version": "131.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome136", "version": "136.0.0.0", "brand": "chrome"},
    {"impersonate": "edge99", "version": "99.0.1150.36", "brand": "edge"},
    {"impersonate": "edge101", "version": "101.0.1210.47", "brand": "edge"},
]

# curl_cffi impersonate 候选（init 失败时轮换，规避偶发 TLS / CF 拦截）
_INIT_IMPERSONATES: Tuple[str, ...] = (
    "chrome120",
    "chrome131",
    "chrome136",
    "chrome119",
    "chrome110",
    "edge101",
)


def _random_chrome_profile() -> Tuple[str, str]:
    profile = random.choice(CHROME_PROFILES)
    if profile.get("brand") == "edge":
        chrome_major = profile["version"].split(".")[0]
        chrome_version = f"{chrome_major}.0.0.0"
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36 Edg/{profile['version']}"
        )
    else:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{profile['version']} Safari/537.36"
        )
    return profile["impersonate"], ua


def _generate_random_name() -> str:
    length = random.randint(4, 6)
    return random.choice(string.ascii_uppercase) + "".join(
        random.choice(string.ascii_lowercase) for _ in range(length - 1)
    )


def _generate_random_string(length: int = 15) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def _encode_grpc_message(field_id: int, string_value: str) -> bytes:
    key = (field_id << 3) | 2
    value_bytes = string_value.encode("utf-8")
    payload = struct.pack("B", key) + struct.pack("B", len(value_bytes)) + value_bytes
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def _encode_grpc_message_verify(email: str, code: str) -> bytes:
    p1 = struct.pack("B", (1 << 3) | 2) + struct.pack("B", len(email)) + email.encode("utf-8")
    p2 = struct.pack("B", (2 << 3) | 2) + struct.pack("B", len(code)) + code.encode("utf-8")
    payload = p1 + p2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


class RegisterRunner:
    """Threaded registration runner."""

    def __init__(
        self,
        target_count: int = 100,
        thread_count: int = 8,
        on_success: Optional[Callable[[str, str, str, int, int], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self.target_count = max(1, int(target_count))
        self.thread_count = max(1, int(thread_count))
        self.on_success = on_success
        self.on_error = on_error
        self.stop_event = stop_event or threading.Event()

        self._post_lock = threading.Lock()
        self._result_lock = threading.Lock()

        self._success_count = 0
        self._start_time = 0.0
        self._tokens: List[str] = []
        self._accounts: List[Dict[str, str]] = []

        self._config: Dict[str, Optional[str]] = {
            "site_key": "0x4AAAAAAAhr9JGVDZbrZOo0",
            "action_id": None,
            "state_tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2Fsign-up%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D",
        }
        self._config_ready: bool = False

    @property
    def success_count(self) -> int:
        return self._success_count

    @property
    def tokens(self) -> List[str]:
        return list(self._tokens)

    @property
    def accounts(self) -> List[Dict[str, str]]:
        return list(self._accounts)

    def _record_success(self, email: str, password: str, token: str) -> None:
        with self._result_lock:
            if self._success_count >= self.target_count:
                if not self.stop_event.is_set():
                    self.stop_event.set()
                return

            self._success_count += 1
            self._tokens.append(token)
            self._accounts.append({"email": email, "password": password, "token": token})

            avg = (time.time() - self._start_time) / max(1, self._success_count)
            logger.info(
                "Register success: {} | sso={}... | avg={:.1f}s ({}/{})",
                email,
                token[:12],
                avg,
                self._success_count,
                self.target_count,
            )

            if self.on_success:
                try:
                    self.on_success(email, password, token, self._success_count, self.target_count)
                except Exception:
                    pass

            if self._success_count >= self.target_count and not self.stop_event.is_set():
                self.stop_event.set()

    def _record_error(self, message: str) -> None:
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                pass

    def _init_config_once(self, impersonate: str) -> None:
        """Single attempt to fetch sitekey / state_tree / action_id from sign-up page."""
        start_url = f"{SITE_URL}/sign-up"
        proxies = get_proxies_dict()
        session_kwargs: Dict[str, object] = {"impersonate": impersonate}
        if proxies:
            session_kwargs["proxies"] = proxies

        with curl_requests.Session(**session_kwargs) as session:  # type: ignore[arg-type]
            resp = session.get(start_url, timeout=20)
            status = getattr(resp, "status_code", None)
            html = resp.text or ""
            logger.debug(
                "Register init page impersonate={} status={} html_len={} proxy={}",
                impersonate,
                status,
                len(html),
                bool(proxies),
            )
            if status and int(status) >= 400:
                raise RuntimeError(f"sign-up HTTP {status} (html_len={len(html)})")
            if len(html) < 500:
                raise RuntimeError(f"sign-up page too short (status={status}, html_len={len(html)})")

            key_match = re.search(r'sitekey":"(0x4[a-zA-Z0-9_-]+)"', html)
            if key_match:
                self._config["site_key"] = key_match.group(1)

            tree_match = re.search(r'next-router-state-tree":"([^"]+)"', html)
            if tree_match:
                self._config["state_tree"] = tree_match.group(1)

            soup = BeautifulSoup(html, "html.parser")
            js_urls = [
                urljoin(start_url, script["src"])
                for script in soup.find_all("script", src=True)
                if "_next/static" in script["src"]
            ]
            if not js_urls:
                raise RuntimeError("no Next.js static scripts found on sign-up page")

            for js_url in js_urls:
                if self.stop_event.is_set():
                    raise RuntimeError("stopped during init")
                try:
                    js_content = session.get(js_url, timeout=20).text or ""
                except Exception as exc:
                    logger.debug("Register init JS fetch failed url={} err={}", js_url, exc)
                    continue
                match = re.search(r"7f[a-fA-F0-9]{40}", js_content)
                if match:
                    self._config["action_id"] = match.group(0)
                    logger.info(
                        "Register: Action ID found: {} (impersonate={})",
                        self._config["action_id"],
                        impersonate,
                    )
                    break

        if not self._config.get("action_id"):
            raise RuntimeError("missing action_id after parsing sign-up assets")

    def _init_config(self, max_attempts: int = 6) -> None:
        """Fetch registration action config with retries / fingerprint rotation.

        curl_cffi can hit intermittent TLS errors (curl 35 OPENSSL_internal) especially
        when heavy browser processes are also starting. Retries + impersonate rotation
        make init resilient; callers should prefer running this *before* starting solver.
        """
        if self._config_ready and self._config.get("action_id"):
            logger.debug("Register: action config already ready, skip init")
            return

        logger.info("Register: initializing action config (max_attempts={})...", max_attempts)
        last_error: Optional[BaseException] = None

        for attempt in range(1, max_attempts + 1):
            if self.stop_event.is_set():
                raise RuntimeError("Register stopped during init")

            impersonate = _INIT_IMPERSONATES[(attempt - 1) % len(_INIT_IMPERSONATES)]
            try:
                self._init_config_once(impersonate)
                self._config_ready = True
                logger.info(
                    "Register: action config ready (attempt={}/{}, site_key={}, action_id={})",
                    attempt,
                    max_attempts,
                    (self._config.get("site_key") or "")[:20],
                    self._config.get("action_id"),
                )
                return
            except Exception as exc:
                last_error = exc
                self._config["action_id"] = None
                self._config_ready = False
                logger.warning(
                    "Register init attempt {}/{} failed (impersonate={}): {}",
                    attempt,
                    max_attempts,
                    impersonate,
                    exc,
                )
                # Backoff; keep solver/browser load from racing TLS retries too hard.
                delay = min(1.5 * attempt, 8.0)
                for _ in range(int(delay * 10)):
                    if self.stop_event.is_set():
                        raise RuntimeError("Register stopped during init backoff")
                    time.sleep(0.1)

        raise RuntimeError(
            f"Register init failed after {max_attempts} attempts: {last_error}"
        )
    def _send_email_code(self, session: curl_requests.Session, email: str) -> bool:
        url = f"{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
        data = _encode_grpc_message(1, email)
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/sign-up?redirect=grok-com",
        }
        try:
            res = session.post(url, data=data, headers=headers, timeout=15)
            return res.status_code == 200
        except Exception as exc:
            self._record_error(f"send code error: {email} - {exc}")
            return False

    def _verify_email_code(self, session: curl_requests.Session, email: str, code: str) -> bool:
        url = f"{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
        data = _encode_grpc_message_verify(email, code)
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/sign-up?redirect=grok-com",
        }
        try:
            res = session.post(url, data=data, headers=headers, timeout=15)
            return res.status_code == 200
        except Exception as exc:
            self._record_error(f"verify code error: {email} - {exc}")
            return False

    def _register_single_thread(self) -> None:
        time.sleep(random.uniform(0, 5))

        email_service: Optional[EmailService] = None
        turnstile_service: Optional[TurnstileService] = None
        user_agreement_service: Optional[UserAgreementService] = None
        grok_setup_service: Optional[GrokSetupService] = None

        # Keep retrying forever until target is met or stop_event is set.
        while not self.stop_event.is_set():
            try:
                if email_service is None:
                    email_service = EmailService()
                    turnstile_service = TurnstileService()
                    user_agreement_service = UserAgreementService()
                    grok_setup_service = GrokSetupService()

                final_action_id = self._config.get("action_id")
                if not final_action_id:
                    self._record_error("missing action id")
                    time.sleep(5)
                    continue

                assert turnstile_service is not None
                assert user_agreement_service is not None
                assert grok_setup_service is not None

                impersonate_fingerprint, account_user_agent = _random_chrome_profile()

                with curl_requests.Session(
                    impersonate=impersonate_fingerprint,
                    proxies=get_proxies_dict() or {},
                ) as session:
                    try:
                        session.get(SITE_URL, timeout=10)
                    except Exception:
                        pass

                    password = _generate_random_string()

                    jwt, email = email_service.create_email()
                    if not email:
                        self._record_error("create_email failed")
                        time.sleep(5)
                        continue

                    if self.stop_event.is_set():
                        return

                    if not self._send_email_code(session, email):
                        self._record_error(f"send_email_code failed: {email}")
                        time.sleep(5)
                        continue

                    verify_code = None
                    for _ in range(30):
                        time.sleep(1)
                        if self.stop_event.is_set():
                            return
                        content = email_service.fetch_first_email(jwt)
                        if content:
                            match = re.search(r">([A-Z0-9]{3}-[A-Z0-9]{3})<", content)
                            if match:
                                verify_code = match.group(1).replace("-", "")
                                break

                    if not verify_code:
                        self._record_error(f"verify_code not received: {email}")
                        time.sleep(3)
                        continue

                    if not self._verify_email_code(session, email, verify_code):
                        self._record_error(f"verify_email_code failed: {email}")
                        time.sleep(3)
                        continue

                    registered = False
                    for _ in range(5):
                        if self.stop_event.is_set():
                            return

                        try:
                            task_id = turnstile_service.create_task(
                                f"{SITE_URL}/sign-up",
                                self._config["site_key"] or "",
                            )
                        except Exception as exc:
                            self._record_error(f"turnstile create_task failed: {exc}")
                            time.sleep(2)
                            continue

                        token = turnstile_service.get_response(
                            task_id, stop_event=self.stop_event
                        )

                        if not token:
                            self._record_error(
                                f"turnstile failed: {turnstile_service.last_error or 'no token'}"
                            )
                            time.sleep(2)
                            continue

                        headers = {
                            "user-agent": account_user_agent,
                            "accept": "text/x-component",
                            "content-type": "text/plain;charset=UTF-8",
                            "origin": SITE_URL,
                            "referer": f"{SITE_URL}/sign-up",
                            "cookie": f"__cf_bm={session.cookies.get('__cf_bm','')}",
                            "next-router-state-tree": self._config["state_tree"] or "",
                            "next-action": final_action_id,
                        }
                        payload = [
                            {
                                "emailValidationCode": verify_code,
                                "createUserAndSessionRequest": {
                                    "email": email,
                                    "givenName": _generate_random_name(),
                                    "familyName": _generate_random_name(),
                                    "clearTextPassword": password,
                                    "tosAcceptedVersion": "$undefined",
                                },
                                "turnstileToken": token,
                                "promptOnDuplicateEmail": True,
                            }
                        ]

                        with self._post_lock:
                            try:
                                res = session.post(
                                    f"{SITE_URL}/sign-up",
                                    json=payload,
                                    headers=headers,
                                    timeout=20,
                                )
                            except Exception as exc:
                                self._record_error(f"sign_up post error: {exc}")
                                time.sleep(3)
                                continue

                        if res.status_code != 200:
                            self._record_error(f"sign_up http {res.status_code}")
                            time.sleep(3)
                            continue

                        match = re.search(
                            r'(https://[^" \s]+set-cookie\?q=[^:" \s]+)1:', res.text
                        )
                        if not match:
                            self._record_error("sign_up missing set-cookie redirect")
                            time.sleep(2)
                            continue

                        verify_url = match.group(1)
                        logger.info("Register: verify_url={}", verify_url[:80])

                        # 浏览器独占 verify_url：完成重定向拿 sso + 做 birth_date/nsfw
                        setup_result = grok_setup_service.setup(
                            sso="",
                            sso_rw="",
                            verify_url=verify_url,
                            accounts_cookies={k: v for k, v in session.cookies.items()},
                            stop_event=self.stop_event,
                        )

                        sso = setup_result.get("sso", "")
                        sso_rw = setup_result.get("sso_rw", "")
                        if not sso:
                            self._record_error("grok_setup: no sso cookie from browser")
                            time.sleep(2)
                            continue

                        logger.info("Register: browser sso ok, len={}", len(sso))

                        if not setup_result.get("ok"):
                            logger.warning(
                                "Register: grok_setup partial: {}",
                                setup_result.get("error") or "unknown",
                            )

                        tos_result = user_agreement_service.accept_tos_version(
                            sso=sso,
                            sso_rw=sso_rw or "",
                            impersonate=impersonate_fingerprint,
                            user_agent=account_user_agent,
                        )
                        if not tos_result.get("ok") or not tos_result.get("hex_reply"):
                            self._record_error(
                                f"accept_tos failed: {tos_result.get('error') or 'unknown'}"
                            )
                            time.sleep(2)
                            continue

                        self._record_success(email, password, sso)
                        registered = True
                        break

                    if not registered and not self.stop_event.is_set():
                        # Exhausted turnstile/sign-up attempts for this email; try another.
                        time.sleep(2)

            except Exception as exc:
                # Transient service/network failures: recreate services next loop.
                email_service = None
                turnstile_service = None
                user_agreement_service = None
                grok_setup_service = None
                self._record_error(f"thread error: {str(exc)[:120]}")
                time.sleep(3)

    def run(self, *, skip_init: bool = False) -> List[str]:
        """Run the registration process and return collected tokens.

        Args:
            skip_init: When True, assume `_init_config()` was already called successfully
                (e.g. manager fetched action config before starting the local solver).
        """
        if not skip_init or not self._config_ready:
            self._init_config()
        self._start_time = time.time()

        logger.info("Register: starting {} threads, target {}", self.thread_count, self.target_count)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as executor:
            futures = [executor.submit(self._register_single_thread) for _ in range(self.thread_count)]
            concurrent.futures.wait(futures)

        return list(self._tokens)
