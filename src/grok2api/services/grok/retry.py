"""
Grok API 重试工具

提供可配置的重试机制，支持:
- 可配置的重试次数
- 可配置的重试状态码
- 仅记录最后一次失败
"""

import asyncio
from functools import wraps
from typing import Any, Callable, List, Optional

from grok2api.core.config import get_config
from grok2api.core.exceptions import UpstreamException
from grok2api.core.logger import logger


class RetryConfig:
    """重试配置"""

    @staticmethod
    def get_max_retry() -> int:
        """获取最大重试次数"""
        return get_config("grok.max_retry", 1)

    @staticmethod
    def get_retry_codes() -> List[int]:
        """获取可重试的状态码"""
        return get_config("grok.retry_status_codes", [401, 429, 403])


class RetryContext:
    """重试上下文"""

    def __init__(self):
        self.attempt = 0
        self.max_retry = RetryConfig.get_max_retry()
        self.retry_codes = RetryConfig.get_retry_codes()
        self.last_error = None
        self.last_status = None

    def should_retry(self, status_code: int) -> bool:
        return self.attempt < self.max_retry and status_code in self.retry_codes

    def record_error(self, status_code: int, error: Exception):
        self.last_status = status_code
        self.last_error = error
        self.attempt += 1


async def retry_on_status(
    func: Callable,
    *args,
    extract_status: Callable[[Exception], Optional[int]] = None,
    on_retry: Callable[[int, int, Exception], None] = None,
    **kwargs,
) -> Any:
    """通用异步重试函数。"""
    ctx = RetryContext()

    if extract_status is None:
        def extract_status(e: Exception) -> Optional[int]:
            if isinstance(e, UpstreamException):
                return e.details.get("status") if e.details else None
            return None

    while ctx.attempt <= ctx.max_retry:
        try:
            result = await func(*args, **kwargs)
            if ctx.attempt > 0:
                logger.info(f"Retry succeeded after {ctx.attempt} attempts")
            return result
        except Exception as e:
            status_code = extract_status(e)
            if status_code is None:
                logger.error(f"Non-retryable error: {e}")
                raise

            ctx.record_error(status_code, e)
            if ctx.should_retry(status_code):
                delay = 0.5 * (ctx.attempt + 1)
                logger.warning(
                    f"Retry {ctx.attempt}/{ctx.max_retry} for status {status_code}, waiting {delay}s"
                )
                if on_retry:
                    on_retry(ctx.attempt, status_code, e)
                await asyncio.sleep(delay)
                continue

            if status_code in ctx.retry_codes:
                logger.warning(
                    f"Retry {ctx.attempt}/{ctx.max_retry} for status {status_code}, failed"
                )
                logger.error(
                    f"Retry exhausted after {ctx.max_retry} attempts, last status: {status_code}"
                )
            else:
                logger.error(f"Non-retryable status code: {status_code}")
            raise


def with_retry(
    extract_status: Callable[[Exception], Optional[int]] = None,
    on_retry: Callable[[int, int, Exception], None] = None,
):
    """重试装饰器。"""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await retry_on_status(
                func,
                *args,
                extract_status=extract_status,
                on_retry=on_retry,
                **kwargs,
            )

        return wrapper

    return decorator


__all__ = ["RetryConfig", "RetryContext", "retry_on_status", "with_retry"]
