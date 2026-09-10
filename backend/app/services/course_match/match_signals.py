"""粗分 v2：lesson_match_signals 写入。"""
from __future__ import annotations

from ...extensions import db
from ...models import Lesson, LessonMatchSignal, Volume
from .engine import build_candidate, score_components_for_pair


def _candidate_from_lesson(les: Lesson, vol: Volume | None):
    if not vol:
        return None
    return build_candidate(
        lesson_id=les.id,
        edition=vol.edition,
        grade=vol.grade,
        semester=vol.semester,
        unit_title=les.unit_title or "",
        lesson_no=les.lesson_no or "",
        lesson_name=les.lesson_name or "",
        old_course_id=les.old_course_id,
        page_count=les.page_count,
        body_text=les.body_text,
    )


def insert_match_signal(
    *,
    lesson_match_id: str,
    new_lesson: Lesson,
    old_lesson: Lesson,
    new_volume: Volume,
    old_volume: Volume,
    signal_source: str,
    llm_reason: str | None = None,
) -> None:
    new_c = _candidate_from_lesson(new_lesson, new_volume)
    old_c = _candidate_from_lesson(old_lesson, old_volume)
    if not new_c or not old_c:
        return

    comp = score_components_for_pair(new_c, old_c)
    row = LessonMatchSignal(lesson_match_id=lesson_match_id)
    db.session.add(row)

    row.title_score = comp["title_score"]
    row.body_score = comp["body_score"]
    row.unit_score = comp["unit_score"]
    row.context_score = comp["context_score"]
    row.page_count_delta = comp["page_count_delta"]
    row.signal_source = (signal_source or "rules").strip() or "rules"
    row.content_verified = bool(comp["content_verified"])
    reason = (llm_reason or "").strip()
    row.llm_reason = reason or None


def upsert_match_signal(
    *,
    lesson_match,
    new_lesson: Lesson,
    old_lesson: Lesson,
    new_volume: Volume,
    old_volume: Volume,
    signal_source: str,
    llm_reason: str | None = None,
) -> None:
    """兼容 ORM 对象调用。"""
    insert_match_signal(
        lesson_match_id=lesson_match.id,
        new_lesson=new_lesson,
        old_lesson=old_lesson,
        new_volume=new_volume,
        old_volume=old_volume,
        signal_source=signal_source,
        llm_reason=llm_reason,
    )