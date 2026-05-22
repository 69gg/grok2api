"""统一代理配置。优先级：环境变量 > 配置文件 > 无。"""
import os
from typing import Optional, Dict

from grok2api.core.config import get_config


def get_proxy_url() -> str:
    """获取代理 URL。优先环境变量，回退配置文件。"""
    return (
        os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or get_config("proxy.base_proxy_url", "")
        or get_config("grok.base_proxy_url", "")
        or ""
    )


def get_proxies_dict() -> Optional[Dict[str, str]]:
    """返回 curl_cffi 格式的代理字典，无代理时返回 None。"""
    url = get_proxy_url()
    return {"http": url, "https": url} if url else None
