"""新教材：按已有页码重生成页图。"""
from __future__ import annotations

from typing import Any

from ....extensions import db
from ....models import Lesson
from ....query.lesson_order import order_lessons_query
from ..volumes import get_new_volume_by_code, volume_detail_dict
from ...old_library.pdf.lesson_pages_build import build_lesson_pages_from_parse
from .parse import _pdf_path_for_volume


def rebuild_lesson_pages_for_volume(
    *,
    volume_code: str,
    render_dpi: int = 120,
) -> dict:
    volume = get_new_volume_by_code(volume_code)
    if not volume.blob_id:
        raise ValueError("请先上传 PDF")

    lessons = order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    row_updates: list[dict[str, Any]] = []
    for les in lessons:
        if les.page_start and les.page_end and les.page_end >= les.page_start:
            row_updates.append(
                {
                    "lesson_id": les.id,
                    "page_start": les.page_start,
                    "page_end": les.page_end,
                }
            )

    if not row_updates:
        raise ValueError("尚无页码数据，请先解析 PDF")

    pdf_path = _pdf_path_for_volume(volume)
    written = build_lesson_pages_from_parse(
        volume=volume,
        pdf_path=pdf_path,
        row_updates=row_updates,
        render_dpi=render_dpi,
    )
    db.session.commit()

    result = volume_detail_dict(volume)
    result.update(
        {
            "ok": True,
            "lesson_pages_written": written,
            "message": f"已生成 {written} 张教材页图",
        }
    )
    return result
