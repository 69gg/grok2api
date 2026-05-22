"""Email service for temporary inbox creation."""
from __future__ import annotations

import os
import random
import string
from typing import Tuple, Optional
from urllib.parse import urljoin, urlparse

import requests

from grok2api.core.config import get_config


def _normalize_worker_base_url(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    if "://" not in value:
        value = f"https://{value}"

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "register.worker_domain 必须是域名，或以 http:// / https:// 开头的完整 URL"
        )

    return value.rstrip("/")


def _build_worker_url(base_url: str, path: str) -> str:
    return urljoin(f"{base_url}/", path.lstrip("/"))


class EmailService:
    """Email service wrapper."""

    def __init__(
        self,
        worker_domain: Optional[str] = None,
        email_domain: Optional[str] = None,
        admin_password: Optional[str] = None,
    ) -> None:
        self.worker_domain = (
            (worker_domain or get_config("register.worker_domain", "") or os.getenv("WORKER_DOMAIN", "")).strip()
        )
        self.worker_base_url = _normalize_worker_base_url(self.worker_domain)
        self.email_domain = (
            (email_domain or get_config("register.email_domain", "") or os.getenv("EMAIL_DOMAIN", "")).strip()
        )
        self.admin_password = (
            (admin_password or get_config("register.admin_password", "") or os.getenv("ADMIN_PASSWORD", "")).strip()
        )

        if not all([self.worker_domain, self.email_domain, self.admin_password]):
            raise ValueError(
                "Missing required email settings: register.worker_domain, register.email_domain, "
                "register.admin_password"
            )

    def _generate_random_name(self) -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 6)))
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(0, 5)))
        return letters1 + numbers + letters2

    def create_email(self) -> Tuple[Optional[str], Optional[str]]:
        """Create a temporary mailbox. Returns (jwt, address)."""
        url = _build_worker_url(self.worker_base_url, "/admin/new_address")
        try:
            random_name = self._generate_random_name()
            res = requests.post(
                url,
                json={
                    "enablePrefix": True,
                    "name": random_name,
                    "domain": self.email_domain,
                },
                headers={
                    "x-admin-auth": self.admin_password,
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("jwt"), data.get("address")
            print(f"[-] Email create failed: {res.status_code} - {res.text}")
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"[-] Email create error ({url}): {exc}")
        return None, None

    def fetch_first_email(self, jwt: str) -> Optional[str]:
        """Fetch the first email content for the mailbox."""
        try:
            res = requests.get(
                _build_worker_url(self.worker_base_url, "/api/mails"),
                params={"limit": 10, "offset": 0},
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("results"):
                    return data["results"][0].get("raw")
            return None
        except Exception as exc:  # pragma: no cover - network/remote errors
            print(f"Email fetch failed: {exc}")
            return None
