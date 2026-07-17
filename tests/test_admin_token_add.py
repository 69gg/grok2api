from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import grok2api.api.v1.admin.token as token_module
from grok2api.main import create_app

TEST_APP_KEY = "test-app-key"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_APP_KEY}"}


class _FakeStorage:
    """内存版 storage，仅实现 admin token 端点用到的接口。"""

    def __init__(self, initial: dict | None = None):
        self._tokens = initial or {}
        self.saved: dict | None = None

    async def load_tokens(self) -> dict:
        return self._tokens

    async def save_tokens(self, data: dict) -> None:
        self.saved = data
        self._tokens = data

    @asynccontextmanager
    async def acquire_lock(self, name: str, timeout: int = 10):
        yield


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch):
    storage = _FakeStorage()
    manager = AsyncMock()
    refresh_calls: list[list[str]] = []

    monkeypatch.setattr("grok2api.core.auth.get_app_key", lambda: TEST_APP_KEY)
    monkeypatch.setattr(token_module, "get_storage", lambda: storage)
    monkeypatch.setattr(
        token_module, "get_token_manager", AsyncMock(return_value=manager)
    )
    monkeypatch.setattr(
        token_module,
        "_trigger_account_settings_refresh_background",
        lambda tokens, concurrency, retries: refresh_calls.append(list(tokens)),
    )

    client = TestClient(create_app())
    return {
        "client": client,
        "storage": storage,
        "manager": manager,
        "refresh_calls": refresh_calls,
    }


def _post_add(client: TestClient, payload: dict, **kwargs):
    return client.post("/v1/admin/tokens/add", json=payload, headers=AUTH_HEADERS, **kwargs)


def test_add_tokens_to_empty_storage(harness) -> None:
    resp = _post_add(
        harness["client"],
        {"ssoBasic": ["tok-a", {"token": "sso=tok-b", "tags": ["x"], "note": "n"}]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["summary"] == {"added": 2, "updated": 0, "invalid": 0}

    saved = harness["storage"].saved
    assert saved is not None
    tokens = {item["token"]: item for item in saved["ssoBasic"]}
    assert set(tokens) == {"tok-a", "tok-b"}  # sso= 前缀被剥离
    assert tokens["tok-b"]["tags"] == ["x"]
    assert tokens["tok-b"]["note"] == "n"

    assert harness["refresh_calls"] == [["tok-a", "tok-b"]]
    harness["manager"].reload.assert_awaited()


def test_add_tokens_preserves_existing(harness) -> None:
    harness["storage"]._tokens = {
        "ssoBasic": [{"token": "old-1", "note": "keep", "quota": 5}],
        "ssoSuper": ["old-2"],
    }

    resp = _post_add(harness["client"], {"ssoBasic": ["new-1"]})

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 1, "updated": 0, "invalid": 0}

    saved = harness["storage"].saved
    basic = {item["token"]: item for item in saved["ssoBasic"]}
    assert set(basic) == {"old-1", "new-1"}  # 旧 token 未被清掉
    assert basic["old-1"]["note"] == "keep"
    assert basic["old-1"]["quota"] == 5
    assert saved["ssoSuper"] == ["old-2"]  # 未涉及的存量条目原样保留

    assert harness["refresh_calls"] == [["new-1"]]


def test_add_tokens_upserts_existing(harness) -> None:
    harness["storage"]._tokens = {
        "ssoBasic": [{"token": "tok-a", "note": "old", "quota": 3, "use_count": 7}],
    }

    resp = _post_add(
        harness["client"], {"ssoBasic": [{"token": "tok-a", "note": "new"}]}
    )

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 0, "updated": 1, "invalid": 0}

    saved = harness["storage"].saved
    entries = saved["ssoBasic"]
    assert len(entries) == 1  # 不产生重复条目
    entry = entries[0]
    assert entry["note"] == "new"  # 提交字段覆盖
    assert entry["quota"] == 3  # 未提交字段保留
    assert entry["use_count"] == 7

    # 已存在 token 不触发后台刷新
    assert harness["refresh_calls"] == [[]]


def test_add_tokens_mixed_new_and_existing(harness) -> None:
    harness["storage"]._tokens = {"ssoBasic": ["tok-a"]}

    resp = _post_add(
        harness["client"],
        {"ssoBasic": ["tok-a", "tok-b"], "ssoSuper": [{"token": "tok-c"}]},
    )

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 2, "updated": 1, "invalid": 0}

    saved = harness["storage"].saved
    assert [item["token"] for item in saved["ssoBasic"]] == ["tok-a", "tok-b"]
    assert [item["token"] for item in saved["ssoSuper"]] == ["tok-c"]
    assert harness["refresh_calls"] == [["tok-b", "tok-c"]]


def test_add_tokens_invalid_items_rejected(harness) -> None:
    resp = _post_add(harness["client"], {"ssoBasic": ["", 123, {"note": "no-token"}]})

    assert resp.status_code == 400
    assert harness["storage"].saved is None  # 未做任何保存
    assert harness["refresh_calls"] == []


def test_add_tokens_partial_invalid(harness) -> None:
    resp = _post_add(harness["client"], {"ssoBasic": ["ok-1", "", 42]})

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 1, "updated": 0, "invalid": 2}
    assert [item["token"] for item in harness["storage"].saved["ssoBasic"]] == ["ok-1"]


def test_add_tokens_empty_payload_rejected(harness) -> None:
    resp = _post_add(harness["client"], {})

    assert resp.status_code == 400
    assert harness["storage"].saved is None


def test_add_tokens_sanitizes_unicode(harness) -> None:
    resp = _post_add(
        harness["client"], {"ssoBasic": ["sso=tok\u2012x\u200b y"]}
    )

    assert resp.status_code == 200
    # en-dash/连字符归一化为 "-"，零宽字符移除，空白去除，sso= 前缀剥离
    assert resp.json()["summary"]["added"] == 1
    assert [item["token"] for item in harness["storage"].saved["ssoBasic"]] == [
        "tok-xy"
    ]


def test_add_tokens_requires_auth(harness) -> None:
    client = harness["client"]

    resp = client.post("/v1/admin/tokens/add", json={"ssoBasic": ["tok-a"]})
    assert resp.status_code == 401

    resp = client.post(
        "/v1/admin/tokens/add",
        json={"ssoBasic": ["tok-a"]},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert resp.status_code == 401

    assert harness["storage"].saved is None


def test_add_tokens_preserves_invalid_existing(harness) -> None:
    """存量中无法通过当前 TokenInfo 校验的条目必须原样保留，不被静默丢弃。"""
    harness["storage"]._tokens = {
        "ssoBasic": [
            {"token": "broken", "quota": "not-an-int"},
            12345,
            {"note": "no-token-field"},
        ],
    }

    resp = _post_add(harness["client"], {"ssoBasic": ["new-1"]})

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 1, "updated": 0, "invalid": 0}

    saved = harness["storage"].saved["ssoBasic"]
    assert saved[0] == {"token": "broken", "quota": "not-an-int"}
    assert saved[1] == 12345
    assert saved[2] == {"note": "no-token-field"}
    assert saved[3]["token"] == "new-1"


def test_add_tokens_keeps_updated_token_position(harness) -> None:
    """合并更新不改变 token 在池中的原有位置。"""
    harness["storage"]._tokens = {"ssoBasic": ["tok-a", "tok-b", "tok-c"]}

    resp = _post_add(
        harness["client"], {"ssoBasic": [{"token": "tok-b", "note": "x"}]}
    )

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 0, "updated": 1, "invalid": 0}

    saved = harness["storage"].saved["ssoBasic"]
    assert len(saved) == 3
    assert saved[0] == "tok-a"  # 未提交的存量条目原样保留
    assert saved[1]["token"] == "tok-b"
    assert saved[1]["note"] == "x"
    assert saved[2] == "tok-c"


def test_add_tokens_moves_across_pools(harness) -> None:
    """已有 token 提交到其他池 = 跨池移动：从原池移除，字段保留，不触发刷新。"""
    harness["storage"]._tokens = {
        "ssoBasic": [{"token": "tok-a", "note": "n", "use_count": 7}, "tok-b"],
        "ssoSuper": ["tok-c"],
    }

    resp = _post_add(
        harness["client"], {"ssoSuper": [{"token": "tok-a", "tags": ["moved"]}]}
    )

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 0, "updated": 1, "invalid": 0}

    saved = harness["storage"].saved
    assert saved["ssoBasic"] == ["tok-b"]  # 从原池移除，不再跨池重复

    super_entries = saved["ssoSuper"]
    assert super_entries[0] == "tok-c"  # 目标池存量原样保留
    moved = super_entries[1]
    assert moved["token"] == "tok-a"
    assert moved["note"] == "n"  # 原池字段保留
    assert moved["use_count"] == 7
    assert moved["tags"] == ["moved"]  # 提交字段覆盖

    assert harness["refresh_calls"] == [[]]  # 非新 token，不触发刷新


def test_add_tokens_move_removes_all_duplicates(harness) -> None:
    """原池中重复出现的 token 跨池移动时全部移除，目标池只留一份。"""
    harness["storage"]._tokens = {"ssoBasic": ["tok-a", "tok-b", "tok-a"]}

    resp = _post_add(harness["client"], {"ssoSuper": ["tok-a"]})

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 0, "updated": 1, "invalid": 0}

    saved = harness["storage"].saved
    assert saved["ssoBasic"] == ["tok-b"]
    assert [item["token"] for item in saved["ssoSuper"]] == ["tok-a"]


def test_update_tokens_still_full_replaces(harness) -> None:
    """重构后 POST /tokens 语义不变：全量替换 + 识别新增 token。"""
    harness["storage"]._tokens = {
        "ssoBasic": [{"token": "old-1", "note": "keep"}],
        "ssoSuper": ["old-2"],
    }

    resp = harness["client"].post(
        "/v1/admin/tokens",
        json={"ssoBasic": [{"token": "old-1", "note": "changed"}, "new-1"]},
        headers=AUTH_HEADERS,
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "message": "Token 已更新"}

    saved = harness["storage"].saved
    # ssoSuper 未提交 → 被全量替换语义移除；old-1 字段合并更新
    assert set(saved) == {"ssoBasic"}
    basic = {item["token"]: item for item in saved["ssoBasic"]}
    assert set(basic) == {"old-1", "new-1"}
    assert basic["old-1"]["note"] == "changed"

    assert harness["refresh_calls"] == [["new-1"]]


def test_add_tokens_preserves_non_list_pool(harness) -> None:
    """存量中损坏为非 list 的池，本次未提交时必须原样透传，不被静默丢弃。"""
    harness["storage"]._tokens = {
        "corrupted": {"unexpected": "dict"},
        "ssoBasic": ["old-1"],
    }

    resp = _post_add(harness["client"], {"ssoBasic": ["new-1"]})

    assert resp.status_code == 200
    saved = harness["storage"].saved
    assert saved["corrupted"] == {"unexpected": "dict"}
    assert saved["ssoBasic"][0] == "old-1"  # 存量条目原样保留
    assert saved["ssoBasic"][1]["token"] == "new-1"


def test_add_tokens_replaces_non_list_pool_when_submitted(harness) -> None:
    """损坏为非 list 的池本次被提交时，由提交数据整体替换。"""
    harness["storage"]._tokens = {"ssoBasic": "not-a-list"}

    resp = _post_add(harness["client"], {"ssoBasic": ["new-1"]})

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 1, "updated": 0, "invalid": 0}
    assert [item["token"] for item in harness["storage"].saved["ssoBasic"]] == ["new-1"]


def test_add_tokens_updated_count_dedupes_across_pools(harness) -> None:
    """同一存量 token 提交到多个池时，updated 按 token 只计 1 次。"""
    harness["storage"]._tokens = {"ssoBasic": ["tok-a"]}

    resp = _post_add(
        harness["client"], {"ssoBasic": ["tok-a"], "ssoSuper": ["tok-a"]}
    )

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 0, "updated": 1, "invalid": 0}


def test_add_tokens_upsert_dedupes_same_pool(harness) -> None:
    """同池内重复出现的 token 被提交更新时收敛为一份（保留第一处位置）。"""
    harness["storage"]._tokens = {
        "ssoBasic": [
            {"token": "tok-a", "note": "old", "use_count": 7},
            "tok-b",
            {"token": "tok-a", "note": "older"},
        ],
    }

    resp = _post_add(
        harness["client"], {"ssoBasic": [{"token": "tok-a", "note": "new"}]}
    )

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 0, "updated": 1, "invalid": 0}

    saved = harness["storage"].saved["ssoBasic"]
    assert len(saved) == 2  # 重复条目收敛，不再残留
    assert saved[0]["token"] == "tok-a"
    assert saved[0]["note"] == "new"  # 提交字段覆盖
    assert saved[0]["use_count"] == 7  # 以第一处出现的存量条目为合并 base
    assert saved[1] == "tok-b"


def test_add_tokens_repairs_broken_existing(harness) -> None:
    """存量条目的损坏字段在提交更新时被剥离并以模型默认值兜底，计为 updated。"""
    harness["storage"]._tokens = {
        "ssoBasic": [{"token": "tok-a", "quota": "not-an-int", "use_count": 7}],
    }

    resp = _post_add(
        harness["client"], {"ssoBasic": [{"token": "tok-a", "note": "x"}]}
    )

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 0, "updated": 1, "invalid": 0}

    saved = harness["storage"].saved["ssoBasic"]
    assert len(saved) == 1
    entry = saved[0]
    assert entry["token"] == "tok-a"
    assert entry["quota"] == 80  # 损坏字段剥离后回落到模型默认值
    assert entry["use_count"] == 7  # 其余存量字段保留
    assert entry["note"] == "x"


def test_add_tokens_rejects_submitter_invalid_field(harness) -> None:
    """提交方自己提供的字段校验失败时不补救，仍计 invalid。"""
    harness["storage"]._tokens = {"ssoBasic": [{"token": "tok-a", "quota": 3}]}

    resp = _post_add(
        harness["client"], {"ssoBasic": [{"token": "tok-a", "quota": "not-an-int"}]}
    )

    assert resp.status_code == 400  # 唯一条目 invalid → 无有效提交
    assert harness["storage"].saved is None  # 存量未被改动


def test_add_tokens_repair_counts_as_updated_not_400(harness) -> None:
    """修复损坏存量 token 是唯一有效条目时返回 200 而非 400。"""
    harness["storage"]._tokens = {
        "ssoBasic": [{"token": "tok-a", "tags": "not-a-list"}],
    }

    resp = _post_add(harness["client"], {"ssoBasic": ["tok-a"]})

    assert resp.status_code == 200
    assert resp.json()["summary"] == {"added": 0, "updated": 1, "invalid": 0}
    saved = harness["storage"].saved["ssoBasic"]
    assert saved[0]["token"] == "tok-a"
    assert saved[0]["tags"] == []  # 损坏字段剥离后回落到模型默认值
