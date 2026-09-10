"""旧库册次查询。"""
from __future__ import annotations

from ...models import Block, CoursewareSlide, FileBlob, Lesson, LessonPage, TextbookAtom, Volume
from ...query.lesson_order import order_lessons_query
from ...repo_paths import old_textbook_mirror_path
from .edition_registry import EDITIONS, get_edition, resolve_edition_id


def edition_meta_for_volume(volume: Volume) -> tuple[str | None, str | None, bool]:
    """Resolve (edition_id, subject, has_old_benchmark).

    Prefer registry id / volume_code prefix / (label + subject) — plain「人教版」
    alone is ambiguous across 科学/化学.
    """
    eid = resolve_edition_id(volume.edition)
    if eid:
        ed = get_edition(eid)
        return ed.edition_id, ed.subject, bool(ed.has_old_benchmark)

    code = (volume.volume_code or "").strip()
    if code:
        try:
            from ..textbook_library.codes import parse_library_volume_code

            ed, *_rest = parse_library_volume_code(code)
            return ed.edition_id, ed.subject, bool(ed.has_old_benchmark)
        except ValueError:
            pass

    label = (volume.edition or "").strip()
    subject = (volume.subject or "").strip()
    matches = [ed for ed in EDITIONS.values() if ed.label == label]
    if subject:
        narrowed = [ed for ed in matches if ed.subject == subject]
        if narrowed:
            matches = narrowed
    if len(matches) == 1:
        ed = matches[0]
        return ed.edition_id, ed.subject, bool(ed.has_old_benchmark)
    return None, subject or None, False


def get_old_volume_by_code(volume_code: str) -> Volume:
    code = (volume_code or "").strip()
    if not code:
        raise ValueError("缺少 volume_code")
    volume = Volume.query.filter_by(volume_code=code, book_type="old").first()
    if not volume:
        raise ValueError(f"未找到旧库册次：{code}")
    return volume


def compute_volume_pipeline(volume: Volume, lesson_rows: list[dict]) -> dict:
    """本册建设四步进度：整本上传 → 课次拆分 → 课件上传 → 区块建立。"""
    n = len(lesson_rows)
    has_pdf = bool(volume.blob_id)

    with_pages = sum(
        1 for row in lesson_rows if row.get("page_start") and row.get("page_end")
    )
    with_slides = sum(1 for row in lesson_rows if (row.get("slide_count") or 0) > 0)
    with_blocks_on_slides = sum(
        1
        for row in lesson_rows
        if (row.get("slide_count") or 0) > 0 and (row.get("block_count") or 0) > 0
    )
    with_blocks_locked = sum(
        1
        for row in lesson_rows
        if row.get("blocks_locked") and (row.get("block_count") or 0) > 0
    )

    parse_done = volume.parse_status == "done"
    split_done = has_pdf and parse_done and n > 0 and with_pages >= n
    courseware_done = n > 0 and with_slides >= n
    blocks_done = (
        courseware_done
        and with_slides > 0
        and with_blocks_on_slides >= with_slides
    )

    def progress(done: bool, current: int, total: int, unit: str = "课") -> str | None:
        if done or total <= 0:
            return None
        return f"{current}/{total} {unit}"

    def block_build_progress(done: bool) -> str | None:
        if done or block_total <= 0:
            return None
        return (
            f"{with_blocks_on_slides}/{block_total} 已建块 · "
            f"{with_blocks_locked}/{block_total} 已保存"
        )

    def step_ratio(done: bool, current: int, total: int) -> float:
        if done:
            return 1.0
        if total <= 0:
            return 0.0
        return min(1.0, current / total)

    r_upload = 1.0 if has_pdf else 0.0
    r_split = step_ratio(split_done, with_pages, n)
    r_courseware = step_ratio(courseware_done, with_slides, n)
    block_total = with_slides if with_slides else n
    r_blocks = step_ratio(blocks_done, with_blocks_on_slides, block_total)
    ratios = [r_upload, r_split, r_courseware, r_blocks]

    steps = [
        {
            "key": "pdf_upload",
            "label": "旧教材整本上传",
            "done": has_pdf,
            "ratio": r_upload,
            "current": 1 if has_pdf else 0,
            "total": 1,
            "progress": None,
            "busy": False,
        },
        {
            "key": "lesson_split",
            "label": "旧教材课次拆分",
            "done": split_done,
            "ratio": r_split,
            "current": with_pages,
            "total": n,
            "progress": progress(split_done, with_pages, n),
            "busy": volume.parse_status == "processing",
        },
        {
            "key": "courseware_upload",
            "label": "课时旧课件上传",
            "done": courseware_done,
            "ratio": r_courseware,
            "current": with_slides,
            "total": n,
            "progress": progress(courseware_done, with_slides, n),
            "busy": False,
        },
        {
            "key": "block_build",
            "label": "旧区块建立",
            "done": blocks_done,
            "ratio": r_blocks,
            "current": with_blocks_on_slides,
            "total": block_total,
            "progress": block_build_progress(blocks_done),
            "blocks_locked_count": with_blocks_locked,
            "busy": False,
        },
    ]

    active_index = len(steps)
    for i, step in enumerate(steps):
        if not step["done"]:
            active_index = i
            break

    return {
        "steps": steps,
        "active_index": active_index,
        "all_done": all(step["done"] for step in steps),
        "overall_ratio": sum(ratios) / len(ratios) if ratios else 0.0,
    }


def volume_detail_dict(volume: Volume) -> dict:
    lessons = order_lessons_query(
        Lesson.query.filter_by(volume_id=volume.id)
    ).all()
    blob = volume.blob_id
    blob_size = None
    blob_storage = None
    if blob:
        row = FileBlob.query.get(blob)
        if row:
            blob_size = row.size_bytes
            blob_storage = "disk" if row.storage_path else "mysql"
    mirror = (
        old_textbook_mirror_path(volume.edition, volume.volume_code)
        if blob
        else None
    )
    verified_count = sum(1 for les in lessons if les.page_range_verified)
    edition_id, subject, has_old_benchmark = edition_meta_for_volume(volume)
    from .annotate.ai_bootstrap import (
        assess_lesson_bootstrap_readiness,
        compute_lesson_build_pipeline,
    )

    lesson_rows = []
    for les in lessons:
        slide_count = CoursewareSlide.query.filter_by(lesson_id=les.id).count()
        block_count = Block.query.filter_by(lesson_id=les.id).count()
        lesson_page_count = LessonPage.query.filter_by(lesson_id=les.id).count()
        atom_count = TextbookAtom.query.filter_by(lesson_id=les.id).count()
        blocks_locked = bool(les.blocks_locked_at)
        readiness = assess_lesson_bootstrap_readiness(
            lesson_uid=les.lesson_uid,
            book_type="old",
        )
        lesson_rows.append(
            {
                "lesson_uid": les.lesson_uid,
                "unit_no": les.unit_no,
                "unit_title": les.unit_title,
                "lesson_no": les.lesson_no,
                "lesson_name": les.lesson_name,
                "old_course_id": les.old_course_id,
                "page_start": les.page_start,
                "page_end": les.page_end,
                "page_range_verified": bool(les.page_range_verified),
                "page_count": les.page_count,
                "lesson_page_count": lesson_page_count,
                "slide_count": slide_count,
                "atom_count": atom_count,
                "block_count": block_count,
                "blocks_locked": blocks_locked,
                "blocks_locked_at": (
                    les.blocks_locked_at.isoformat(timespec="seconds")
                    if les.blocks_locked_at
                    else None
                ),
                "bootstrap_mode": readiness["recommended_mode"],
                "build_pipeline": compute_lesson_build_pipeline(
                    readiness=readiness,
                    block_count=block_count,
                    blocks_locked=blocks_locked,
                    slide_count=slide_count,
                    lesson_page_count=lesson_page_count,
                    atom_count=atom_count,
                    lesson_id=les.id,
                ),
                "slides_fetch_status": les.slides_fetch_status,
            }
        )
    return {
        "volume_id": volume.id,
        "volume_code": volume.volume_code,
        "display_title": volume.display_title,
        "edition": volume.edition,
        "edition_id": edition_id,
        "subject": subject,
        "has_old_benchmark": has_old_benchmark,
        "grade": volume.grade,
        "semester": volume.semester,
        "term": volume.semester,
        "book_type": volume.book_type,
        "parse_status": volume.parse_status,
        "parse_error": volume.parse_error,
        "has_pdf": bool(blob),
        "blob_id": blob,
        "blob_size_bytes": blob_size,
        "blob_storage": blob_storage if blob else None,
        "mirror_path": str(mirror) if mirror and mirror.is_file() else None,
        "lesson_count": len(lessons),
        "verified_lesson_count": verified_count,
        "pipeline": compute_volume_pipeline(volume, lesson_rows),
        "lessons": lesson_rows,
    }
