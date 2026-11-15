"""Environment parsing that cannot stop the app from starting.

A mistyped value in .env should fall back to the default, not raise at import
time and leave the user with a stack trace instead of an application.
"""
from __future__ import annotations

import os


def int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def bool_env(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}
