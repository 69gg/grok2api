"""
Retry helpers for token switching.
"""

from typing import Any, Optional, Set

from grok2api.core.exceptions import UpstreamException
from grok2api.services.grok.services.model import ModelService
from grok2api.services.reverse.utils.retry import is_transient_network_error


async def pick_token(
    token_mgr: Any,
    model_id: str,
    tried: Set[str],
    preferred: Optional[str] = None,
    prefer_tags: Optional[Set[str]] = None,
) -> Optional[str]:
    if preferred and preferred not in tried:
        return preferred

    token = None
    for pool_name in ModelService.pool_candidates_for_model(model_id):
        token = token_mgr.get_token_round_robin(
            pool_name,
            exclude=tried,
            prefer_tags=prefer_tags,
        )
        if token:
            break

    if not token and not tried:
        await token_mgr.refresh_cooling_tokens_on_demand()
        for pool_name in ModelService.pool_candidates_for_model(model_id):
            token = token_mgr.get_token_round_robin(
                pool_name,
                exclude=tried,
                prefer_tags=prefer_tags,
            )
            if token:
                break

    return token


async def pick_token_round_robin(
    token_mgr: Any,
    model_id: str,
    tried: Set[str],
    preferred: Optional[str] = None,
    prefer_tags: Optional[Set[str]] = None,
) -> Optional[str]:
    if preferred and preferred not in tried:
        return preferred

    token = None
    for pool_name in ModelService.pool_candidates_for_model(model_id):
        token = token_mgr.get_token_round_robin(
            pool_name,
            exclude=tried,
            prefer_tags=prefer_tags,
        )
        if token:
            break

    if not token and not tried:
        await token_mgr.refresh_cooling_tokens_on_demand()
        for pool_name in ModelService.pool_candidates_for_model(model_id):
            token = token_mgr.get_token_round_robin(
                pool_name,
                exclude=tried,
                prefer_tags=prefer_tags,
            )
            if token:
                break

    return token


async def pick_token_info_round_robin(
    token_mgr: Any,
    model_id: str,
    tried: Set[str],
    preferred: Optional[str] = None,
    prefer_tags: Optional[Set[str]] = None,
) -> Optional[tuple[str, Any]]:
    if preferred and preferred not in tried:
        pool_name = token_mgr.get_pool_name_for_token(preferred)
        if pool_name:
            pool = token_mgr.pools.get(pool_name)
            raw_preferred = (
                preferred[4:] if preferred.startswith("sso=") else preferred
            )
            token_info = pool.get(raw_preferred) if pool else None
            if token_info:
                return pool_name, token_info

    selected: Optional[tuple[str, Any]] = None
    for pool_name in ModelService.pool_candidates_for_model(model_id):
        token_info = token_mgr.get_token_info_round_robin(
            pool_name,
            exclude=tried,
            prefer_tags=prefer_tags,
        )
        if token_info:
            selected = (pool_name, token_info)
            break

    if not selected and not tried:
        await token_mgr.refresh_cooling_tokens_on_demand()
        for pool_name in ModelService.pool_candidates_for_model(model_id):
            token_info = token_mgr.get_token_info_round_robin(
                pool_name,
                exclude=tried,
                prefer_tags=prefer_tags,
            )
            if token_info:
                selected = (pool_name, token_info)
                break

    return selected


def rate_limited(error: Exception) -> bool:
    if not isinstance(error, UpstreamException):
        return False
    status = error.details.get("status") if error.details else None
    code = error.details.get("error_code") if error.details else None
    return status == 429 or code == "rate_limit_exceeded"


def transient_upstream(error: Exception) -> bool:
    """Whether error is likely transient and safe to retry with another token."""
    if is_transient_network_error(error):
        return True
    if not isinstance(error, UpstreamException):
        return False
    details = error.details or {}
    status = details.get("status")
    err = str(details.get("error") or error).lower()
    transient_status = {408, 500, 502, 503, 504}
    if status in transient_status:
        return True
    timeout_markers = (
        "timed out",
        "timeout",
        "connection reset",
        "temporarily unavailable",
        "http2",
    )
    return any(marker in err for marker in timeout_markers)


__all__ = [
    "pick_token",
    "pick_token_info_round_robin",
    "pick_token_round_robin",
    "rate_limited",
    "transient_upstream",
]
