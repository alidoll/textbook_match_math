"""基准库 → volumes + lessons（模块 A bootstrap）。"""
from __future__ import annotations

from ....extensions import db
from ....models import Lesson, Volume
from ....query.lesson_order import sort_order_for_lesson_no
from ....parsers.benchmark_xlsx import BenchmarkLessonRow
from ..volume_codes import make_display_title, make_lesson_uid_for_volume, make_volume_code, normalize_term
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


def preview_bootstrap(*, edition_id: str, grade: int, term: str) -> dict:
    edition, rows = load_volume_lesson_rows(edition_id=edition_id, grade=grade, term=term)
    return {
        "edition_id": edition.edition_id,
        "edition_label": edition.label,
        "grade": grade,
        "term": normalize_term(term),
        "volume_code": make_volume_code(edition, grade=grade, term=term),
        "display_title": make_display_title(edition, grade=grade, term=term),
        "lesson_count": len(rows),
        "lessons": [
            {
                "unit_title": r.unit_title,
                "lesson_no": r.lesson_no or str(i + 1),
                "lesson_name": r.lesson_name,
                "old_course_id": r.old_course_id,
                "page_count": r.page_count,
            }
            for i, r in enumerate(rows[:50])
        ],
        "truncated": len(rows) > 50,
    }


def bootstrap_edition_from_benchmark(
    *,
    edition_id: str,
    replace: bool = False,
    only_missing: bool = True,
) -> dict:
    """按版本一键载入全部年级×学期基准目录（小学科学旧库）。

    only_missing=True（默认）：已有课时的册次跳过，不覆盖。
    replace=True：对已有册次覆盖重建（危险，需前端二次确认）。
    """
    from ..edition_registry import get_edition, grade_term_pairs_for_edition
    from ..volume_codes import make_volume_code

    edition = get_edition(edition_id)
    if not edition.has_old_benchmark or not (edition.benchmark_sheet or "").strip():
        raise FileNotFoundError(f"旧课标基准库暂无「{edition.label}」工作表")

    created: list[dict] = []
    skipped: list[dict] = []
    empty: list[dict] = []
    errors: list[dict] = []

    for grade, term in grade_term_pairs_for_edition(edition):
        term_key = normalize_term(term)
        volume_code = make_volume_code(edition, grade=grade, term=term_key)
        try:
            _, rows = load_volume_lesson_rows(
                edition_id=edition.edition_id, grade=grade, term=term_key
            )
        except (FileNotFoundError, ValueError, KeyError) as exc:
            errors.append(
                {
                    "volume_code": volume_code,
                    "grade": grade,
                    "term": term_key,
                    "error": str(exc),
                }
            )
            continue
        if not rows:
            empty.append(
                {"volume_code": volume_code, "grade": grade, "term": term_key}
            )
            continue

        volume = Volume.query.filter_by(volume_code=volume_code).first()
        existing_count = (
            Lesson.query.filter_by(volume_id=volume.id).count() if volume else 0
        )
        if existing_count and only_missing and not replace:
            skipped.append(
                {
                    "volume_code": volume_code,
                    "grade": grade,
                    "term": term_key,
                    "lesson_count": existing_count,
                    "reason": "already_loaded",
                }
            )
            continue

        try:
            result = bootstrap_volume_from_benchmark(
                edition_id=edition.edition_id,
                grade=grade,
                term=term_key,
                replace=bool(replace and existing_count),
            )
            created.append(
                {
                    "volume_code": result["volume_code"],
                    "grade": grade,
                    "term": term_key,
                    "lessons_created": result["lessons_created"],
                }
            )
        except Exception as exc:
            db.session.rollback()
            errors.append(
                {
                    "volume_code": volume_code,
                    "grade": grade,
                    "term": term_key,
                    "error": str(exc),
                }
            )

    return {
        "edition_id": edition.edition_id,
        "edition_label": edition.label,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "empty_count": len(empty),
        "error_count": len(errors),
        "lessons_created_total": sum(int(x.get("lessons_created") or 0) for x in created),
        "created": created,
        "skipped": skipped,
        "empty": empty,
        "errors": errors,
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
            f"基准库中未找到课时：{edition.label} {grade}年级 {normalize_term(term)}册"
        )

    volume_code = make_volume_code(edition, grade=grade, term=term)
    term_key = normalize_term(term)
    book_type = "old"

    volume = Volume.query.filter_by(volume_code=volume_code).first()
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
            Lesson.query.filter_by(volume_id=volume.id).delete()
            db.session.flush()

    created = 0
    for row, unit_no in _assign_unit_numbers(rows):
        lesson_no = row.lesson_no or "0"
        lesson_name = (row.lesson_name or row.lesson_raw or "未命名").strip()
        lesson_name = lesson_name.lstrip(".．、").strip() or lesson_name
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

    db.session.commit()
    return {
        "volume_id": volume.id,
        "volume_code": volume.volume_code,
        "display_title": volume.display_title,
        "lessons_created": created,
        "replace": replace,
    }


def _bench_match_key(*, lesson_no: str, lesson_name: str) -> tuple[str, str]:
    return ((lesson_no or "").strip(), (lesson_name or "").strip())


def sync_volume_metadata_from_benchmark(
    *,
    edition_id: str,
    grade: int,
    term: str,
) -> dict:
    """从基准 xlsx 同步单元名/课件 id/页数（不删课时、不动 PDF/建块）。"""
    edition, rows = load_volume_lesson_rows(edition_id=edition_id, grade=grade, term=term)
    if not rows:
        raise ValueError(
            f"基准库中未找到课时：{edition.label} {grade}年级 {normalize_term(term)}册"
        )

    volume_code = make_volume_code(edition, grade=grade, term=term)
    volume = Volume.query.filter_by(volume_code=volume_code).first()
    if not volume:
        raise ValueError(f"册次 {volume_code} 尚未载入，请先在旧库建设载入基准目录")

    by_key: dict[tuple[str, str], tuple[BenchmarkLessonRow, int]] = {}
    by_name: dict[str, tuple[BenchmarkLessonRow, int]] = {}
    for row, unit_no in _assign_unit_numbers(rows):
        key = _bench_match_key(lesson_no=row.lesson_no or "", lesson_name=row.lesson_name or "")
        by_key[key] = (row, unit_no)
        name = key[1]
        if name and name not in by_name:
            by_name[name] = (row, unit_no)

    from ....query.lesson_order import order_lessons_query

    lessons = order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    updated = 0
    for les in lessons:
        key = _bench_match_key(lesson_no=les.lesson_no or "", lesson_name=les.lesson_name or "")
        hit = by_key.get(key) or by_name.get(key[1])
        if not hit:
            continue
        row, unit_no = hit
        unit_title = (row.unit_title or "").strip() or "未命名单元"
        changed = False
        if les.unit_title != unit_title:
            les.unit_title = unit_title
            changed = True
        if row.old_course_id and les.old_course_id != row.old_course_id:
            les.old_course_id = row.old_course_id
            changed = True
        if row.page_count is not None and les.page_count != row.page_count:
            les.page_count = row.page_count
            changed = True
        if changed:
            updated += 1

    db.session.commit()
    return {
        "volume_id": volume.id,
        "volume_code": volume.volume_code,
        "display_title": volume.display_title,
        "lesson_count": len(lessons),
        "lessons_updated": updated,
    }
