"""小科课对比缓存（非小科安全回退）。"""
from __future__ import annotations

from typing import Any


def load_latest_lesson_compare(uid: str) -> dict[str, Any] | None:
    return None


def load_latest_lesson_bundle(uid: str, side: str) -> dict[str, Any] | None:
    return None


def compare_brief(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {}
