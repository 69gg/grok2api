"""Background + on-demand OIDC acquisition for free CLI channel.

Mirrors Console team id auto-init:
- scan existing accounts (ssoBasic) that lack OIDC
- mint access/refresh into oidcBuild (silent background)
- requests only round-robin accounts that already have OIDC
- access tokens are refreshed on demand / scheduled
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from grok2api.core.config import get_config
from grok2api.core.logger import logger
from grok2api.services.reverse.build_constants import (
    CLI_POOL_NAME,
    DEFAULT_BASE_URL,
    REFRESH_LEAD_SEC,
    TOKEN_ENDPOINT,
)
from grok2api.services.reverse.build_native import BUILD_PROXY_KEYS
from grok2api.services.reverse.build_oauth import (
    OAuthDeviceError,
    OAuthRefreshError,
    TokenBundle,
    poll_device_token,
    refresh_tokens_async,
    request_device_code,
    sub_from_access_token,
)
from grok2api.services.reverse.utils.proxy import get_current_proxy_url_from
from grok2api.services.token import get_token_manager
from grok2api.services.token.models import TokenInfo, TokenStatus

SSO_SOURCE_POOL = "ssoBasic"


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except Exception:
        return default


def token_has_cli_auth(token_info: TokenInfo) -> bool:
    """Whether this credential can serve CLI (has refresh or non-empty access)."""
    if token_info.status not in {TokenStatus.ACTIVE, TokenStatus.COOLING}:
        # cooling still "has auth" but not selectable for traffic
        pass
    if (token_info.refresh_token or "").strip():
        return True
    if (token_info.access_token or "").strip():
        return True
    return False


def token_cli_selectable(token_info: TokenInfo) -> bool:
    """Ready for request routing: active + has OIDC + not in free-usage cool window."""
    if token_info.status != TokenStatus.ACTIVE:
        return False
    if not token_has_cli_auth(token_info):
        return False
    # free-usage cool: last_sync_at stores resume_at_ms when last_fail_reason is free-usage
    if (token_info.last_fail_reason or "") == "free-usage-exhausted":
        resume_at = int(token_info.last_sync_at or 0)
        if resume_at and int(time.time() * 1000) < resume_at:
            return False
    return True


def is_oidc_refresh_revoked_error(exc: BaseException) -> bool:
    """Whether refresh failed permanently (invalid_grant / revoked refresh token)."""
    msg = str(exc).lower()
    if "invalid_grant" in msg:
        return True
    if "revoked" in msg and "refresh" in msg:
        return True
    if "refresh token has been revoked" in msg:
        return True
    return False


class BuildAuthService:
    """Mint / refresh OIDC for CLI channel."""

    _mint_locks: dict[str, asyncio.Lock] = {}
    _mint_locks_guard = asyncio.Lock()

    @classmethod
    async def _mint_lock(cls, key: str) -> asyncio.Lock:
        async with cls._mint_locks_guard:
            lock = cls._mint_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                cls._mint_locks[key] = lock
            return lock

    @staticmethod
    def _proxy_url() -> Optional[str]:
        _, proxy = get_current_proxy_url_from(*BUILD_PROXY_KEYS)
        return proxy or None

    @staticmethod
    async def persist_bundle(
        bundle: TokenBundle,
        *,
        email: str = "",
        sso_source: str = "",
        pool_name: str = CLI_POOL_NAME,
    ) -> bool:
        mgr = await get_token_manager()
        account_id = (
            (bundle.sub or "").strip()
            or (email or "").strip()
            or sub_from_access_token(bundle.access_token)
            or bundle.access_token[:32]
        )
        return await mgr.add_cli_oidc(
            account_id=account_id,
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
            expired_at=bundle.expired_at_ms,
            base_url=str(get_config("build.base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL),
            email=email,
            oidc_sub=bundle.sub,
            pool_name=pool_name,
            sso_source=sso_source,
        )

    @staticmethod
    async def _invalidate_dead_oidc(
        token_info: TokenInfo,
        pool_name: str,
        *,
        reason: str,
    ) -> None:
        """Drop revoked OIDC secrets so schedulers stop thrashing refresh."""
        now_ms = int(time.time() * 1000)
        token_info.access_token = ""
        token_info.refresh_token = ""
        token_info.last_fail_reason = reason
        token_info.last_fail_at = now_ms
        mgr = await get_token_manager()
        await mgr.update_token_fields(
            pool_name,
            token_info.token,
            {
                "access_token": "",
                "refresh_token": "",
                "last_fail_reason": reason,
                "last_fail_at": now_ms,
            },
        )

    @classmethod
    async def remint_oidc_for_token_info(
        cls,
        token_info: TokenInfo,
        pool_name: str = CLI_POOL_NAME,
        *,
        trigger: str = "refresh_revoked",
    ) -> Optional[TokenInfo]:
        """Re-run device mint using linked sso_source after refresh_token is dead."""
        if not _as_bool(get_config("build.mint_enabled", True), True):
            return None
        if not _as_bool(get_config("build.remint_on_revoked", True), True):
            return None

        sso = (token_info.sso_source or "").strip()
        if not sso:
            logger.warning(
                "CLI OIDC remint skipped {}: no sso_source (cannot re-device-auth)",
                token_info.token[:16],
            )
            await cls._invalidate_dead_oidc(
                token_info, pool_name, reason="oidc_refresh_revoked_no_sso"
            )
            return None

        email = (token_info.email or "").strip()
        dead_refresh = (token_info.refresh_token or "").strip()
        lock = await cls._mint_lock(sso)
        async with lock:
            mgr = await get_token_manager()
            pool = mgr.pools.get(pool_name)
            current = pool.get(token_info.token) if pool else None
            if current is None and pool:
                for info in pool.list():
                    if (info.sso_source or "") == sso:
                        current = info
                        break
            if current is not None:
                cur_refresh = (current.refresh_token or "").strip()
                # Another task already replaced the revoked refresh token.
                if (
                    cur_refresh
                    and dead_refresh
                    and cur_refresh != dead_refresh
                    and token_has_cli_auth(current)
                ):
                    logger.info(
                        "CLI OIDC remint skipped {}: already reminted by peer",
                        token_info.token[:16],
                    )
                    return current

            try:
                logger.info(
                    "CLI OIDC remint start trigger={} token={} sso={}...",
                    trigger,
                    token_info.token[:16],
                    sso[:12],
                )
                bundle = await cls.mint_from_sso_protocol(sso, email=email)
                await cls.persist_bundle(
                    bundle,
                    email=email,
                    sso_source=sso,
                    pool_name=pool_name,
                )
                mgr = await get_token_manager()
                pool = mgr.pools.get(pool_name)
                if not pool:
                    return None
                for info in pool.list():
                    if (info.sso_source or "") == sso or (
                        bundle.sub and info.token == bundle.sub
                    ):
                        logger.info(
                            "CLI OIDC remint ok trigger={} token={}",
                            trigger,
                            info.token[:16],
                        )
                        return info
                return None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "CLI OIDC remint failed trigger={} token={} sso={}... err={}",
                    trigger,
                    token_info.token[:16],
                    sso[:12],
                    exc,
                )
                await cls._invalidate_dead_oidc(
                    token_info, pool_name, reason="oidc_refresh_revoked"
                )
                return None

    @staticmethod
    async def refresh_token_info(
        token_info: TokenInfo,
        pool_name: str = CLI_POOL_NAME,
        *,
        force: bool = False,
    ) -> TokenInfo:
        lead = _as_int(get_config("build.refresh_lead_sec", REFRESH_LEAD_SEC), REFRESH_LEAD_SEC)
        now_ms = int(time.time() * 1000)
        expired_at = int(token_info.expired_at or 0)
        if not force and expired_at and now_ms < expired_at - lead * 1000:
            return token_info
        refresh = (token_info.refresh_token or "").strip()
        if not refresh:
            # Dead credential with linked SSO: force path remints after cooldown.
            if force and (token_info.sso_source or "").strip():
                if BuildAuthService._remint_in_cooldown(token_info):
                    return token_info
                reminted = await BuildAuthService.remint_oidc_for_token_info(
                    token_info, pool_name, trigger="empty_refresh"
                )
                if reminted is not None:
                    return reminted
            return token_info
        endpoint = str(get_config("build.token_endpoint", TOKEN_ENDPOINT) or TOKEN_ENDPOINT)
        try:
            bundle = await refresh_tokens_async(
                refresh,
                token_endpoint=endpoint,
                proxy=BuildAuthService._proxy_url(),
            )
        except OAuthRefreshError as exc:
            logger.warning("CLI OIDC refresh failed {}: {}", token_info.token[:16], exc)
            if is_oidc_refresh_revoked_error(exc):
                if not BuildAuthService._remint_in_cooldown(token_info):
                    reminted = await BuildAuthService.remint_oidc_for_token_info(
                        token_info, pool_name, trigger="refresh_revoked"
                    )
                    if reminted is not None:
                        return reminted
            raise
        token_info.access_token = bundle.access_token
        if bundle.refresh_token:
            token_info.refresh_token = bundle.refresh_token
        token_info.expired_at = bundle.expired_at_ms
        token_info.last_refresh_at = now_ms
        if bundle.sub:
            token_info.oidc_sub = bundle.sub
        mgr = await get_token_manager()
        await mgr.update_token_fields(
            pool_name,
            token_info.token,
            {
                "access_token": token_info.access_token,
                "refresh_token": token_info.refresh_token,
                "expired_at": token_info.expired_at,
                "last_refresh_at": token_info.last_refresh_at,
                "oidc_sub": token_info.oidc_sub,
                "last_fail_reason": None,
            },
        )
        return token_info

    @staticmethod
    def _remint_in_cooldown(token_info: TokenInfo) -> bool:
        """Avoid hammering device-auth when SSO mint keeps failing."""
        reason = (token_info.last_fail_reason or "").strip()
        if not reason.startswith("oidc_refresh_revoked"):
            return False
        cooldown = _as_int(get_config("build.remint_cooldown_sec", 300), 300, minimum=0)
        if cooldown <= 0:
            return False
        last_fail = int(token_info.last_fail_at or 0)
        if not last_fail:
            return False
        return int(time.time() * 1000) - last_fail < cooldown * 1000

    @staticmethod
    async def refresh_expiring_cli_tokens(
        *,
        max_tokens: int = 50,
        pool_name: str = CLI_POOL_NAME,
    ) -> int:
        """Scheduled pass: refresh OIDC access tokens nearing expiry.

        Permanently revoked refresh tokens are reminted via linked sso_source.
        """
        mgr = await get_token_manager()
        await mgr.reload_if_stale()
        pool = mgr.pools.get(pool_name)
        if not pool:
            return 0
        lead = _as_int(get_config("build.refresh_lead_sec", REFRESH_LEAD_SEC), REFRESH_LEAD_SEC)
        now_ms = int(time.time() * 1000)
        refreshed = 0
        for info in list(pool.list()):
            if refreshed >= max_tokens:
                break
            has_refresh = bool((info.refresh_token or "").strip())
            needs_remint_only = (
                not has_refresh
                and bool((info.sso_source or "").strip())
                and (info.last_fail_reason or "").startswith("oidc_refresh_revoked")
            )
            if not has_refresh and not needs_remint_only:
                continue
            expired_at = int(info.expired_at or 0)
            if has_refresh and expired_at and now_ms < expired_at - lead * 1000:
                continue
            try:
                updated = await BuildAuthService.refresh_token_info(
                    info, pool_name, force=True
                )
                if token_has_cli_auth(updated):
                    refreshed += 1
            except Exception as exc:
                # refresh_token_info already attempted remint on revoked; still log.
                logger.warning("CLI scheduled refresh failed {}: {}", info.token[:16], exc)
        if refreshed:
            logger.info("CLI OIDC scheduled refresh: {} tokens", refreshed)
        return refreshed

    @staticmethod
    async def mint_from_sso_protocol(
        sso_token: str,
        *,
        email: str = "",
        log: Optional[Callable[[str], None]] = None,
    ) -> TokenBundle:
        """Device-auth mint using SSO cookie over pure HTTP (no browser).

        Flow (captured): device/code → device/verify → device/approve(action=allow)
        → token poll. Browser is only a last-resort fallback if enabled.

        Permanent consent errors (invalid/expired user_code) trigger a fresh
        device_code request rather than replaying the same code.
        """
        log = log or (lambda m: logger.info("CLI mint: {}", m))
        proxy = BuildAuthService._proxy_url()
        prefer_protocol = _as_bool(get_config("build.mint_prefer_protocol", True), True)
        allow_browser = _as_bool(get_config("build.mint_allow_browser", False), False)
        max_rounds = _as_int(get_config("build.mint_protocol_rounds", 2), 2, minimum=1)

        from grok2api.services.reverse.build_device_consent import (
            approve_device_with_sso_protocol,
            is_permanent_device_consent_error,
        )

        protocol_err: Exception | None = None
        session = None

        for round_idx in range(max_rounds):
            session = await asyncio.to_thread(request_device_code, proxy=proxy)
            log(
                f"device_code ok user_code={session.user_code} "
                f"uri={session.verification_uri_complete} "
                f"(round {round_idx + 1}/{max_rounds})"
            )

            if not prefer_protocol:
                break

            try:
                await asyncio.to_thread(
                    approve_device_with_sso_protocol,
                    user_code=session.user_code,
                    sso_token=sso_token,
                    proxy=proxy,
                    log=log,
                    retries=3,
                )
                protocol_err = None
                break
            except Exception as exc:  # noqa: BLE001
                protocol_err = exc
                log(f"protocol consent failed: {exc}")
                # Permanent errors need a brand-new device_code; transient ones
                # already exhausted inner retries.
                if (
                    is_permanent_device_consent_error(exc)
                    and round_idx + 1 < max_rounds
                ):
                    log("requesting fresh device_code after permanent consent error")
                    continue
                break

        if session is None:
            raise OAuthDeviceError("device code request produced no session")

        consent_ok = protocol_err is None and prefer_protocol
        if not prefer_protocol:
            consent_ok = False

        if not consent_ok and allow_browser:
            log("falling back to browser consent")
            from grok2api.services.grok.services.build_mint_browser import (
                approve_device_with_sso,
            )

            headless = _as_bool(get_config("build.mint_headless", False), False)
            await asyncio.to_thread(
                approve_device_with_sso,
                verification_uri_complete=session.verification_uri_complete,
                sso_token=sso_token,
                email=email,
                proxy=proxy,
                headless=headless,
                log=log,
            )
            consent_ok = True
            protocol_err = None

        if not consent_ok:
            raise OAuthDeviceError(
                f"device consent failed (protocol"
                f"{'' if protocol_err is None else f': {protocol_err}'}"
                f"; browser disabled)"
            )

        bundle = await asyncio.to_thread(
            poll_device_token,
            session.device_code,
            interval=session.interval,
            expires_in=min(int(session.expires_in), 120),
            proxy=proxy,
            log=log,
        )
        return bundle

    @classmethod
    async def ensure_oidc_for_sso(
        cls,
        sso_token: str,
        *,
        email: str = "",
        trigger: str = "request",
    ) -> Optional[TokenInfo]:
        """Ensure oidcBuild has a credential derived from this SSO (mint if missing)."""
        sso_token = sso_token[4:] if sso_token.startswith("sso=") else sso_token
        if not sso_token:
            return None
        mgr = await get_token_manager()
        pool = mgr.pools.get(CLI_POOL_NAME)
        if pool:
            for info in pool.list():
                if (info.sso_source or "") == sso_token and token_has_cli_auth(info):
                    return info

        if not _as_bool(get_config("build.mint_enabled", True), True):
            return None

        lock = await cls._mint_lock(sso_token)
        async with lock:
            # re-check after lock
            mgr = await get_token_manager()
            pool = mgr.pools.get(CLI_POOL_NAME)
            if pool:
                for info in pool.list():
                    if (info.sso_source or "") == sso_token and token_has_cli_auth(info):
                        return info
            try:
                logger.info(
                    "CLI OIDC mint start trigger={} sso={}...",
                    trigger,
                    sso_token[:12],
                )
                bundle = await cls.mint_from_sso_protocol(sso_token, email=email)
                await cls.persist_bundle(
                    bundle, email=email, sso_source=sso_token, pool_name=CLI_POOL_NAME
                )
                # reload info
                mgr = await get_token_manager()
                pool = mgr.pools.get(CLI_POOL_NAME)
                if not pool:
                    return None
                for info in pool.list():
                    if (info.sso_source or "") == sso_token or (
                        bundle.sub and info.token == bundle.sub
                    ):
                        return info
            except Exception as exc:
                logger.warning(
                    "CLI OIDC mint failed trigger={} sso={}... err={}",
                    trigger,
                    sso_token[:12],
                    exc,
                )
                return None
        return None

    @classmethod
    async def init_missing_cli_oidc(
        cls,
        *,
        trigger: str = "scheduler",
        max_tokens: int = 20,
        concurrency: int = 2,
        source_pool: str = SSO_SOURCE_POOL,
    ) -> int:
        """Background: for existing SSO accounts without OIDC, mint silently."""
        if not _as_bool(get_config("build.enabled", True), True):
            return 0
        if not _as_bool(get_config("build.mint_enabled", True), True):
            return 0
        if not _as_bool(get_config("build.auto_init_from_sso_enabled", True), True):
            return 0

        mgr = await get_token_manager()
        await mgr.reload_if_stale()
        source = mgr.pools.get(source_pool)
        if not source:
            return 0
        existing_sso: set[str] = set()
        oidc_pool = mgr.pools.get(CLI_POOL_NAME)
        if oidc_pool:
            for info in oidc_pool.list():
                # Only treat still-usable OIDC rows as covering this SSO.
                # Dead rows (revoked refresh cleared) should be reminted.
                if info.sso_source and token_has_cli_auth(info):
                    existing_sso.add(info.sso_source)

        candidates: list[TokenInfo] = []
        for info in source.list():
            if info.status != TokenStatus.ACTIVE:
                continue
            if info.token in existing_sso:
                continue
            # Already has OIDC fields on the SSO record itself (optional dual-write)
            if token_has_cli_auth(info) and (info.refresh_token or "").strip():
                # promote into oidcBuild if missing
                if info.token not in existing_sso:
                    await mgr.add_cli_oidc(
                        account_id=info.oidc_sub or info.email or info.token[:32],
                        access_token=info.access_token,
                        refresh_token=info.refresh_token,
                        expired_at=info.expired_at,
                        base_url=info.base_url or DEFAULT_BASE_URL,
                        email=info.email,
                        oidc_sub=info.oidc_sub,
                        sso_source=info.token,
                    )
                continue
            candidates.append(info)
            if len(candidates) >= max_tokens:
                break

        if not candidates:
            return 0

        logger.info(
            "CLI OIDC auto-init: {} candidates trigger={} concurrency={}",
            len(candidates),
            trigger,
            concurrency,
        )
        sem = asyncio.Semaphore(max(1, concurrency))
        done = 0

        async def _one(info: TokenInfo) -> None:
            nonlocal done
            async with sem:
                result = await cls.ensure_oidc_for_sso(
                    info.token, email=info.email or "", trigger=trigger
                )
                if result is not None:
                    done += 1

        await asyncio.gather(*[_one(c) for c in candidates])
        logger.info("CLI OIDC auto-init done: {}/{} trigger={}", done, len(candidates), trigger)
        return done


__all__ = [
    "BuildAuthService",
    "SSO_SOURCE_POOL",
    "is_oidc_refresh_revoked_error",
    "token_cli_selectable",
    "token_has_cli_auth",
]
