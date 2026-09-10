"""新教材册次查询与详情。"""
from __future__ import annotations

from sqlalchemy import desc

from ...models import Block, FileBlob, Lesson, LessonPage, MatchJob, Volume
from ...parsers.pdf_spread import page_layout_label, stored_page_layout
from ...query.lesson_order import order_lessons_query
from ...repo_paths import textbook_mirror_path
from ..lesson_filters import filter_master_class_lessons
from ..old_library.edition_registry import resolve_edition_id
from .lesson_analysis import get_analysis_batch_job, lesson_analysis_map, summarize_analysis
from .block_pipeline_run import get_block_batch_job, lesson_block_pipeline_dict


def _anchored_block_count(lesson_id: str) -> int:
    blocks = Block.query.filter_by(lesson_id=lesson_id).all()
    return sum(
        1 for block in blocks if (block.metadata_json or {}).get("anchor_old_refs")
    )


def get_new_volume_by_code(volume_code: str) -> Volume:
    code = (volume_code or "").strip()
    if not code:
        raise ValueError("缺少 volume_code")
    volume = Volume.query.filter_by(volume_code=code, book_type="new").first()
    if not volume:
        raise ValueError(f"未找到新教材册次：{code}")
    return volume


def _course_match_summary_for_volume(volume: Volume) -> dict | None:
    job = (
        MatchJob.query.filter_by(new_volume_id=volume.id)
        .order_by(desc(MatchJob.created_at))
        .first()
    )
    if not job:
        return None
    return {
        "job_id": job.id,
        "status": job.status,
        "summary": job.summary_json,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def compute_new_volume_pipeline(
    volume: Volume,
    lesson_rows: list[dict],
    *,
    course_match: dict | None = None,
) -> dict:
    """新教材建设五步：PDF 上传 → 识别目录 → 页码划分 → 粗分对照 → 建块。"""
    n = len(lesson_rows)
    has_pdf = bool(volume.blob_id)

    with_pages = sum(
        1 for row in lesson_rows if row.get("page_start") and row.get("page_end")
    )
    with_blocks = sum(1 for row in lesson_rows if (row.get("block_count") or 0) > 0)

    cm_status = (course_match or {}).get("status")
    cm_summary = (course_match or {}).get("summary") or {}
    coarse_matched = int(cm_summary.get("exact_match") or 0) + int(
        cm_summary.get("high_similarity") or 0
    )
    coarse_total = n

    catalog_done = has_pdf and n > 0
    parse_done = volume.parse_status == "done"
    split_done = has_pdf and parse_done and n > 0 and with_pages >= n
    coarse_done = cm_status == "done" and n > 0
    blocks_done = n > 0 and with_blocks >= n

    def progress(done: bool, current: int, total: int, unit: str = "课") -> str | None:
        if done or total <= 0:
            return None
        return f"{current}/{total} {unit}"

    def step_ratio(done: bool, current: int, total: int) -> float:
        if done:
            return 1.0
        if total <= 0:
            return 0.0
        return min(1.0, current / total)

    r_upload = 1.0 if has_pdf else 0.0
    r_catalog = step_ratio(catalog_done, n if catalog_done else 0, max(n, 1))
    r_split = step_ratio(split_done, with_pages, n)
    r_coarse = step_ratio(coarse_done, coarse_matched, coarse_total)
    r_blocks = step_ratio(blocks_done, with_blocks, n)
    ratios = [r_upload, r_catalog, r_split, r_coarse, r_blocks]

    steps = [
        {
            "key": "pdf_upload",
            "label": "新教材 PDF 上传",
            "done": has_pdf,
            "ratio": r_upload,
            "current": 1 if has_pdf else 0,
            "total": 1,
            "progress": None,
            "busy": False,
        },
        {
            "key": "catalog_extract",
            "label": "识别目录",
            "done": catalog_done,
            "ratio": r_catalog,
            "current": n if catalog_done else 0,
            "total": max(n, 1),
            "progress": progress(catalog_done, n, n) if catalog_done else None,
            "busy": False,
        },
        {
            "key": "lesson_split",
            "label": "页码划分",
            "done": split_done,
            "ratio": r_split,
            "current": with_pages,
            "total": n,
            "progress": progress(split_done, with_pages, n),
            "busy": volume.parse_status == "processing",
        },
        {
            "key": "coarse_match",
            "label": "旧库粗分对照",
            "done": coarse_done,
            "ratio": r_coarse,
            "current": coarse_matched,
            "total": coarse_total,
            "progress": progress(coarse_done, coarse_matched, coarse_total),
            "busy": cm_status == "running",
        },
        {
            "key": "block_build",
            "label": "新区块建立",
            "done": blocks_done,
            "ratio": r_blocks,
            "current": with_blocks,
            "total": n,
            "progress": progress(blocks_done, with_blocks, n),
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


def _lesson_rows_for_volume(volume: Volume) -> list[dict]:
    lessons = filter_master_class_lessons(
        order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    )
    return [
        {
            "lesson_uid": les.lesson_uid,
            "page_start": les.page_start,
            "page_end": les.page_end,
            "block_count": Block.query.filter_by(lesson_id=les.id).count(),
        }
        for les in lessons
    ]


def volume_workbench_pipeline(volume: Volume) -> dict:
    """入口页册卡进度（与 intake pipeline 同源）。"""
    lesson_rows = _lesson_rows_for_volume(volume)
    course_match = _course_match_summary_for_volume(volume)
    return compute_new_volume_pipeline(
        volume, lesson_rows, course_match=course_match
    )


def volume_detail_dict(volume: Volume) -> dict:
    from ..parse_status_heal import heal_stuck_parse_status

    heal_stuck_parse_status(volume)
    lessons = filter_master_class_lessons(
        order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    )
    blob = volume.blob_id
    blob_size = None
    blob_storage = None
    if blob:
        row = FileBlob.query.get(blob)
        if row:
            blob_size = row.size_bytes
            blob_storage = "disk" if row.storage_path else "mysql"
    mirror = (
        textbook_mirror_path(
            book_type=volume.book_type,
            edition_label=volume.edition,
            volume_code=volume.volume_code,
        )
        if blob
        else None
    )
    verified_count = sum(1 for les in lessons if les.page_range_verified)
    analysis_by_uid = lesson_analysis_map(lessons, volume_code=volume.volume_code)
    lesson_rows = [
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
            "lesson_page_count": LessonPage.query.filter_by(lesson_id=les.id).count(),
            "block_count": Block.query.filter_by(lesson_id=les.id).count(),
            "anchored_block_count": _anchored_block_count(les.id),
            "analysis": analysis_by_uid.get(les.lesson_uid),
            "block_pipeline": lesson_block_pipeline_dict(
                lesson_id=les.id, lesson_uid=les.lesson_uid
            ),
        }
        for les in lessons
    ]
    course_match = _course_match_summary_for_volume(volume)
    layout = stored_page_layout(volume)
    return {
        "volume_id": volume.id,
        "volume_code": volume.volume_code,
        "display_title": volume.display_title,
        "subject": volume.subject,
        "edition": volume.edition,
        "edition_id": resolve_edition_id(volume.edition),
        "grade": volume.grade,
        "semester": volume.semester,
        "book_type": volume.book_type,
        "page_layout": layout,
        "page_layout_label": page_layout_label(layout) if layout else None,
        "parse_status": volume.parse_status,
        "parse_error": volume.parse_error,
        "has_pdf": bool(blob),
        "blob_id": blob,
        "blob_size_bytes": blob_size,
        "blob_storage": blob_storage if blob else None,
        "mirror_path": str(mirror) if mirror and mirror.is_file() else None,
        "lesson_count": len(lessons),
        "verified_lesson_count": verified_count,
        "lessons": lesson_rows,
        "course_match": course_match,
        "analysis_summary": summarize_analysis(analysis_by_uid),
        "analysis_batch": get_analysis_batch_job(volume.volume_code),
        "block_batch": get_block_batch_job(volume.volume_code),
        "pipeline": volume_workbench_pipeline(volume),
    }
