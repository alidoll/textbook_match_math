"""课时目录手动排序（旧库 / 新库共用）。"""
from __future__ import annotations

from ..extensions import db
from ..models import Lesson
from ..query.lesson_order import ensure_lesson_sort_orders, order_lessons_query
from .new_library.volumes import get_new_volume_by_code
from .old_library.volumes import get_old_volume_by_code
from .textbook_diff.volumes import get_diff_volume_by_code


def reorder_lesson_in_volume(
    *,
    volume_code: str,
    lesson_uid: str,
    direction: str,
    book_type: str = "old",
) -> None:
    direction = (direction or "").strip().lower()
    if direction not in ("up", "down"):
        raise ValueError("direction 须为 up 或 down")

    if book_type in ("diff_old", "diff_new"):
        volume = get_diff_volume_by_code(volume_code)
    elif book_type == "new":
        volume = get_new_volume_by_code(volume_code)
    else:
        volume = get_old_volume_by_code(volume_code)

    les = Lesson.query.filter_by(volume_id=volume.id, lesson_uid=lesson_uid).first()
    if not les:
        raise ValueError(f"未找到课时：{lesson_uid}")

    unit_lessons = order_lessons_query(
        Lesson.query.filter_by(volume_id=volume.id, unit_no=les.unit_no)
    ).all()
    if len(unit_lessons) < 2:
        return

    ensure_lesson_sort_orders(unit_lessons)
    idx = next(i for i, row in enumerate(unit_lessons) if row.id == les.id)
    if direction == "up":
        if idx == 0:
            return
        neighbor = unit_lessons[idx - 1]
    else:
        if idx >= len(unit_lessons) - 1:
            return
        neighbor = unit_lessons[idx + 1]

    les.sort_order, neighbor.sort_order = neighbor.sort_order, les.sort_order
    db.session.commit()
