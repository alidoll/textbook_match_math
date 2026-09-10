"""旧库课时目录手动排序（转发至共用实现）。"""
from __future__ import annotations

from ..lesson_reorder import reorder_lesson_in_volume as _reorder


def reorder_lesson_in_volume(
    *,
    volume_code: str,
    lesson_uid: str,
    direction: str,
) -> None:
    _reorder(
        volume_code=volume_code,
        lesson_uid=lesson_uid,
        direction=direction,
        book_type="old",
    )
