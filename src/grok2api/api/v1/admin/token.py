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


def _item_token_key(item) -> str:
    """提取条目归一化后的 token 键（str 条目或 dict 条目的 token 字段）。"""
    if isinstance(item, str):
        return _sanitize_token_text(item)
    if isinstance(item, dict):
        return _sanitize_token_text(item.get("token"))
    return ""


def _build_existing_index(existing: dict) -> tuple[set[str], dict]:
    """扫描存量数据，构建 (跨池 token 集合, {pool_name: {token: 条目字段 dict}})。

    existing_map 中条目的 token 字段已归一化，用作提交数据的合并 base。
    只扫描一遍，不做 TokenInfo 校验（存量条目原样索引，不丢数据）。
    同一池内 token 重复时以第一份为合并 base，与合并更新时保留第一处
    出现位置的策略一致。
    """
    existing_set: set[str] = set()
    existing_map: dict = {}
    for pool_name, tokens in (existing or {}).items():
        if not isinstance(tokens, list):
            continue
        pool_map: dict = {}
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
            if isinstance(token_key, str) and token_key:
                existing_set.add(token_key)
                pool_map.setdefault(token_key, token_data)
        existing_map[pool_name] = pool_map
    return existing_set, existing_map


def _try_build_token_info(filtered: dict, token_data: dict, pool_name: str):
    """用合并字段构建 TokenInfo；存量 base 字段损坏时剥离后重试一次。

    提交方自己提供的字段校验失败时不补救（真 invalid，返回 None）；
    仅 base 带入的损坏字段会被剥离并由模型默认值兜底，使提交可以修复
    损坏的存量 token，而不是误报 invalid 后原样保留坏数据。
    """
    from pydantic import ValidationError

    from grok2api.services.token.models import TokenInfo

    try:
        return TokenInfo(**filtered)
    except ValidationError as e:
        bad_fields = {str(err["loc"][0]) for err in e.errors() if err.get("loc")}
        if not bad_fields or bad_fields & set(token_data):
            logger.warning(f"Skip invalid token in pool '{pool_name}': {e}")
            return None
        logger.warning(
            f"Drop invalid existing fields {sorted(bad_fields)} for token "
            f"'{filtered.get('token')}' in pool '{pool_name}'"
        )
        stripped = {k: v for k, v in filtered.items() if k not in bad_fields}
        try:
            return TokenInfo(**stripped)
        except Exception as e2:
            logger.warning(f"Skip invalid token in pool '{pool_name}': {e2}")
            return None
    except Exception as e:
        logger.warning(f"Skip invalid token in pool '{pool_name}': {e}")
        return None


def _normalize_items(
    existing_set: set[str],
    existing_map: dict,
    data: dict,
) -> tuple[dict, list[str], int, int]:
    """归一化提交的 token 数据，并以 existing_map 为 base 按 token 合并。

    base 查找先按目标池精确匹配；目标池不存在该 token 时回退到其他池的
    存量条目（取第一处出现者），使跨池移动的 token 保留原有字段。

    Args:
        existing_set: 存量 token 集合（跨池去重）
        existing_map: _build_existing_index 构建的 {pool_name: {token: 字段 dict}}
        data: 待归一化的提交数据（{pool_name: [item, ...]}）

    Returns:
        (normalized, added_tokens, updated_count, invalid_count)
        - normalized: {pool_name: [TokenInfo.model_dump(), ...]}，可直接 save_tokens；
          同一 pool 内相同 token 只保留一份（后出现者覆盖先出现者的字段）
        - added_tokens: 归一化结果中不属于存量的 token（可能跨池重复，调用方自行去重）
        - updated_count: data 中 token 已存在于存量的有效条目数（按 token 跨池去重）
        - invalid_count: data 中被丢弃的无效条目数
    """
    from grok2api.services.token.models import TokenInfo

    allowed_fields = set(TokenInfo.model_fields.keys())
    # 跨池 base 索引：token -> 第一处出现的存量条目（目标池查找失败时回退）
    fallback_map: dict = {}
    for _pool_map in existing_map.values():
        for _token_key, _token_fields in _pool_map.items():
            fallback_map.setdefault(_token_key, _token_fields)
    normalized: dict = {}
    added_tokens: list[str] = []
    updated_seen: set[str] = set()
    updated_count = 0
    invalid_count = 0
    for pool_name, tokens in (data or {}).items():
        if not isinstance(tokens, list):
            continue
        pool_map: dict = {}
        for item in tokens:
            if isinstance(item, str):
                token_data = {"token": item}
            elif isinstance(item, dict):
                token_data = dict(item)
            else:
                invalid_count += 1
                continue

            raw_token = token_data.get("token")
            if raw_token is not None:
                token_data["token"] = _sanitize_token_text(raw_token)
            if not token_data.get("token"):
                logger.warning(f"Skip empty token in pool '{pool_name}'")
                invalid_count += 1
                continue

            base = existing_map.get(pool_name, {}).get(token_data.get("token"))
            if base is None:
                base = fallback_map.get(token_data.get("token"), {})
            merged = dict(base)
            merged.update(token_data)
            if merged.get("tags") is None:
                merged["tags"] = []

            filtered = {k: v for k, v in merged.items() if k in allowed_fields}
            info = _try_build_token_info(filtered, token_data, pool_name)
            if info is None:
                invalid_count += 1
                continue

            if info.token not in pool_map:
                if info.token in existing_set:
                    if info.token not in updated_seen:
                        updated_seen.add(info.token)
                        updated_count += 1
                else:
                    added_tokens.append(info.token)
            pool_map[info.token] = info.model_dump()
        normalized[pool_name] = list(pool_map.values())

    return normalized, added_tokens, updated_count, invalid_count


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
    """更新 Token 信息（全量替换：仅保存本次提交的 token）"""
    storage = get_storage()
    try:
        async with storage.acquire_lock("tokens_save", timeout=10):
            existing = await storage.load_tokens() or {}
            existing_set, existing_map = _build_existing_index(existing)
            normalized, added_tokens, _, _ = _normalize_items(
                existing_set, existing_map, data
            )

            await storage.save_tokens(normalized)
            mgr = await get_token_manager()
            await mgr.reload()

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


@router.post("/tokens/add", dependencies=[Depends(verify_app_key)])
async def add_tokens(data: dict):
    """增量添加 Token。

    保留全部现有 token（存量条目原样保留，不做二次校验，避免静默丢数据）；
    提交的 token 不存在时新增，已存在时合并更新其字段（同池重复条目收敛为一份）；
    已有 token 被提交到其他池时视为跨池移动：从原池移除（含重复条目），
    原有字段保留，计为 updated 且不触发后台刷新。
    存量条目中损坏的字段在校验失败时会被剥离并以模型默认值兜底，
    使提交可以修复损坏 token；提交方自己提供的字段校验失败仍计 invalid。
    仅对新添加的 token 触发后台账户设置刷新。
    """
    storage = get_storage()
    try:
        async with storage.acquire_lock("tokens_save", timeout=10):
            existing = await storage.load_tokens() or {}
            existing_set, existing_map = _build_existing_index(existing)
            submitted, added_tokens, updated_count, invalid_count = _normalize_items(
                existing_set, existing_map, data
            )

            added_tokens = list(dict.fromkeys(added_tokens))
            if not added_tokens and updated_count == 0:
                raise HTTPException(status_code=400, detail="No valid tokens provided")

            # 提交 token -> 目标池集合，用于识别跨池移动
            submitted_pools: dict[str, set[str]] = {}
            for pool_name, items in submitted.items():
                for item in items:
                    submitted_pools.setdefault(item["token"], set()).add(pool_name)

            # 以存量为基底原地合并：存量条目保持位置与内容不变；
            # 本次提交的条目按 token 覆盖对应位置，新 token 追加到池尾；
            # 跨池移动的 token 从原池移除（含原池中的重复条目）；
            # 同池内被提交 token 的重复条目只保留第一份（随后被合并条目覆盖）
            normalized: dict = {}
            for pool_name, tokens in existing.items():
                if not isinstance(tokens, list):
                    # 非 list 的损坏存量：本次未提交该池时原样透传，避免静默丢数据；
                    # 提交了该池时由提交数据整体替换（下方 submitted 循环兜底）
                    if pool_name not in submitted:
                        normalized[pool_name] = tokens
                    continue
                submitted_keys = {
                    item["token"] for item in submitted.get(pool_name, [])
                }
                seen_submitted: set[str] = set()
                pool_list = []
                for item in tokens:
                    key = _item_token_key(item)
                    target_pools = submitted_pools.get(key) if key else None
                    if target_pools and pool_name not in target_pools:
                        continue
                    if key and key in submitted_keys:
                        if key in seen_submitted:
                            continue
                        seen_submitted.add(key)
                    pool_list.append(item)
                positions: dict[str, int] = {}
                for idx, item in enumerate(pool_list):
                    key = _item_token_key(item)
                    if key and key not in positions:
                        positions[key] = idx
                for item in submitted.get(pool_name, []):
                    token_key = item["token"]
                    if token_key in positions:
                        pool_list[positions[token_key]] = item
                    else:
                        positions[token_key] = len(pool_list)
                        pool_list.append(item)
                normalized[pool_name] = pool_list
            for pool_name, items in submitted.items():
                if pool_name not in normalized:
                    normalized[pool_name] = items

            await storage.save_tokens(normalized)
            mgr = await get_token_manager()
            await mgr.reload()

        concurrency = _resolve_nsfw_refresh_concurrency()
        retries = _resolve_nsfw_refresh_retries()
        _trigger_account_settings_refresh_background(
            tokens=added_tokens,
            concurrency=concurrency,
            retries=retries,
        )

        return {
            "status": "success",
            "summary": {
                "added": len(added_tokens),
                "updated": updated_count,
                "invalid": invalid_count,
            },
        }
    except HTTPException:
        raise
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
