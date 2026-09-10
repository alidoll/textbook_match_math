"""新教材人工校正页码（不触发整册 OCR / 默认不批量生成页图）。"""
from __future__ import annotations

from ...old_library.pdf.lesson_page_range import update_lesson_page_range as _update_lesson_page_range


def update_lesson_page_range(
    *,
    lesson_uid: str,
    page_start: int,
    page_end: int,
    rebuild_pages: bool = False,
    render_dpi: int = 96,
) -> dict:
    return _update_lesson_page_range(
        lesson_uid=lesson_uid,
        page_start=page_start,
        page_end=page_end,
        rebuild_pages=rebuild_pages,
        render_dpi=render_dpi,
        skip_body_text=True,
    )
