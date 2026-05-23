"""Cache pristine console reasoning blobs before client-facing SSE transforms."""

from __future__ import annotations

import copy
from threading import Lock
from typing import Any, Dict

_lock = Lock()
_blobs_by_item_id: dict[str, str] = {}


def remember_reasoning_blob(item: Dict[str, Any]) -> None:
    if not isinstance(item, dict):
        return
    item_id = str(item.get("id") or "").strip()
    enc = item.get("encrypted_content")
    if not item_id or not isinstance(enc, str) or not enc:
        return
    with _lock:
        _blobs_by_item_id[item_id] = enc


def restore_reasoning_item(item: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return item
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return item
    with _lock:
        cached = _blobs_by_item_id.get(item_id)
    if not cached:
        return item
    restored = copy.deepcopy(item)
    restored["encrypted_content"] = cached
    return restored


def clear_reasoning_blobs() -> None:
    with _lock:
        _blobs_by_item_id.clear()


def is_display_only_replay_item(item: Dict[str, Any]) -> bool:
    if item.get("_grok2api_display_only"):
        return True
    item_id = str(item.get("id") or "").strip()
    return item_id.startswith("rs_grok2api_")


__all__ = [
    "clear_reasoning_blobs",
    "is_display_only_replay_item",
    "remember_reasoning_blob",
    "restore_reasoning_item",
]
