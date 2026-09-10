"""删除册次下全部课时及关联数据（粗分、页图、建块等）。"""
from __future__ import annotations

import logging
import time

from sqlalchemy import or_
from sqlalchemy.exc import OperationalError

from ..extensions import db
from ..models import Lesson, LessonMatch, MatchJob, Volume
from .old_library.pdf.lesson_pages_build import _delete_lesson_pages_for_lessons

_log = logging.getLogger(__name__)
_LOCK_RETRY_ATTEMPTS = 3
_LOCK_RETRY_DELAY_S = 2.0


def _is_lock_wait_timeout(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "1205" in msg or "lock wait timeout" in msg


def _delete_lesson_matches_for_ids(lesson_ids: list[str]) -> None:
    """ORM 删除 lesson_matches，触发 block_matches 等 cascade。"""
    if not lesson_ids:
        return
    matches = (
        LessonMatch.query.filter(
            or_(
                LessonMatch.new_lesson_id.in_(lesson_ids),
                LessonMatch.old_lesson_id.in_(lesson_ids),
            )
        )
        .order_by(LessonMatch.id)
        .all()
    )
    for match in matches:
        db.session.delete(match)
    if matches:
        db.session.flush()


def _delete_match_jobs_for_volume(volume_id: str) -> None:
    jobs = MatchJob.query.filter_by(new_volume_id=volume_id).order_by(MatchJob.id).all()
    for job in jobs:
        db.session.delete(job)
    if jobs:
        db.session.flush()


def _delete_all_lessons_for_volume_once(volume: Volume) -> int:
    """单次尝试：ORM 级联删除，避免 bulk delete 长时间锁表。"""
    lessons = Lesson.query.filter_by(volume_id=volume.id).order_by(Lesson.id).all()
    if not lessons:
        return 0

    lesson_ids = [les.id for les in lessons]
    _delete_lesson_matches_for_ids(lesson_ids)
    _delete_match_jobs_for_volume(volume.id)
    _delete_lesson_pages_for_lessons(lesson_ids)

    for les in lessons:
        db.session.delete(les)
    db.session.flush()
    return len(lessons)


def delete_all_lessons_for_volume(volume: Volume) -> int:
    """删除册次课时；遇 InnoDB 锁等待超时时自动重试。"""
    volume_id = volume.id
    last_exc: Exception | None = None

    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            vol = db.session.get(Volume, volume_id)
            if vol is None:
                return 0
            return _delete_all_lessons_for_volume_once(vol)
        except OperationalError as exc:
            last_exc = exc
            db.session.rollback()
            if not _is_lock_wait_timeout(exc) or attempt + 1 >= _LOCK_RETRY_ATTEMPTS:
                raise
            _log.warning(
                "删除课时遇锁等待超时，%ss 后重试 (%d/%d)",
                _LOCK_RETRY_DELAY_S * (attempt + 1),
                attempt + 2,
                _LOCK_RETRY_ATTEMPTS,
            )
            time.sleep(_LOCK_RETRY_DELAY_S * (attempt + 1))

    if last_exc:
        raise last_exc
    return 0
