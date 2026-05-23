"""Console.x.ai auth helpers (gRPC-Web team discovery)."""

from __future__ import annotations

import re
import uuid
from typing import Optional

from curl_cffi.requests import AsyncSession

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger
from grok2api.core.proxy_pool import build_http_proxies, get_current_proxy_from
from grok2api.services.reverse.utils.grpc import GrpcClient, GrpcStatus
from grok2api.services.reverse.utils.headers import build_console_headers

_UUID_RE = re.compile(
    rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

LIST_JOINABLE_TEAMS_API = (
    "https://console.x.ai/team_management.v1.TeamManagement/ListDomainJoinableTeams"
)
GET_TEAM_API = "https://console.x.ai/team_management.v1.TeamManagement/GetTeam"


def _grpc_headers(token: str, team_id: Optional[str] = None) -> dict[str, str]:
    headers = build_console_headers(
        cookie_token=token,
        team_id=team_id or "",
        content_type="application/grpc-web+proto",
    )
    headers.update(
        {
            "Accept": "*/*",
            "Sec-Fetch-Dest": "empty",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )
    return headers


def _encode_string_field(tag: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return bytes([tag]) + bytes([len(encoded)]) + encoded


def _extract_team_ids(messages: list[bytes]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for match in _UUID_RE.finditer(message):
            team_id = match.group(0).decode("ascii")
            if team_id not in seen:
                seen.add(team_id)
                found.append(team_id)
    return found


class ConsoleAuthReverse:
    """Resolve console team context for SSO tokens."""

    @staticmethod
    async def list_joinable_team_ids(session: AsyncSession, token: str) -> list[str]:
        headers = _grpc_headers(token)
        payload = GrpcClient.encode_payload(b"")
        timeout = float(get_config("console.timeout") or get_config("chat.timeout") or 60)
        browser = get_config("proxy.browser")
        _, proxy_url = get_current_proxy_from("proxy.base_proxy_url")
        proxies = build_http_proxies(proxy_url)

        response = await session.post(
            LIST_JOINABLE_TEAMS_API,
            headers=headers,
            data=payload,
            timeout=timeout,
            proxies=proxies,
            impersonate=browser,
        )
        if response.status_code != 200:
            raise UpstreamException(
                message=f"ConsoleAuthReverse: ListDomainJoinableTeams failed, {response.status_code}",
                details={"status": response.status_code},
            )

        messages, trailers = GrpcClient.parse_response(
            response.content,
            content_type=response.headers.get("content-type"),
            headers=response.headers,
        )
        status = GrpcClient.get_status(trailers)
        if not status.ok:
            raise UpstreamException(
                message=f"ConsoleAuthReverse: ListDomainJoinableTeams grpc error {status.code}",
                details={"grpc_status": status.code, "grpc_message": status.message},
            )
        team_ids = _extract_team_ids(messages)
        logger.debug("ConsoleAuthReverse: found %d joinable teams", len(team_ids))
        return team_ids

    @staticmethod
    async def resolve_team_id(session: AsyncSession, token: str) -> str:
        configured = str(get_config("console.default_team_id") or "").strip()
        if configured:
            return configured
        team_ids = await ConsoleAuthReverse.list_joinable_team_ids(session, token)
        if not team_ids:
            raise UpstreamException(
                message="ConsoleAuthReverse: no joinable teams found",
                details={"status": 502},
            )
        return team_ids[0]

    @staticmethod
    async def verify_team(
        session: AsyncSession,
        token: str,
        team_id: str,
    ) -> GrpcStatus:
        headers = _grpc_headers(token, team_id=team_id)
        body = _encode_string_field(0x0A, team_id)
        payload = GrpcClient.encode_payload(body)
        timeout = float(get_config("console.timeout") or get_config("chat.timeout") or 60)
        browser = get_config("proxy.browser")
        _, proxy_url = get_current_proxy_from("proxy.base_proxy_url")
        proxies = build_http_proxies(proxy_url)

        response = await session.post(
            GET_TEAM_API,
            headers=headers,
            data=payload,
            timeout=timeout,
            proxies=proxies,
            impersonate=browser,
        )
        if response.status_code != 200:
            raise UpstreamException(
                message=f"ConsoleAuthReverse: GetTeam failed, {response.status_code}",
                details={"status": response.status_code},
            )
        _, trailers = GrpcClient.parse_response(
            response.content,
            content_type=response.headers.get("content-type"),
            headers=response.headers,
        )
        return GrpcClient.get_status(trailers)


async def ensure_console_team_id(
    session: AsyncSession,
    token: str,
    existing_team_id: Optional[str] = None,
) -> str:
    if existing_team_id:
        return existing_team_id
    return await ConsoleAuthReverse.resolve_team_id(session, token)


__all__ = [
    "ConsoleAuthReverse",
    "ensure_console_team_id",
]
