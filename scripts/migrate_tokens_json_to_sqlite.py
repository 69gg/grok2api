#!/usr/bin/env python3
"""Migrate token.json pool data into SQLite storage in batches."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

DEFAULT_POOLS = ("ssoBasic", "ssoSuper")


def _normalize_entry(item: Any) -> Dict[str, Any] | None:
    if isinstance(item, dict):
        data = dict(item)
    elif isinstance(item, str):
        data = {"token": item}
    else:
        return None
    token = data.get("token")
    if not isinstance(token, str) or not token.strip():
        return None
    if token.startswith("sso="):
        data["token"] = token[4:]
    return data


async def migrate(
    *,
    json_path: Path,
    batch_size: int,
    pools: Iterable[str],
) -> None:
    from grok2api.core.config import config
    from grok2api.core.paths import DATA_DIR
    from grok2api.core.storage import SQLStorage, StorageFactory

    await config.ensure_loaded()

    with json_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise SystemExit("token.json root must be an object")

    db_path = DATA_DIR / "grok2api.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    storage = SQLStorage(f"sqlite+aiosqlite:///{db_path}")
    StorageFactory._instance = storage
    await storage._ensure_schema()

    grand_total = 0
    for pool_name in pools:
        items = payload.get(pool_name)
        if not isinstance(items, list):
            print(f"skip pool {pool_name}: missing or not a list")
            continue
        total = len(items)
        print(f"migrating {pool_name}: {total:,} tokens (batch={batch_size})")
        for offset in range(0, total, batch_size):
            chunk = items[offset : offset + batch_size]
            updates: List[Dict[str, Any]] = []
            for item in chunk:
                token_data = _normalize_entry(item)
                if not token_data:
                    continue
                token_data["pool_name"] = pool_name
                token_data["_update_kind"] = "state"
                updates.append(token_data)
            if updates:
                await storage.save_tokens_delta(updates, deleted=None)
            done = min(offset + batch_size, total)
            print(f"  {pool_name}: {done:,}/{total:,}")
        grand_total += total

    loaded = await storage.load_tokens() or {}
    print("verify counts:")
    for pool_name in pools:
        count = len(loaded.get(pool_name, []))
        print(f"  {pool_name}: {count:,}")
    print(f"sqlite db: {db_path} ({db_path.stat().st_size / 1024 / 1024:.1f} MiB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        type=Path,
        default=PROJECT_ROOT / "data" / "token.json",
        help="Source token.json path",
    )
    parser.add_argument("--batch", type=int, default=5000, help="Batch size per upsert")
    parser.add_argument(
        "--pools",
        nargs="+",
        default=list(DEFAULT_POOLS),
        help="Pool names to import (default: ssoBasic ssoSuper)",
    )
    args = parser.parse_args()
    if not args.json.exists():
        raise SystemExit(f"token file not found: {args.json}")
    asyncio.run(
        migrate(
            json_path=args.json,
            batch_size=max(1, args.batch),
            pools=args.pools,
        )
    )


if __name__ == "__main__":
    main()
