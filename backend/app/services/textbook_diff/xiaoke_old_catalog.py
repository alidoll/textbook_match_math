"""小科旧库目录快照（非小科安全回退）。"""
from __future__ import annotations

from typing import Any


def old_library_catalog_snapshot(old_vol: Any) -> dict[str, Any] | None:
    return None


def old_library_edition_catalogs(edition: str) -> list[dict[str, Any]]:
    return []


def resolve_old_library_volume(old_vol: Any) -> Any:
    return None
