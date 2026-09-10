"""本册建设：整册课时页码整体平移（不重生成页图）。"""
from __future__ import annotations

from typing import Any

from ...extensions import db
from ...models import Lesson
from .volumes import get_diff_volume_by_code


def shift_volume_lesson_page_ranges(*, volume_code: str, delta: int) -> dict[str, Any]:
    delta = int(delta)
    if delta == 0:
        return {"ok": True, "delta": 0, "updated": 0, "message": "偏移为 0，未改动"}

    volume = get_diff_volume_by_code(volume_code)
    lessons = Lesson.query.filter_by(volume_id=volume.id).all()
    ranged = [les for les in lessons if les.page_start]
    if not ranged:
        raise ValueError("该册尚无已划分页码的课时")
    if any(int(les.page_start) + delta < 1 for les in ranged):
        raise ValueError("偏移后起始页会小于 1")

    from ..old_library.pdf.lesson_pages_build import _delete_lesson_pages_for_lessons

    _delete_lesson_pages_for_lessons([les.id for les in ranged])

    updates: list[dict[str, Any]] = []
    for les in ranged:
        start = int(les.page_start) + delta
        end = int(les.page_end or les.page_start) + delta
        les.page_start = start
        les.page_end = end
        les.page_range_verified = True
        updates.append(
            {
                "lesson_uid": les.lesson_uid,
                "page_start": start,
                "page_end": end,
            }
        )
    db.session.commit()
    direction = "后移" if delta > 0 else "前移"
    return {
        "ok": True,
        "delta": delta,
        "updated": len(updates),
        "lessons": updates,
        "message": f"已将 {len(updates)} 课页码{direction} {abs(delta)} 页",
    }
