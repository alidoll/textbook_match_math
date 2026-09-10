"""人工校正课时 PDF 页码范围。"""
from __future__ import annotations

from pathlib import Path

from ....extensions import db
from ....models import Lesson, Volume
from ....parsers.pdf_pages import (
    body_text_for_range,
    extract_all_page_texts,
    trim_trailing_blank_end_from_pdf,
)
from ..volumes import volume_detail_dict
from .lesson_pages_build import build_lesson_pages_from_parse
from .parse import _pdf_path_for_volume


def _volume_detail_for_page_range(volume: Volume) -> dict:
    bt = (volume.book_type or "").strip().lower()
    if bt in ("diff_old", "diff_new"):
        from ...textbook_diff.volumes import diff_volume_detail

        return diff_volume_detail(volume)
    if bt == "new":
        from ...new_library.volumes import volume_detail_dict as new_volume_detail_dict

        return new_volume_detail_dict(volume)
    return volume_detail_dict(volume)


def rebuild_lesson_pages_for_lesson(
    *,
    lesson: Lesson,
    volume: Volume,
    pdf_path: Path,
    render_dpi: int = 120,
) -> int:
    if not lesson.page_start or not lesson.page_end or lesson.page_end < lesson.page_start:
        from .lesson_pages_build import _delete_lesson_pages_for_lessons

        _delete_lesson_pages_for_lessons([lesson.id])
        return 0
    return build_lesson_pages_from_parse(
        volume=volume,
        pdf_path=pdf_path,
        row_updates=[
            {
                "lesson_id": lesson.id,
                "page_start": lesson.page_start,
                "page_end": lesson.page_end,
            }
        ],
        render_dpi=render_dpi,
    )


def update_lesson_page_range(
    *,
    lesson_uid: str,
    page_start: int,
    page_end: int,
    rebuild_pages: bool = True,
    render_dpi: int = 120,
    skip_body_text: bool = False,
) -> dict:
    les = Lesson.query.filter_by(lesson_uid=lesson_uid).first()
    if not les:
        raise ValueError("未找到课时")
    if page_start < 1 or page_end < page_start:
        raise ValueError(f"页码无效：{page_start}–{page_end}")

    volume = Volume.query.get(les.volume_id)
    if not volume:
        raise ValueError("册次不存在")
    if not volume.blob_id:
        raise ValueError("请先上传 PDF")

    pdf_path = _pdf_path_for_volume(volume)
    page_end = trim_trailing_blank_end_from_pdf(pdf_path, page_start, page_end)

    les.page_start = page_start
    les.page_end = page_end
    les.page_range_verified = True
    if not skip_body_text:
        pages_text, _ = extract_all_page_texts(pdf_path, ocr_dpi=render_dpi)
        les.body_text = body_text_for_range(pages_text, page_start, page_end)

    pages_written = 0
    if rebuild_pages:
        pages_written = rebuild_lesson_pages_for_lesson(
            lesson=les,
            volume=volume,
            pdf_path=pdf_path,
            render_dpi=render_dpi,
        )
    else:
        from .lesson_pages_build import _delete_lesson_pages_for_lessons

        _delete_lesson_pages_for_lessons([les.id])

    db.session.commit()
    detail = _volume_detail_for_page_range(volume)
    lesson_row = next(
        (x for x in detail["lessons"] if x["lesson_uid"] == lesson_uid),
        None,
    )
    return {
        "ok": True,
        "lesson_uid": lesson_uid,
        "page_start": page_start,
        "page_end": page_end,
        "page_range_verified": True,
        "lesson_page_count": pages_written,
        "lesson": lesson_row,
        "message": f"已保存页码 {page_start}–{page_end}，生成 {pages_written} 张页图",
    }
