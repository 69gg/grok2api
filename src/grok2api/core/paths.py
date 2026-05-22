"""Shared filesystem paths for the application."""

from __future__ import annotations

import os
from pathlib import Path


def find_project_root() -> Path:
    """Resolve the project root without relying on the current working directory."""
    env_root = os.getenv("GROK2API_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return path.parents[3]


PROJECT_ROOT = find_project_root()
CONFIG_FILE = PROJECT_ROOT / "config.toml"
CONFIG_EXAMPLE_FILE = PROJECT_ROOT / "config.toml.example"
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser()
LOG_DIR = Path(os.getenv("LOG_DIR", str(PROJECT_ROOT / "logs"))).expanduser()


__all__ = [
    "CONFIG_EXAMPLE_FILE",
    "CONFIG_FILE",
    "DATA_DIR",
    "LOG_DIR",
    "PROJECT_ROOT",
    "find_project_root",
]
