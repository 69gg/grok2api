"""
Grok 服务默认配置

此文件读取 config.toml.example，作为 Grok 服务的默认值来源。
"""

import tomllib

from grok2api.core.logger import logger
from grok2api.core.paths import CONFIG_EXAMPLE_FILE

DEFAULTS_FILE = CONFIG_EXAMPLE_FILE

# Grok 服务默认配置（运行时从 config.toml.example 读取并缓存）
GROK_DEFAULTS: dict = {}


def get_grok_defaults():
    """获取 Grok 默认配置"""
    global GROK_DEFAULTS
    if GROK_DEFAULTS:
        return GROK_DEFAULTS
    if not DEFAULTS_FILE.exists():
        logger.warning(f"Defaults file not found: {DEFAULTS_FILE}")
        return GROK_DEFAULTS
    try:
        with DEFAULTS_FILE.open("rb") as f:
            GROK_DEFAULTS = tomllib.load(f)
    except Exception as e:
        logger.warning(f"Failed to load defaults from {DEFAULTS_FILE}: {e}")
    return GROK_DEFAULTS


__all__ = ["GROK_DEFAULTS", "get_grok_defaults"]
