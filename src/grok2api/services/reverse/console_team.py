"""Reverse interface: console.x.ai team creation."""

from __future__ import annotations

import inspect
import re
from typing import Any, Mapping, Optional

from curl_cffi.requests import AsyncSession

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger
from grok2api.core.proxy_pool import rotate_proxy, should_rotate_proxy
from grok2api.services.reverse.console_constants import CONSOLE_BASE_URL, CONSOLE_TIMEOUT
from grok2api.services.reverse.utils.grpc import GrpcClient
from grok2api.services.reverse.utils.headers import (
    _build_console_sentry_headers,
    build_console_sso_cookie,
    resolve_proxy_browser,
)
from grok2api.services.reverse.utils.proxy import (
    CONSOLE_PROXY_KEYS,
    build_curl_cffi_proxy_kwargs,
)
from grok2api.services.reverse.utils.retry import retry_on_status


CONSOLE_CREATE_TEAM_API = f"{CONSOLE_BASE_URL}/auth_mgmt.AuthManagement/CreateTeam"
TEAM_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint value cannot be negative")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ValueError("invalid protobuf varint")


def _encode_string_field(field_number: int, value: str) -> bytes:
    value_bytes = value.encode("utf-8")
    key = (field_number << 3) | 2
    return _encode_varint(key) + _encode_varint(len(value_bytes)) + value_bytes


def _decode_first_string_field(data: bytes, field_number: int) -> Optional[str]:
    offset = 0
    while offset < len(data):
        key, offset = _decode_varint(data, offset)
        current_field = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            _, offset = _decode_varint(data, offset)
            continue
        if wire_type != 2:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        length, offset = _decode_varint(data, offset)
        end = offset + length
        if end > len(data):
            raise ValueError("protobuf length exceeds message size")
        value_bytes = data[offset:end]
        offset = end
        if current_field == field_number:
            return value_bytes.decode("utf-8")
    return None


def build_create_team_payload(team_name: str) -> bytes:
    """Build grpc-web payload for CreateTeamRequest.name."""
    return GrpcClient.encode_payload(_encode_string_field(1, team_name))


def parse_create_team_response(
    body: bytes,
    content_type: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> str:
    """Parse CreateTeamResponse.team_id from grpc-web response body."""
    messages, trailers = GrpcClient.parse_response(
        body,
        content_type=content_type,
        headers=headers,
    )
    grpc_status = GrpcClient.get_status(trailers)
    if grpc_status.code not in (-1, 0):
        raise UpstreamException(
            message=f"ConsoleTeamReverse: gRPC failed, {grpc_status.code}",
            details={
                "status": grpc_status.http_equiv,
                "grpc_status": grpc_status.code,
                "grpc_message": grpc_status.message,
            },
        )
    if not messages:
        raise UpstreamException(
            message="ConsoleTeamReverse: empty gRPC response",
            details={"status": 502, "grpc_status": grpc_status.code},
        )
    try:
        team_id = _decode_first_string_field(messages[0], 1)
    except Exception as exc:
        raise UpstreamException(
            message=f"ConsoleTeamReverse: failed to parse team id ({exc})",
            details={"status": 502, "error": str(exc)},
        ) from exc
    if not team_id or not TEAM_ID_RE.fullmatch(team_id):
        raise UpstreamException(
            message="ConsoleTeamReverse: missing team id in response",
            details={"status": 502, "team_id": team_id or ""},
        )
    return team_id


class ConsoleTeamReverse:
    """Create a console.x.ai team using SSO cookies."""

    @staticmethod
    async def create(session: AsyncSession, token: str, team_name: str) -> str:
        user_agent = str(get_config("proxy.user_agent") or "")
        browser = resolve_proxy_browser(get_config("proxy.browser"), user_agent)
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/grpc-web+proto",
            "Origin": CONSOLE_BASE_URL,
            "Referer": f"{CONSOLE_BASE_URL}/welcome",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": user_agent,
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "Cookie": build_console_sso_cookie(token),
        }
        headers.update(_build_console_sentry_headers())
        payload = build_create_team_payload(team_name)
        timeout = float(get_config("token.console_team_init_timeout", CONSOLE_TIMEOUT) or CONSOLE_TIMEOUT)
        active_proxy_key = None

        async def _do_request() -> Any:
            nonlocal active_proxy_key
            proxy_kwargs = build_curl_cffi_proxy_kwargs(*CONSOLE_PROXY_KEYS)
            active_proxy_key = proxy_kwargs.active_proxy_key
            response = await session.post(
                CONSOLE_CREATE_TEAM_API,
                headers=headers,
                data=payload,
                timeout=timeout,
                proxy=proxy_kwargs.proxy,
                proxies=proxy_kwargs.proxies,
                impersonate=browser,
            )
            if response.status_code != 200:
                body = ""
                try:
                    value = response.text
                    if callable(value):
                        value = value()
                    if inspect.isawaitable(value):
                        value = await value
                    body = value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value)
                except Exception:
                    body = ""
                logger.warning(
                    "ConsoleTeamReverse: CreateTeam failed status={} body={}",
                    response.status_code,
                    str(body)[:500],
                )
                raise UpstreamException(
                    message=f"ConsoleTeamReverse: request failed, {response.status_code}",
                    details={"status": response.status_code, "body": str(body)[:2000]},
                )
            return response

        async def _on_retry(attempt: int, status_code: int, error: Exception, delay: float) -> None:
            if active_proxy_key and should_rotate_proxy(status_code):
                rotate_proxy(active_proxy_key)

        response = await retry_on_status(_do_request, on_retry=_on_retry)
        team_id = parse_create_team_response(
            response.content,
            content_type=response.headers.get("content-type"),
            headers=response.headers,
        )
        logger.info("ConsoleTeamReverse: CreateTeam succeeded team_id={}", team_id)
        return team_id


__all__ = [
    "CONSOLE_CREATE_TEAM_API",
    "ConsoleTeamReverse",
    "build_create_team_payload",
    "parse_create_team_response",
]
