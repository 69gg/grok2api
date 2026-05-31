"""
Grok 用量服务
"""

import asyncio
import uuid
from typing import Dict, Optional

import orjson
from curl_cffi.requests import AsyncSession

from grok2api.core.logger import logger
from grok2api.core.config import get_config
from grok2api.core.proxy import get_proxies_dict
from grok2api.core.exceptions import UpstreamException, AppException
from grok2api.services.grok.statsig import StatsigService
from grok2api.services.grok.fingerprint import get_impersonate, get_user_agent, is_firefox_ua
from grok2api.services.grok.retry import retry_on_status
from grok2api.services.reverse.utils.retry import extract_status_for_retry


LIMITS_API = "https://grok.com/rest/rate-limits"
TIMEOUT = 10
DEFAULT_MAX_CONCURRENT = 25
_USAGE_SEMAPHORE = asyncio.Semaphore(DEFAULT_MAX_CONCURRENT)
_USAGE_SEM_VALUE = DEFAULT_MAX_CONCURRENT

def _get_usage_semaphore() -> asyncio.Semaphore:
    global _USAGE_SEMAPHORE, _USAGE_SEM_VALUE
    value = get_config("performance.usage_max_concurrent", DEFAULT_MAX_CONCURRENT)
    try:
        value = int(value)
    except Exception:
        value = DEFAULT_MAX_CONCURRENT
    value = max(1, value)
    if value != _USAGE_SEM_VALUE:
        _USAGE_SEM_VALUE = value
        _USAGE_SEMAPHORE = asyncio.Semaphore(value)
    return _USAGE_SEMAPHORE


class UsageService:
    """用量查询服务"""
    
    def __init__(self):
        self.timeout = get_config("grok.timeout", TIMEOUT)
    
    def _build_headers(self, token: str) -> dict:
        """构建请求头"""
        user_agent = get_user_agent()
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Baggage": "sentry-environment=production,sentry-release=d6add6fb0460641fd482d767a335ef72b9b6abb8,sentry-public_key=b311e0f2690c81f25e2c4cf6d4f7ce1c",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "Origin": "https://grok.com",
            "Pragma": "no-cache",
            "Referer": "https://grok.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": user_agent,
        }

        if not is_firefox_ua():
            headers["Priority"] = "u=1, i"
            headers["Sec-Ch-Ua"] = '"Google Chrome";v="136", "Chromium";v="136", "Not(A:Brand";v="24"'
            headers["Sec-Ch-Ua-Arch"] = "arm"
            headers["Sec-Ch-Ua-Bitness"] = "64"
            headers["Sec-Ch-Ua-Mobile"] = "?0"
            headers["Sec-Ch-Ua-Model"] = ""
            headers["Sec-Ch-Ua-Platform"] = '"macOS"'
        
        # Statsig ID
        headers["x-statsig-id"] = StatsigService.gen_id()
        headers["x-xai-request-id"] = str(uuid.uuid4())
        
        # Cookie
        token = token[4:] if token.startswith("sso=") else token
        cf = get_config("grok.cf_clearance", "")
        headers["Cookie"] = f"sso={token};cf_clearance={cf}" if cf else f"sso={token}"
        
        return headers
    
    def _build_proxies(self) -> Optional[dict]:
        """构建代理配置"""
        return get_proxies_dict()
    
    async def get(self, token: str, model_name: str = "grok-4-1-thinking-1129") -> Dict:
        """
        获取速率限制信息
        
        Args:
            token: 认证 Token
            model_name: 模型名称
            
        Returns:
            响应数据
            
        Raises:
            UpstreamException: 当获取失败且重试耗尽时
        """
        async with _get_usage_semaphore():
            # 定义状态码提取器
            extract_status = extract_status_for_retry
            
            # 定义实际的请求函数
            async def do_request():
                try:
                    headers = self._build_headers(token)
                    payload = {
                        "requestKind": "DEFAULT",
                        "modelName": model_name
                    }
                    
                    async with AsyncSession() as session:
                        response = await session.post(
                            LIMITS_API,
                            headers=headers,
                            json=payload,
                            impersonate=get_impersonate(),
                            timeout=self.timeout,
                            proxies=self._build_proxies()
                        )
                    
                    if response.status_code == 200:
                        data = response.json()
                        remaining = data.get('remainingTokens', 0)
                        logger.info(f"Usage: quota {remaining} remaining")
                        return data
                    
                    logger.error(f"Usage failed: {response.status_code}")

                    raise UpstreamException(
                        message=f"Failed to get usage stats: {response.status_code}",
                        details={"status": response.status_code}
                    )
                    
                except Exception as e:
                    if isinstance(e, UpstreamException):
                        raise
                    logger.error(f"Usage error: {e}")
                    raise UpstreamException(
                        message=f"Usage service error: {str(e)}",
                        details={"error": str(e), "status": 502},
                    )
            
            # 带重试的执行
            try:
                result = await retry_on_status(
                    do_request,
                    extract_status=extract_status
                )
                return result
                
            except Exception as e:
                # 最后一次失败已经被记录
                raise


__all__ = ["UsageService"]
