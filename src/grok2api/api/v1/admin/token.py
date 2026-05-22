import asyncio
import re

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from grok2api.core.auth import get_app_key, verify_app_key
from grok2api.core.batch import create_task, expire_task, get_task
from grok2api.core.logger import logger
from grok2api.core.storage import get_storage
from grok2api.services.grok.batch_services.usage import UsageService
from grok2api.services.grok.batch_services.nsfw import NSFWService
from grok2api.services.register import get_auto_register_manager
from grok2api.services.register.account_settings_refresh import (
    normalize_sso_token,
    refresh_account_settings_for_tokens,
)
from grok2api.services.token.manager import get_token_manager

router = APIRouter()

_TOKEN_CHAR_REPLACEMENTS = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u2007": " ",
        "\u202f": " ",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\ufeff": "",
    }
)


def _sanitize_token_text(value) -> str:
    token = "" if value is None else str(value)
    token = token.translate(_TOKEN_CHAR_REPLACEMENTS)
    token = re.sub(r"\s+", "", token)
    if token.startswith("sso="):
        token = token[4:]
    return token.encode("ascii", errors="ignore").decode("ascii")


def _resolve_nsfw_refresh_concurrency(override=None) -> int:
    from grok2api.core.config import get_config

    source = override if override is not None else get_config(
        "token.nsfw_refresh_concurrency", 10
    )
    try:
        return max(1, int(source))
    except Exception:
        return 10


def _resolve_nsfw_refresh_retries(override=None) -> int:
    from grok2api.core.config import get_config

    source = override if override is not None else get_config(
        "token.nsfw_refresh_retries", 3
    )
    try:
        return max(0, int(source))
    except Exception:
        return 3


def _trigger_account_settings_refresh_background(
    tokens: list[str],
    concurrency: int,
    retries: int,
) -> None:
    if not tokens:
        return

    async def _run() -> None:
        try:
            result = await refresh_account_settings_for_tokens(
                tokens=tokens,
                concurrency=concurrency,
                retries=retries,
            )
            summary = result.get("summary") or {}
            logger.info(
                "Background account-settings refresh finished: total={} success={} failed={} invalidated={}",
                summary.get("total", 0),
                summary.get("success", 0),
                summary.get("failed", 0),
                summary.get("invalidated", 0),
            )
        except Exception as exc:
            logger.warning("Background account-settings refresh failed: {}", exc)

    asyncio.create_task(_run())


@router.get("/tokens/counts", dependencies=[Depends(verify_app_key)])
async def get_token_counts():
    """获取 Token 池统计计数（轻量，不含完整 token 数据）"""
    mgr = await get_token_manager()
    results = {}
    for pool_name, pool in mgr.pools.items():
        s = pool.get_stats()
        results[pool_name] = {
            "total": s.total,
            "active": s.active,
            "cooling": s.cooling,
            "expired": s.expired,
            "disabled": s.disabled,
            "quota_total": s.total_quota,
            "consumed_total": s.total_consumed,
        }
    return {"pools": results}


@router.get("/tokens", dependencies=[Depends(verify_app_key)])
async def get_tokens():
    """获取所有 Token"""
    # 获取消耗模式配置
    from grok2api.core.config import get_config
    mgr = await get_token_manager()
    results = {}
    for pool_name, pool in mgr.pools.items():
        results[pool_name] = [t.model_dump() for t in pool.list()]
    consumed_mode = get_config("token.consumed_mode_enabled", False)
    return {
        "tokens": results or {},
        "consumed_mode_enabled": consumed_mode,
    }


@router.post("/tokens", dependencies=[Depends(verify_app_key)])
async def update_tokens(data: dict):
    """更新 Token 信息"""
    storage = get_storage()
    try:
        from grok2api.services.token.models import TokenInfo

        existing_tokens: list[str] = []
        added_tokens: list[str] = []

        async with storage.acquire_lock("tokens_save", timeout=10):
            existing = await storage.load_tokens() or {}
            for pool_name, tokens in existing.items():
                if not isinstance(tokens, list):
                    continue
                for item in tokens:
                    if isinstance(item, str):
                        token_val = _sanitize_token_text(item)
                    elif isinstance(item, dict):
                        token_val = _sanitize_token_text(item.get("token"))
                    else:
                        token_val = ""
                    if token_val:
                        existing_tokens.append(token_val)

            normalized = {}
            allowed_fields = set(TokenInfo.model_fields.keys())
            existing_map = {}
            for pool_name, tokens in existing.items():
                if not isinstance(tokens, list):
                    continue
                pool_map = {}
                for item in tokens:
                    if isinstance(item, str):
                        token_data = {"token": item}
                    elif isinstance(item, dict):
                        token_data = dict(item)
                    else:
                        continue
                    raw_token = token_data.get("token")
                    if raw_token is not None:
                        token_data["token"] = _sanitize_token_text(raw_token)
                    token_key = token_data.get("token")
                    if isinstance(token_key, str):
                        pool_map[token_key] = token_data
                existing_map[pool_name] = pool_map
            for pool_name, tokens in (data or {}).items():
                if not isinstance(tokens, list):
                    continue
                pool_list = []
                for item in tokens:
                    if isinstance(item, str):
                        token_data = {"token": item}
                    elif isinstance(item, dict):
                        token_data = dict(item)
                    else:
                        continue

                    raw_token = token_data.get("token")
                    if raw_token is not None:
                        token_data["token"] = _sanitize_token_text(raw_token)
                    if not token_data.get("token"):
                        logger.warning(f"Skip empty token in pool '{pool_name}'")
                        continue

                    base = existing_map.get(pool_name, {}).get(
                        token_data.get("token"), {}
                    )
                    merged = dict(base)
                    merged.update(token_data)
                    if merged.get("tags") is None:
                        merged["tags"] = []

                    filtered = {k: v for k, v in merged.items() if k in allowed_fields}
                    try:
                        info = TokenInfo(**filtered)
                        pool_list.append(info.model_dump())
                    except Exception as e:
                        logger.warning(f"Skip invalid token in pool '{pool_name}': {e}")
                        continue
                normalized[pool_name] = pool_list

            await storage.save_tokens(normalized)
            mgr = await get_token_manager()
            await mgr.reload()

            existing_set = set(existing_tokens)
            for pool_tokens in normalized.values():
                for item in pool_tokens:
                    token_val = _sanitize_token_text(item.get("token")) if isinstance(item, dict) else ""
                    if token_val and token_val not in existing_set:
                        added_tokens.append(token_val)

        concurrency = _resolve_nsfw_refresh_concurrency()
        retries = _resolve_nsfw_refresh_retries()
        _trigger_account_settings_refresh_background(
            tokens=list(dict.fromkeys(added_tokens)),
            concurrency=concurrency,
            retries=retries,
        )

        return {"status": "success", "message": "Token 已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/refresh", dependencies=[Depends(verify_app_key)])
async def refresh_tokens(data: dict):
    """刷新 Token 状态"""
    try:
        mgr = await get_token_manager()
        tokens = []
        if isinstance(data.get("token"), str) and data["token"].strip():
            tokens.append(data["token"].strip())
        if isinstance(data.get("tokens"), list):
            tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens provided")

        unique_tokens = list(dict.fromkeys(tokens))

        raw_results = await UsageService.batch(
            unique_tokens,
            mgr,
        )

        # 强制保存变更到存储
        await mgr._save(force=True)

        results = {}
        for token, res in raw_results.items():
            results[token] = bool(res.get("ok")) and res.get("data") is True

        response = {"status": "success", "results": results}
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/refresh/async", dependencies=[Depends(verify_app_key)])
async def refresh_tokens_async(data: dict):
    """刷新 Token 状态（异步批量 + SSE 进度）"""
    mgr = await get_token_manager()
    tokens = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    unique_tokens = list(dict.fromkeys(tokens))

    task = create_task(len(unique_tokens))

    async def _run():
        try:

            async def _on_item(item: str, res: dict):
                task.record(bool(res.get("ok")) and res.get("data") is True)

            raw_results = await UsageService.batch(
                unique_tokens,
                mgr,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results: dict[str, bool] = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                if res.get("ok") and res.get("data") is True:
                    ok_count += 1
                    results[token] = True
                else:
                    fail_count += 1
                    results[token] = False

            await mgr._save(force=True)

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            task.finish(result)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            import asyncio
            asyncio.create_task(expire_task(task.id, 300))

    import asyncio
    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }


@router.get("/batch/{task_id}/stream")
async def batch_stream(task_id: str, request: Request):
    app_key = get_app_key()
    if app_key:
        key = request.query_params.get("app_key")
        if key != app_key:
            raise HTTPException(status_code=401, detail="Invalid authentication token")
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_stream():
        queue = task.attach()
        try:
            yield f"data: {orjson.dumps({'type': 'snapshot', **task.snapshot()}).decode()}\n\n"

            final = task.final_event()
            if final:
                yield f"data: {orjson.dumps(final).decode()}\n\n"
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    final = task.final_event()
                    if final:
                        yield f"data: {orjson.dumps(final).decode()}\n\n"
                        return
                    continue

                yield f"data: {orjson.dumps(event).decode()}\n\n"
                if event.get("type") in ("done", "error", "cancelled"):
                    return
        finally:
            task.detach(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/batch/{task_id}/cancel", dependencies=[Depends(verify_app_key)])
async def batch_cancel(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.cancel()
    return {"status": "success"}


@router.post("/tokens/nsfw/enable", dependencies=[Depends(verify_app_key)])
async def enable_nsfw(data: dict):
    """批量开启 NSFW (Unhinged) 模式"""
    try:
        mgr = await get_token_manager()

        tokens = []
        if isinstance(data.get("token"), str) and data["token"].strip():
            tokens.append(data["token"].strip())
        if isinstance(data.get("tokens"), list):
            tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

        if not tokens:
            for pool_name, pool in mgr.pools.items():
                for info in pool.list():
                    raw = (
                        info.token[4:] if info.token.startswith("sso=") else info.token
                    )
                    tokens.append(raw)

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens available")

        unique_tokens = list(dict.fromkeys(tokens))

        raw_results = await NSFWService.batch(
            unique_tokens,
            mgr,
        )

        results = {}
        ok_count = 0
        fail_count = 0

        for token, res in raw_results.items():
            masked = f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token
            if res.get("ok") and res.get("data", {}).get("success"):
                ok_count += 1
                results[masked] = res.get("data", {})
            else:
                fail_count += 1
                results[masked] = res.get("data") or {"error": res.get("error")}

        response = {
            "status": "success",
            "summary": {
                "total": len(unique_tokens),
                "ok": ok_count,
                "fail": fail_count,
            },
            "results": results,
        }

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Enable NSFW failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/nsfw/enable/async", dependencies=[Depends(verify_app_key)])
async def enable_nsfw_async(data: dict):
    """批量开启 NSFW (Unhinged) 模式（异步批量 + SSE 进度）"""
    mgr = await get_token_manager()

    tokens = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

    if not tokens:
        for pool_name, pool in mgr.pools.items():
            for info in pool.list():
                raw = info.token[4:] if info.token.startswith("sso=") else info.token
                tokens.append(raw)

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens available")

    unique_tokens = list(dict.fromkeys(tokens))

    task = create_task(len(unique_tokens))

    async def _run():
        try:

            async def _on_item(item: str, res: dict):
                ok = bool(res.get("ok") and res.get("data", {}).get("success"))
                task.record(ok)

            raw_results = await NSFWService.batch(
                unique_tokens,
                mgr,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                masked = f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token
                if res.get("ok") and res.get("data", {}).get("success"):
                    ok_count += 1
                    results[masked] = res.get("data", {})
                else:
                    fail_count += 1
                    results[masked] = res.get("data") or {"error": res.get("error")}

            await mgr._save(force=True)

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            task.finish(result)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            import asyncio
            asyncio.create_task(expire_task(task.id, 300))

    import asyncio
    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }


@router.post("/tokens/nsfw/refresh", dependencies=[Depends(verify_app_key)])
async def refresh_tokens_nsfw_api(data: dict):
    """Refresh account settings (TOS + birth date + NSFW) for selected/all tokens."""
    payload = data if isinstance(data, dict) else {}
    mgr = await get_token_manager()

    tokens: list[str] = []
    seen: set[str] = set()

    if bool(payload.get("all")):
        for pool in mgr.pools.values():
            for info in pool.list():
                token = normalize_sso_token(str(info.token or "").strip())
                if not token or token in seen:
                    continue
                seen.add(token)
                tokens.append(token)
    else:
        candidates: list[str] = []
        single = payload.get("token")
        if isinstance(single, str):
            candidates.append(single)
        batch = payload.get("tokens")
        if isinstance(batch, list):
            candidates.extend([item for item in batch if isinstance(item, str)])

        for raw in candidates:
            token = normalize_sso_token(str(raw or "").strip())
            if not token or token in seen:
                continue
            seen.add(token)
            tokens.append(token)

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    concurrency = _resolve_nsfw_refresh_concurrency(payload.get("concurrency"))
    retries = _resolve_nsfw_refresh_retries(payload.get("retries"))
    result = await refresh_account_settings_for_tokens(
        tokens=tokens,
        concurrency=concurrency,
        retries=retries,
    )
    return {
        "status": "success",
        "summary": result.get("summary") or {},
        "failed": result.get("failed") or [],
    }


@router.post("/tokens/auto-register", dependencies=[Depends(verify_app_key)])
async def auto_register_tokens_api(data: dict):
    """Start auto registration."""
    try:
        from grok2api.core.config import get_config

        data = data or {}
        count = data.get("count")
        concurrency = data.get("concurrency")
        pool = (data.get("pool") or "ssoBasic").strip() or "ssoBasic"

        try:
            count_val = int(count)
        except Exception:
            count_val = int(get_config("register.default_count", 100) or 100)

        if count_val <= 0:
            count_val = int(get_config("register.default_count", 100) or 100)

        try:
            concurrency_val = int(concurrency)
        except Exception:
            concurrency_val = None
        if concurrency_val is not None and concurrency_val <= 0:
            concurrency_val = None

        manager = get_auto_register_manager()
        job = await manager.start_job(
            count=count_val,
            pool=pool,
            concurrency=concurrency_val,
        )
        return {"status": "started", "job": job.to_dict()}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tokens/auto-register/status", dependencies=[Depends(verify_app_key)])
async def auto_register_status_api(job_id: str | None = None):
    """Get auto registration status."""
    manager = get_auto_register_manager()
    status = manager.get_status(job_id)
    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Job not found")
    return status


@router.post("/tokens/auto-register/stop", dependencies=[Depends(verify_app_key)])
async def auto_register_stop_api(job_id: str | None = None):
    """Stop auto registration (best-effort)."""
    manager = get_auto_register_manager()
    status = manager.get_status(job_id)
    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Job not found")
    await manager.stop_job()
    return {"status": "stopping"}
