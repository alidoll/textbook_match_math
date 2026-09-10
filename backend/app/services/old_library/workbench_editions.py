"""工作台：版本列表与册次状态组装。"""
from __future__ import annotations

from sqlalchemy import func, not_, select

from ...extensions import db
from ...models import Block, CoursewareSlide, Lesson, Volume
from ..new_library.volumes import volume_workbench_pipeline
from .edition_registry import edition_to_api_dict, grade_term_pairs_for_edition, list_active_editions
from .volume_codes import make_volume_code


def _old_volume_build_progress(vol: Volume, lesson_count: int) -> dict | None:
    if lesson_count <= 0:
        return None
    lessons = Lesson.query.filter_by(volume_id=vol.id).all()
    ids = [les.id for les in lessons]
    if not ids:
        return None
    with_slides = db.session.scalar(
        select(func.count(func.distinct(CoursewareSlide.lesson_id))).where(
            CoursewareSlide.lesson_id.in_(ids)
        )
    ) or 0
    with_blocks = db.session.scalar(
        select(func.count(func.distinct(Block.lesson_id))).where(Block.lesson_id.in_(ids))
    ) or 0
    n = len(lessons)
    return {
        "has_pdf": bool(vol.blob_id),
        "parse_done": vol.parse_status == "done",
        "lessons_with_slides": with_slides,
        "lessons_with_blocks": with_blocks,
        "lesson_count": n,
    }


def build_editions_payload(*, book_type: str) -> list[dict]:
    eds = list_active_editions()
    if book_type == "old":
        eds = [e for e in eds if e.has_old_benchmark]
    elif book_type == "new":
        eds = [e for e in eds if e.new_benchmark_sheet]
    editions = []
    for ed in eds:
        volumes = []
        for grade, term in grade_term_pairs_for_edition(ed):
            code = make_volume_code(ed, grade=grade, term=term, book_type=book_type)
            vol = Volume.query.filter_by(volume_code=code, book_type=book_type).first()
            lesson_count = 0
            if vol:
                q = Lesson.query.filter_by(volume_id=vol.id)
                if book_type == "new":
                    q = q.filter(not_(Lesson.unit_title.contains("大师课")))
                lesson_count = q.count()
            vol_entry = {
                    "grade": grade,
                    "term": term,
                    "volume_code": code,
                    "lesson_count": lesson_count,
                    "parse_status": vol.parse_status if vol else None,
                    "has_pdf": bool(vol and vol.blob_id),
                    "in_db": vol is not None,
                }
            if book_type == "old" and vol and lesson_count > 0:
                vol_entry["build_progress"] = _old_volume_build_progress(vol, lesson_count)
            elif book_type == "new" and vol:
                vol_entry["pipeline"] = volume_workbench_pipeline(vol)
            volumes.append(vol_entry)
        payload = edition_to_api_dict(ed)
        payload["volumes"] = volumes
        editions.append(payload)
    return editions
