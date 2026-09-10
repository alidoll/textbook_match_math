"""解析新课时的粗分配对。"""
from __future__ import annotations

from sqlalchemy import desc

from ....models import Lesson, LessonMatch
from ...lesson_lookup import get_lesson_by_uid


def get_lesson_match_for_new_lesson(
    new_lesson_uid: str,
    *,
    lesson_match_id: str | None = None,
) -> tuple[LessonMatch, Lesson, Lesson]:
    new_les = get_lesson_by_uid(new_lesson_uid, book_type="new")
    if lesson_match_id:
        match = LessonMatch.query.get(lesson_match_id)
        if not match or match.new_lesson_id != new_les.id:
            raise ValueError("无效的 lesson_match_id")
    else:
        match = (
            LessonMatch.query.filter_by(new_lesson_id=new_les.id)
            .filter(LessonMatch.old_lesson_id.isnot(None))
            .filter(LessonMatch.match_tier.in_(("exact", "high_similarity")))
            .order_by(LessonMatch.match_rank, desc(LessonMatch.created_at))
            .first()
        )
    if not match or not match.old_lesson_id:
        raise ValueError("本课尚无粗分旧课配对，请先运行粗分")
    old_les = Lesson.query.get(match.old_lesson_id)
    if not old_les:
        raise ValueError("粗分配对指向的旧课不存在")
    return match, old_les, new_les
