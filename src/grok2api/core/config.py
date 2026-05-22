"""Runtime configuration loaded from the project root config.toml."""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from pathlib import Path
from typing import Any
import tomllib

from grok2api.core.logger import logger
from grok2api.core.paths import CONFIG_EXAMPLE_FILE, CONFIG_FILE


DictConfig = dict[str, Any]


def _deep_merge(base: DictConfig, override: DictConfig) -> DictConfig:
    if not isinstance(base, dict):
        return deepcopy(override) if isinstance(override, dict) else deepcopy(base)

    result = deepcopy(base)
    if not isinstance(override, dict):
        return result

    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_toml(path: Path) -> DictConfig:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(f"Failed to load config from {path}: {exc}")
        return {}


def _set_path(config_data: DictConfig, dotted_key: str, value: str) -> None:
    section, _, key = dotted_key.partition(".")
    if not section or not key:
        return
    target = config_data.setdefault(section, {})
    if isinstance(target, dict):
        target[key] = value


def _apply_env_overrides(config_data: DictConfig) -> DictConfig:
    result = deepcopy(config_data)
    explicit_map = {
        "GROK2API_API_KEY": "app.api_key",
        "GROK2API_APP_KEY": "app.app_key",
        "GROK2API_FUNCTION_KEY": "app.function_key",
        "SERVER_HOST": "server.host",
        "SERVER_PORT": "server.port",
        "SERVER_WORKERS": "server.workers",
        "BASE_PROXY_URL": "proxy.base_proxy_url",
        "ASSET_PROXY_URL": "proxy.asset_proxy_url",
        "CF_CLEARANCE": "proxy.cf_clearance",
        "CF_COOKIES": "proxy.cf_cookies",
        "YESCAPTCHA_KEY": "register.yescaptcha_key",
        "WORKER_DOMAIN": "register.worker_domain",
        "EMAIL_DOMAIN": "register.email_domain",
        "ADMIN_PASSWORD": "register.admin_password",
    }
    for env_key, config_key in explicit_map.items():
        if env_key in os.environ:
            _set_path(result, config_key, os.environ[env_key])
    return result


class Config:
    """Small async-safe configuration holder."""

    def __init__(self) -> None:
        self._config: DictConfig = {}
        self._defaults: DictConfig = {}
        self._code_defaults: DictConfig = {}
        self._loaded = False
        self._load_lock = asyncio.Lock()

    def register_defaults(self, defaults: DictConfig) -> None:
        self._code_defaults = _deep_merge(self._code_defaults, defaults or {})

    async def load(self) -> None:
        defaults = _deep_merge(self._code_defaults, _load_toml(CONFIG_EXAMPLE_FILE))
        runtime = _load_toml(CONFIG_FILE)
        merged = _deep_merge(defaults, runtime)
        merged = _apply_env_overrides(merged)
        self._defaults = defaults
        self._config = merged
        self._loaded = True
        if not CONFIG_FILE.exists():
            logger.warning(
                f"{CONFIG_FILE.name} not found; using defaults from {CONFIG_EXAMPLE_FILE.name}"
            )

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            await self.load()

    def get(self, key: str, default: Any = None) -> Any:
        if "." not in key:
            return self._config.get(key, default)
        section, attr = key.split(".", 1)
        value = self._config.get(section, {})
        if not isinstance(value, dict):
            return default
        return value.get(attr, default)

    async def update(self, new_config: DictConfig) -> None:
        async with self._load_lock:
            base = _deep_merge(self._defaults, self._config or {})
            self._config = _deep_merge(base, new_config or {})
            self._loaded = True
            await _write_toml(CONFIG_FILE, self._config)


async def _write_toml(path: Path, data: DictConfig) -> None:
    def _format_value(value: Any) -> str:
        import json

        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return json.dumps(str(value), ensure_ascii=False)

    lines: list[str] = []
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_format_value(value)}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


config = Config()


def get_config(key: str, default: Any = None) -> Any:
    return config.get(key, default)


def register_defaults(defaults: DictConfig) -> None:
    config.register_defaults(defaults)


__all__ = ["Config", "config", "get_config", "register_defaults"]
