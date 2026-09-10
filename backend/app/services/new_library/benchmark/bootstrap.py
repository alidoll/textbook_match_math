"""新课标基准库 → volumes + lessons。"""
from __future__ import annotations

from ....extensions import db
from ....models import Lesson, Volume
from ....query.lesson_order import sort_order_for_lesson_no
from ....parsers.benchmark_xlsx import BenchmarkLessonRow
from ...lesson_delete import delete_all_lessons_for_volume
from ...old_library.volume_codes import (
    make_display_title,
    make_lesson_uid_for_volume,
    make_volume_code,
    normalize_term,
)
from .reader import load_volume_lesson_rows


def _assign_unit_numbers(rows: list[BenchmarkLessonRow]) -> list[tuple[BenchmarkLessonRow, int]]:
    out: list[tuple[BenchmarkLessonRow, int]] = []
    unit_counter = 0
    prev_unit = object()
    for row in rows:
        unit_title = (row.unit_title or "").strip() or "未命名单元"
        if unit_title != prev_unit:
            unit_counter += 1
            prev_unit = unit_title
        out.append((row, unit_counter))
    return out


def _prepare_lesson_rows(
    edition,
    rows: list[BenchmarkLessonRow],
    *,
    grade: int,
    term: str,
    book_type: str,
) -> list[tuple[BenchmarkLessonRow, int, str]]:
    """
    为写入 lessons 表准备行：单元内补全课次号，并按 lesson_uid 去重。
    基准 xlsx 可能出现重复行，或「大师课」等无课次号行。
    """
    term_key = normalize_term(term)
    prepared: list[tuple[BenchmarkLessonRow, int, str]] = []
    seen_uids: set[str] = set()
    lesson_counters: dict[int, int] = {}

    for row, unit_no in _assign_unit_numbers(rows):
        raw_no = (row.lesson_no or "").strip()
        if raw_no:
            lesson_no = raw_no
        else:
            lesson_counters[unit_no] = lesson_counters.get(unit_no, 0) + 1
            lesson_no = str(lesson_counters[unit_no])

        lesson_uid = make_lesson_uid_for_volume(
            edition,
            grade=grade,
            term=term_key,
            book_type=book_type,
            unit_no=unit_no,
            lesson_no=lesson_no,
        )
        if lesson_uid in seen_uids:
            continue
        seen_uids.add(lesson_uid)
        prepared.append((row, unit_no, lesson_no))
    return prepared


def preview_bootstrap(*, edition_id: str, grade: int, term: str) -> dict:
    edition, rows = load_volume_lesson_rows(edition_id=edition_id, grade=grade, term=term)
    prepared = _prepare_lesson_rows(
        edition, rows, grade=grade, term=term, book_type="new"
    )
    return {
        "edition_id": edition.edition_id,
        "edition_label": edition.label,
        "grade": grade,
        "term": normalize_term(term),
        "volume_code": make_volume_code(edition, grade=grade, term=term, book_type="new"),
        "display_title": make_display_title(edition, grade=grade, term=term),
        "lesson_count": len(prepared),
        "lessons": [
            {
                "unit_title": row.unit_title,
                "lesson_no": lesson_no,
                "lesson_name": row.lesson_name,
                "old_course_id": row.old_course_id,
                "page_count": row.page_count,
            }
            for row, _unit_no, lesson_no in prepared[:50]
        ],
        "truncated": len(prepared) > 50,
    }


def bootstrap_volume_from_benchmark(
    *,
    edition_id: str,
    grade: int,
    term: str,
    replace: bool = False,
) -> dict:
    edition, rows = load_volume_lesson_rows(edition_id=edition_id, grade=grade, term=term)
    if not rows:
        raise ValueError(
            f"新课标基准库中未找到课时：{edition.label} {grade}年级 {normalize_term(term)}册"
        )

    volume_code = make_volume_code(edition, grade=grade, term=term, book_type="new")
    term_key = normalize_term(term)
    book_type = "new"

    volume = Volume.query.filter_by(volume_code=volume_code, book_type=book_type).first()
    existing_count = (
        Lesson.query.filter_by(volume_id=volume.id).count() if volume else 0
    )
    if volume and existing_count and not replace:
        raise ValueError(
            f"册次 {volume_code} 已有 {existing_count} 节课时；"
            "请传 replace=true 覆盖重建"
        )

    if not volume:
        volume = Volume(
            volume_code=volume_code,
            subject=edition.subject,
            edition=edition.label,
            grade=grade,
            semester=term_key,
            book_type=book_type,
            display_title=make_display_title(edition, grade=grade, term=term),
            parse_status="pending",
        )
        db.session.add(volume)
        db.session.flush()
    else:
        volume.display_title = make_display_title(edition, grade=grade, term=term)
        volume.parse_status = volume.parse_status or "pending"
        if replace:
            delete_all_lessons_for_volume(volume)

    created = 0
    for row, unit_no, lesson_no in _prepare_lesson_rows(
        edition, rows, grade=grade, term=term, book_type=book_type
    ):
        lesson_name = row.lesson_name or row.lesson_raw or "未命名"
        created += 1
        les = Lesson(
            volume_id=volume.id,
            lesson_uid=make_lesson_uid_for_volume(
                edition,
                grade=grade,
                term=term_key,
                book_type=book_type,
                unit_no=unit_no,
                lesson_no=lesson_no,
            ),
            unit_no=unit_no,
            unit_title=(row.unit_title or "").strip() or "未命名单元",
            lesson_no=lesson_no,
            lesson_name=lesson_name,
            sort_order=sort_order_for_lesson_no(lesson_no, fallback=created * 10),
            old_course_id=row.old_course_id or None,
            page_count=row.page_count,
            import_batch=row.import_batch,
            slides_fetch_status="not_uploaded",
        )
        db.session.add(les)
        created += 1

    db.session.commit()
    return {
        "volume_id": volume.id,
        "volume_code": volume.volume_code,
        "display_title": volume.display_title,
        "lessons_created": created,
        "replace": replace,
    }
