"""Command line helpers for Grok2API."""

from __future__ import annotations

import argparse
import asyncio

from grok2api.core.config import config
from grok2api.services.register import get_auto_register_manager


async def _config_get(key: str | None) -> None:
    await config.ensure_loaded()
    if key:
        print(config.get(key, ""))
        return
    import orjson

    print(orjson.dumps(config._config, option=orjson.OPT_INDENT_2).decode())


async def _config_set(key: str, value: str) -> None:
    await config.ensure_loaded()
    section, sep, attr = key.partition(".")
    if not sep:
        raise SystemExit("配置键必须使用 section.key 格式")
    await config.update({section: {attr: value}})
    print("ok")


async def _register_status() -> None:
    manager = get_auto_register_manager()
    print(manager.get_status())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="grok2api")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cfg = sub.add_parser("config")
    cfg_sub = cfg.add_subparsers(dest="config_cmd", required=True)
    cfg_get = cfg_sub.add_parser("get")
    cfg_get.add_argument("key", nargs="?")
    cfg_set = cfg_sub.add_parser("set")
    cfg_set.add_argument("key")
    cfg_set.add_argument("value")

    reg = sub.add_parser("register")
    reg_sub = reg.add_subparsers(dest="register_cmd", required=True)
    reg_sub.add_parser("status")

    args = parser.parse_args(argv)
    if args.cmd == "config" and args.config_cmd == "get":
        asyncio.run(_config_get(args.key))
    elif args.cmd == "config" and args.config_cmd == "set":
        asyncio.run(_config_set(args.key, args.value))
    elif args.cmd == "register" and args.register_cmd == "status":
        asyncio.run(_register_status())


if __name__ == "__main__":
    main()
