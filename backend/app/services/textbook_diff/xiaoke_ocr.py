"""小科 OCR / 整课一键 / 整册一键（非小科安全回退）。

非小科 volume 不会走到这些函数；如被调用（如 cancel），返回安全默认值。
"""
from __future__ import annotations

from typing import Any


def start_xiaoke_volume_full(*, old_code: str, new_code: str, skip_cached: bool = True, new_lesson_uids: list[str] | None = None) -> dict[str, Any]:
    raise ValueError("当前册次非小科，不支持整册一键")


def start_xiaoke_lesson_full(*, old_code: str, new_code: str, new_lesson_uid: str, skip_cached: bool = True) -> dict[str, Any]:
    raise ValueError("当前册次非小科，不支持整课一键")


def cancel_xiaoke_volume_full(*, old_code: str, new_code: str) -> dict[str, Any]:
    return {"ok": True, "cancelled": 0, "message": "无小科整册任务可取消"}


def get_xiaoke_volume_full_status(*, old_code: str, new_code: str) -> dict[str, Any]:
    return {"status": "idle", "message": ""}


def get_xiaoke_lesson_full_status(*, old_code: str, new_code: str, new_lesson_uid: str) -> dict[str, Any]:
    return {"status": "idle", "message": ""}


def get_xiaoke_lesson_full_result(*, old_code: str, new_code: str, new_lesson_uid: str) -> dict[str, Any]:
    return {"ok": False, "error": "非小科册次"}


def ocr_xiaoke_new_lesson_pages(*, old_code: str, new_code: str, new_lesson_uid: str, **kwargs: Any) -> dict[str, Any]:
    return {"ok": False, "error": "非小科册次"}


def assemble_atoms_from_side_caches(*, ocr_old_vol: Any = None, new_vol: Any = None, old_page: int = 0, new_page: int = 0) -> dict[str, Any] | None:
    return None
