"""CLI free Build channel: token pick, OIDC refresh, header passthrough."""

from __future__ import annotations

import time
from typing import Any, AsyncIterator, Dict, Optional, Set, Tuple, cast

import orjson

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException, ValidationException
from grok2api.core.logger import logger
from grok2api.services.grok.services.model import Channel, ModelService
from grok2api.services.grok.utils.errors import no_token_error
from grok2api.services.grok.utils.retry import pick_token_info_round_robin
from grok2api.services.reverse.build_constants import (
    FREE_USAGE_COOLDOWN_SEC,
    WEB_SEARCH_TOOL,
)
from grok2api.services.reverse.build_oauth import OAuthRefreshError
from grok2api.services.reverse.build_native import BuildNativeResponse, BuildNativeReverse
from grok2api.services.grok.services.build_auth import BuildAuthService
from grok2api.services.token import get_token_manager
from grok2api.services.token.models import TokenInfo, TokenStatus


def _is_cli_model(model_id: str) -> bool:
    model = ModelService.get(model_id)
    return bool(model and model.channel == Channel.CLI)


def _upstream_model_id(model_id: str) -> str:
    model = ModelService.get(model_id)
    if not model:
        raise ValidationException(
            message=f"Model `{model_id}` is not a CLI model",
            param="model",
            code="model_not_found",
        )
    return model.grok_model or model_id


def _wants_web_search(model_id: str) -> bool:
    model = ModelService.get(model_id)
    return bool(model and getattr(model, "cli_search", False))


def inject_web_search_tools(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Append web_search tool if missing."""
    tools = payload.get("tools")
    if tools is None:
        payload = {**payload, "tools": [dict(WEB_SEARCH_TOOL)]}
        return payload
    if not isinstance(tools, list):
        return payload
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "web_search":
            return payload
    return {**payload, "tools": [*tools, dict(WEB_SEARCH_TOOL)]}


def prepare_cli_payload(model_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite model id to upstream and optionally inject web_search."""
    out = dict(payload)
    out["model"] = _upstream_model_id(model_id)
    if _wants_web_search(model_id) or bool(get_config("build.default_web_search", False)):
        out = inject_web_search_tools(out)
    return out


def is_free_usage_exhausted(exc: UpstreamException) -> bool:
    details = exc.details or {}
    body = str(details.get("body") or "")
    status = details.get("status")
    if status != 429 and "free-usage" not in body.lower():
        # still check body for code string
        pass
    lowered = body.lower()
    return (
        "free-usage-exhausted" in lowered
        or "included free usage" in lowered
        or "subscription:free-usage-exhausted" in lowered
    )


class BuildChannelService:
    """Orchestrate CLI native passthrough with OIDC pool."""

    @staticmethod
    def _access_token(token_info: TokenInfo) -> str:
        access = (token_info.access_token or "").strip()
        if access:
            return access
        # fallback: token field may hold access jwt for simple entries
        return (token_info.token or "").strip()

    @staticmethod
    async def ensure_fresh_access(token_info: TokenInfo, pool_name: str) -> TokenInfo:
        """On-demand OIDC access refresh (same idea as console team ensure)."""
        return await BuildAuthService.refresh_token_info(token_info, pool_name, force=False)

    @staticmethod
    async def _handle_failure(
        token_mgr: Any,
        token_info: TokenInfo,
        pool_name: str,
        exc: UpstreamException,
    ) -> None:
        details = exc.details or {}
        status = details.get("status")
        token_key = token_info.token
        if is_free_usage_exhausted(exc):
            cooldown = int(
                get_config("build.free_usage_cooldown_sec", FREE_USAGE_COOLDOWN_SEC)
                or FREE_USAGE_COOLDOWN_SEC
            )
            token_info.status = TokenStatus.COOLING
            token_info.last_fail_reason = "free-usage-exhausted"
            token_info.last_fail_at = int(time.time() * 1000)
            # store cooling until as note metadata in last_sync_at for reuse
            token_info.last_sync_at = int(time.time() * 1000) + cooldown * 1000
            await token_mgr.update_token_fields(
                pool_name,
                token_key,
                {
                    "status": TokenStatus.COOLING.value,
                    "last_fail_reason": token_info.last_fail_reason,
                    "last_fail_at": token_info.last_fail_at,
                    "last_sync_at": token_info.last_sync_at,
                },
            )
            logger.warning(
                "CLI token {} free usage exhausted; cooling {}s",
                token_key[:16],
                cooldown,
            )
            return
        if status == 401:
            token_info.record_fail(status_code=401, reason="cli_auth_failed")
            await token_mgr.update_token_fields(
                pool_name,
                token_key,
                {
                    "status": token_info.status.value,
                    "fail_count": token_info.fail_count,
                    "last_fail_reason": token_info.last_fail_reason,
                    "last_fail_at": token_info.last_fail_at,
                },
            )

    @staticmethod
    async def proxy(
        *,
        model_id: str,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        body: bytes | None = None,
        stream: bool = False,
        content_type: Optional[str] = "application/json",
        conv_id: Optional[str] = None,
        rewrite_model: bool = True,
    ) -> BuildNativeResponse | AsyncIterator[bytes]:
        if not _is_cli_model(model_id):
            raise ValidationException(
                message=f"Model `{model_id}` is not a CLI model",
                param="model",
                code="model_not_found",
            )
        if not bool(get_config("build.enabled", True)):
            raise ValidationException(
                message="CLI Build channel is disabled",
                param="model",
                code="channel_disabled",
            )

        if body is None and payload is not None:
            prepared = prepare_cli_payload(model_id, payload) if rewrite_model else dict(payload)
            body = orjson.dumps(prepared)
        elif body is not None and rewrite_model and path.rstrip("/").endswith(
            ("chat/completions", "messages", "responses")
        ):
            try:
                parsed = orjson.loads(body)
                if isinstance(parsed, dict):
                    body = orjson.dumps(prepare_cli_payload(model_id, parsed))
            except orjson.JSONDecodeError:
                pass

        token_mgr = await get_token_manager()
        await token_mgr.reload_if_stale()
        tried: Set[str] = set()
        max_retries = int(get_config("retry.max_retry") or 3)
        last_error: Optional[UpstreamException] = None

        # Prefer accounts that already have OIDC; if none, kick background/on-demand mint.
        selected_probe = await pick_token_info_round_robin(token_mgr, model_id, set())
        if not selected_probe:
            try:
                await BuildAuthService.init_missing_cli_oidc(
                    trigger="request_on_demand",
                    max_tokens=1,
                    concurrency=1,
                )
            except Exception as exc:
                logger.warning("CLI on-demand mint trigger failed: {}", exc)

        for attempt in range(max_retries):
            # Round-robin only among credentials that already have auth (oidcBuild filter)
            selected = await pick_token_info_round_robin(token_mgr, model_id, tried)
            if not selected:
                break
            pool_name, token_info = selected
            tried.add(token_info.token)
            try:
                token_info = await BuildChannelService.ensure_fresh_access(
                    token_info, pool_name
                )
            except OAuthRefreshError as exc:
                last_error = UpstreamException(
                    message=f"OIDC refresh failed: {exc}",
                    details={"status": 401, "body": str(exc)},
                )
                await BuildChannelService._handle_failure(
                    token_mgr, token_info, pool_name, last_error
                )
                continue

            access = BuildChannelService._access_token(token_info)
            if not access:
                continue
            base_url = (token_info.base_url or "").strip() or None
            try:
                return await BuildNativeReverse.request(
                    access_token=access,
                    method=method,
                    path=path,
                    body=body,
                    content_type=content_type,
                    stream=stream,
                    conv_id=conv_id,
                    base_url=base_url,
                )
            except UpstreamException as exc:
                last_error = exc
                # try refresh once on 401
                status = (exc.details or {}).get("status")
                if status == 401 and (token_info.refresh_token or "").strip():
                    try:
                        # force refresh by clearing expired_at
                        token_info.expired_at = 1
                        token_info = await BuildChannelService.ensure_fresh_access(
                            token_info, pool_name
                        )
                        access = BuildChannelService._access_token(token_info)
                        return await BuildNativeReverse.request(
                            access_token=access,
                            method=method,
                            path=path,
                            body=body,
                            content_type=content_type,
                            stream=stream,
                            conv_id=conv_id,
                            base_url=base_url,
                        )
                    except Exception as refresh_exc:
                        logger.warning(
                            "CLI 401 refresh retry failed: {}", refresh_exc
                        )
                await BuildChannelService._handle_failure(
                    token_mgr, token_info, pool_name, exc
                )
                logger.warning(
                    "CLI upstream failed token={}... status={} attempt={}/{}",
                    token_info.token[:10],
                    status,
                    attempt + 1,
                    max_retries,
                )
                continue

        if last_error:
            raise last_error
        raise no_token_error(model_id)

    @staticmethod
    async def json_request(
        *,
        model_id: str,
        path: str,
        payload: Dict[str, Any],
        stream: bool = False,
    ) -> BuildNativeResponse | AsyncIterator[bytes]:
        return await BuildChannelService.proxy(
            model_id=model_id,
            method="POST",
            path=path,
            payload=payload,
            stream=stream,
        )


__all__ = [
    "BuildChannelService",
    "inject_web_search_tools",
    "is_free_usage_exhausted",
    "prepare_cli_payload",
]
