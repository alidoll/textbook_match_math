"""按 lesson_uid 查询课时并校验册次类型。"""
from __future__ import annotations

from ..models import Lesson, Volume


def get_lesson_by_uid(lesson_uid: str, *, book_type: str) -> Lesson:
    uid = (lesson_uid or "").strip()
    if not uid:
        raise ValueError("缺少 lesson_uid")
    bt = (book_type or "").strip().lower()
    if bt not in ("old", "new"):
        raise ValueError(f"无效 book_type：{book_type}")
    les = Lesson.query.filter_by(lesson_uid=uid).first()
    if not les:
        raise ValueError(f"未找到课时：{uid}")
    volume = Volume.query.get(les.volume_id)
    if not volume or volume.book_type != bt:
        label = "旧库" if bt == "old" else "新教材"
        raise ValueError(f"非{label}课时：{uid}")
    return les
