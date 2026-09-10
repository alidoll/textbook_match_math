# -*- coding: utf-8 -*-
"""教材库册级预处理 facade：目录 → 划页 → 页图。"""
from __future__ import annotations

import threading
from typing import Any

from flask import current_app

from ...extensions import db
from ...models import Lesson, LessonPage, Volume
from ...query.lesson_order import order_lessons_query
from ..old_library.volumes import get_old_volume_by_code


def get_library_volume(volume_code: str) -> Volume:
    return get_old_volume_by_code(volume_code)


def _lesson_page_stats(volume: Volume) -> dict[str, int]:
    lessons = order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    lesson_count = len(lessons)
    with_range = sum(
        1
        for les in lessons
        if les.page_start and les.page_end and les.page_end >= les.page_start
    )
    with_images = (
        db.session.query(LessonPage.id)
        .join(Lesson, Lesson.id == LessonPage.lesson_id)
        .filter(Lesson.volume_id == volume.id, LessonPage.blob_id.isnot(None))
        .count()
    )
    return {
        "lesson_count": lesson_count,
        "with_range": with_range,
        "with_images": int(with_images),
    }


def library_preprocess_status(volume_code: str) -> dict[str, Any]:
    volume = get_library_volume(volume_code)
    stats = _lesson_page_stats(volume)
    has_pdf = bool(volume.blob_id)
    lesson_count = stats["lesson_count"]
    catalog_ready = lesson_count > 0
    page_range_ready = lesson_count > 0 and stats["with_range"] >= lesson_count
    pages_ready = stats["with_images"] > 0
    return {
        "ok": True,
        "volume_code": volume.volume_code,
        "has_pdf": has_pdf,
        "lesson_count": lesson_count,
        "lessons_with_page_range": stats["with_range"],
        "page_image_count": stats["with_images"],
        "catalog_ready": catalog_ready,
        "page_range_ready": page_range_ready,
        "pages_ready": pages_ready,
        "parse_status": volume.parse_status or "pending",
        "parse_error": volume.parse_error,
    }


def run_library_catalog(*, volume_code: str, replace: bool = False) -> dict[str, Any]:
    volume = get_library_volume(volume_code)
    if not volume.blob_id:
        raise ValueError("请先上传 PDF")
    from ..textbook_diff.catalog import bootstrap_diff_catalog

    return bootstrap_diff_catalog(volume_code=volume_code, replace=replace)


def library_volume_detail(volume_code: str) -> dict[str, Any]:
    """册次详情：diff 预处理字段 + 教材库 slot 导航字段。"""
    from ..textbook_diff.volumes import diff_volume_detail
    from .codes import parse_library_volume_code

    volume = get_library_volume(volume_code)
    out = diff_volume_detail(volume)
    try:
        ed, _, _, _, _ = parse_library_volume_code(volume_code)
        out["edition_id"] = ed.edition_id
        out["has_old_benchmark"] = bool(ed.has_old_benchmark)
    except ValueError:
        out.setdefault("edition_id", None)
        out.setdefault("has_old_benchmark", False)
    out["term"] = volume.semester
    return out


def _start_parse_in_background(volume_code: str, **kwargs: Any) -> None:
    app = current_app._get_current_object()

    def worker() -> None:
        with app.app_context():
            from ..textbook_diff.parse import parse_diff_volume_pdf

            parse_diff_volume_pdf(volume_code=volume_code, **kwargs)

    threading.Thread(
        target=worker, daemon=True, name=f"library-parse-{volume_code}"
    ).start()


def start_library_parse(
    *,
    volume_code: str,
    replace_lessons: bool = False,
    force_recalibrate: bool = False,
) -> dict[str, Any]:
    """异步划分页码，对齐 textbook_diff intake parse 行为。"""
    volume = get_library_volume(volume_code)
    if volume.parse_status == "processing" and not force_recalibrate:
        return {
            "ok": True,
            "parse_status": "processing",
            "volume_code": volume_code,
            "message": "划分页码仍在后台进行，请稍候…",
            "http_status": 202,
        }

    volume.parse_status = "processing"
    volume.parse_error = None
    db.session.commit()
    _start_parse_in_background(
        volume_code,
        replace_lessons=replace_lessons,
        force_recalibrate=force_recalibrate,
    )
    return {
        "ok": True,
        "parse_status": "processing",
        "volume_code": volume_code,
        "message": "划分页码已在后台开始",
        "http_status": 202,
    }


def rebuild_library_lesson_pages(*, volume_code: str) -> dict[str, Any]:
    """生成页图：将已有页码的课时渲染为 PNG 存入 lesson_pages + file_blobs。"""
    from ..old_library.pdf.lesson_pages_build import build_lesson_pages_from_parse
    from ..textbook_diff.catalog import _pdf_path_for_volume
    from ..textbook_diff.volumes import diff_volume_detail

    volume = get_library_volume(volume_code)
    if not volume.blob_id:
        raise ValueError("请先上传 PDF")

    lessons = order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    row_updates = [
        {"lesson_id": les.id, "page_start": les.page_start, "page_end": les.page_end}
        for les in lessons
        if les.page_start and les.page_end and les.page_end >= les.page_start
    ]
    if not row_updates:
        raise ValueError(
            "尚无页码数据，请先点「划分页码」（识别目录后若重导过目录也需再划分一次）"
        )

    pdf_path = _pdf_path_for_volume(volume)
    written = build_lesson_pages_from_parse(
        volume=volume,
        pdf_path=pdf_path,
        row_updates=row_updates,
        render_dpi=120,
    )
    db.session.commit()

    result = diff_volume_detail(volume)
    result.update(
        {
            "ok": True,
            "lesson_pages_written": written,
            "message": f"已生成 {written} 张教材页图",
        }
    )
    return result
