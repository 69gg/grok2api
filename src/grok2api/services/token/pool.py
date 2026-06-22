"""Token 池管理"""

import random
from threading import Lock
from typing import Dict, List, Optional, Iterator, Set

from grok2api.services.token.models import TokenInfo, TokenStatus, TokenPoolStats
from grok2api.core.config import get_config


class TokenPool:
    """Token 池（管理一组 Token）"""

    def __init__(self, name: str):
        self.name = name
        self._tokens: Dict[str, TokenInfo] = {}
        self._round_robin_index = 0
        self._round_robin_lock = Lock()

    def add(self, token: TokenInfo):
        """添加 Token"""
        self._tokens[token.token] = token

    def remove(self, token_str: str) -> bool:
        """删除 Token"""
        if token_str in self._tokens:
            del self._tokens[token_str]
            return True
        return False

    def get(self, token_str: str) -> Optional[TokenInfo]:
        """获取 Token"""
        return self._tokens.get(token_str)

    def _is_consumed_mode(self) -> bool:
        """检查是否启用 consumed 模式"""
        try:
            return get_config("token.consumed_mode_enabled", False)
        except Exception:
            return False

    @staticmethod
    def _normalize_exclude(exclude: Optional[Set[str]] = None) -> Set[str]:
        return {
            token[4:] if token.startswith("sso=") else token
            for token in (exclude or set())
        }

    def _available_tokens(
        self,
        *,
        exclude: Optional[Set[str]] = None,
        prefer_tags: Optional[Set[str]] = None,
    ) -> List[TokenInfo]:
        consumed_mode = self._is_consumed_mode()
        excluded = self._normalize_exclude(exclude)
        available = [
            t
            for t in self._tokens.values()
            if t.is_available(consumed_mode=consumed_mode)
            and (not excluded or t.token not in excluded)
        ]
        if not available:
            return []

        if prefer_tags:
            preferred = [t for t in available if prefer_tags.issubset(set(t.tags or []))]
            if preferred:
                return preferred
        return available

    def select(
        self, exclude: Optional[Set[str]] = None, prefer_tags: Optional[Set[str]] = None
    ) -> Optional[TokenInfo]:
        """
        选择一个可用 Token

        默认模式（consumed_mode_enabled=false）:
            1. 选择 active 状态且 quota > 0 的 token
            2. 在可用集合中直接随机选择

        Consumed 模式（consumed_mode_enabled=true）:
            1. 选择 active 状态的 token
            2. 在可用集合中直接随机选择

        Args:
            exclude: 需要排除的 token 字符串集合
            prefer_tags: 优先选择包含这些 tag 的 token（若存在则仅在其子集中选择）
        """
        available = self._available_tokens(exclude=exclude, prefer_tags=prefer_tags)
        if not available:
            return None
        return random.choice(available)

    def select_round_robin(
        self,
        exclude: Optional[Set[str]] = None,
        prefer_tags: Optional[Set[str]] = None,
    ) -> Optional[TokenInfo]:
        """按池内顺序轮询选择可用 Token，并在每次选择后推进游标。"""
        available = self._available_tokens(exclude=exclude, prefer_tags=prefer_tags)
        if not available:
            return None

        available_tokens = {token.token for token in available}
        ordered_tokens = list(self._tokens.values())
        total = len(ordered_tokens)
        if total == 0:
            return None

        with self._round_robin_lock:
            start_index = self._round_robin_index % total
            for offset in range(total):
                index = (start_index + offset) % total
                token = ordered_tokens[index]
                if token.token not in available_tokens:
                    continue
                self._round_robin_index = (index + 1) % total
                return token
            return None

    def count(self) -> int:
        """Token 数量"""
        return len(self._tokens)

    def list(self) -> List[TokenInfo]:
        """获取所有 Token"""
        return list(self._tokens.values())

    def get_stats(self) -> TokenPoolStats:
        """获取池统计信息"""
        stats = TokenPoolStats(total=len(self._tokens))

        for token in self._tokens.values():
            stats.total_quota += token.quota
            stats.total_consumed += token.consumed

            if token.status == TokenStatus.ACTIVE:
                stats.active += 1
            elif token.status == TokenStatus.DISABLED:
                stats.disabled += 1
            elif token.status == TokenStatus.EXPIRED:
                stats.expired += 1
            elif token.status == TokenStatus.COOLING:
                stats.cooling += 1

        if stats.total > 0:
            stats.avg_quota = stats.total_quota / stats.total
            stats.avg_consumed = stats.total_consumed / stats.total

        return stats

    def _rebuild_index(self):
        """重建索引（预留接口，用于加载时调用）"""
        self._round_robin_index = self._round_robin_index % max(1, len(self._tokens))

    def __iter__(self) -> Iterator[TokenInfo]:
        return iter(self._tokens.values())


__all__ = ["TokenPool"]
