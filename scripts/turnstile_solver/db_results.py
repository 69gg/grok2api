import time
from typing import Any

RESULT_TTL_SECONDS = 30 * 60
MAX_RESULTS = 1000

# 内存数据库，用于临时存储验证码结果。
results_db: dict[str, dict[str, Any]] = {}


async def init_db() -> None:
    print("[系统] 结果数据库初始化成功 (内存模式)")


async def save_result(task_id: str, task_type: str, data: dict[str, Any] | str) -> None:
    if isinstance(data, dict):
        payload = dict(data)
    else:
        payload = {"value": data}
    payload.setdefault("taskType", task_type)
    payload.setdefault("createTime", time.time())
    payload["updatedAt"] = time.time()
    results_db[task_id] = payload
    await cleanup_old_results()
    print(f"[系统] 任务 {task_id} 状态更新: {payload.get('value', '正在处理')}")


async def load_result(task_id: str) -> dict[str, Any] | None:
    return results_db.get(task_id)


async def cleanup_old_results(days_old: int | None = None) -> int:
    ttl = RESULT_TTL_SECONDS if days_old is None else max(1, days_old) * 86400
    now = time.time()
    to_delete: list[str] = []
    for tid, res in results_db.items():
        created_at = float(res.get("createTime") or res.get("updatedAt") or now)
        if now - created_at > ttl:
            to_delete.append(tid)

    overflow = max(0, len(results_db) - len(to_delete) - MAX_RESULTS)
    if overflow:
        kept = [tid for tid in results_db if tid not in set(to_delete)]
        kept.sort(
            key=lambda tid: float(
                results_db[tid].get("updatedAt") or results_db[tid].get("createTime") or 0
            )
        )
        to_delete.extend(kept[:overflow])

    for tid in to_delete:
        results_db.pop(tid, None)
    return len(to_delete)
