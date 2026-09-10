"""教材对比划分页码：OCR 精修回退（扫描版 / 校准失败）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...models import Lesson
from ...parsers.catalog_page_offset import (
    get_cached_catalog_pdf_offset,
    offset_toc_entries_to_pdf,
)
from ...parsers.llm_toc_cache import get_llm_toc_entries
from ...parsers.pdf_pages import (
    LazyPageTexts,
    PdfOcrCache,
    compute_page_ends,
    refine_lesson_starts_near_hints,
    trim_backmatter_pages,
    trim_leading_unit_summary_pages,
    trim_trailing_blank_pages,
    trim_trailing_blank_pages_from_pdf,
    trim_unit_boundary_pages,
    trim_unit_summary_pages,
    trim_unit_summary_pages_from_pdf,
)
from ...parsers.pdf_toc import pick_toc_page_hint
from ...services.lesson_filters import is_unit_summary_lesson
from ...services.old_library.pdf.lesson_targets import build_lesson_targets

_log = logging.getLogger(__name__)

_DEFAULT_OCR_DPI = 96


def _offset_warning_label(pdf_path: Path, *, content_hash: str | None = None) -> str:
    off = get_cached_catalog_pdf_offset(pdf_path, content_hash=content_hash)
    if off is None:
        return "目录页码已按版别经验值转换"
    if off.offset_first == off.offset_default:
        return f"目录锚点校准：全书 +{off.offset_first}（{off.source}）"
    return (
        f"目录锚点校准：首课 +{off.offset_first}，其余 +{off.offset_default}（{off.source}）"
    )


def try_ocr_refine_diff_pages(
    *,
    lessons: list[Lesson],
    pdf_path: Path,
    content_hash: str | None,
    layout: str,
    edition_label: str | None,
    ocr_dpi: int = _DEFAULT_OCR_DPI,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    """
    OCR 精修回退：LLM 目录锚点 → 偏移到 PDF → 课名附近 OCR 定位起始页并修剪边界。

    返回与 ``compute_diff_lesson_pages`` 相同的 ``(meta, row_updates, match_details)``；
    无法精修时返回 None（``layout != "single"`` 暂不映射 view，由 page_plan 现有 spread OCR 承接）。
    """
    entries = get_llm_toc_entries(pdf_path, content_hash=content_hash)
    if not entries:
        return None

    if layout != "single":
        _log.warning(
            "OCR refine 暂未支持 layout=%s 的 view 映射，跳过：%s",
            layout,
            pdf_path.name,
        )
        return None

    import fitz

    with fitz.open(str(pdf_path)) as doc:
        total_pages = len(doc)

    targets = build_lesson_targets(lessons)
    work_indices = [
        i for i, les in enumerate(lessons) if not is_unit_summary_lesson(les)
    ]
    lessons_work = [lessons[i] for i in work_indices]
    targets_work = [targets[i] for i in work_indices]
    if not lessons_work:
        return None

    entries_pdf = offset_toc_entries_to_pdf(
        entries,
        pdf_path=pdf_path,
        lessons=lessons,
        targets=targets,
        edition_label=edition_label,
        ocr_dpi=ocr_dpi,
        content_hash=content_hash,
    )

    hints: list[int | None] = []
    for les in lessons_work:
        hints.append(
            pick_toc_page_hint(
                entries_pdf,
                les.unit_title or "",
                les.lesson_name or "",
                str(les.lesson_no or ""),
            )
        )
    hint_count = sum(1 for h in hints if h is not None)
    if hint_count < max(3, int(len(lessons_work) * 0.75)) or not all(
        h is not None for h in hints
    ):
        _log.info(
            "diff OCR refine：LLM 页码提示不足（%d/%d），无法精修",
            hint_count,
            len(lessons_work),
        )
        return None

    hints_1based = [int(h) for h in hints]
    starts_0 = [h - 1 for h in hints_1based]

    ocr_cache = PdfOcrCache(pdf_path, dpi=min(ocr_dpi, 96))
    pages_text = LazyPageTexts(ocr_cache)
    try:
        starts_0 = refine_lesson_starts_near_hints(
            pages_text, targets_work, hints_1based
        )
        ends_1 = compute_page_ends(starts_0, total_pages)
        starts_0 = trim_leading_unit_summary_pages(
            pages_text, targets_work, starts_0, ends_1
        )
        starts_0, _ = trim_unit_boundary_pages(
            pages_text,
            targets_work,
            starts_0,
            ends_1,
            trim_trailing=False,
        )
        ends_1 = compute_page_ends(starts_0, total_pages)
        _, ends_1 = trim_unit_boundary_pages(
            pages_text,
            targets_work,
            starts_0,
            ends_1,
            trim_leading=False,
        )
        ends_1 = trim_unit_summary_pages(
            pages_text, targets_work, starts_0, ends_1
        )
        ends_1 = trim_backmatter_pages(pages_text, starts_0, ends_1)
        ends_1 = trim_trailing_blank_pages(
            pages_text, starts_0, ends_1, pdf_path=pdf_path
        )
    finally:
        ocr_cache.close()

    ends_1 = trim_unit_summary_pages_from_pdf(
        pdf_path, targets_work, starts_0, ends_1, ocr_dpi=min(ocr_dpi, 96)
    )
    ends_1 = trim_trailing_blank_pages_from_pdf(pdf_path, starts_0, ends_1)

    starts_full: list[int | None] = [None] * len(lessons)
    ends_full: list[int | None] = [None] * len(lessons)
    for j, orig_i in enumerate(work_indices):
        starts_full[orig_i] = starts_0[j]
        ends_full[orig_i] = ends_1[j]

    warnings = [
        f"OCR 精修：{_offset_warning_label(pdf_path, content_hash=content_hash)} + 课名 OCR 定位起始页，"
        "并修剪单元扉页/小结",
    ]
    match_details: list[dict[str, Any]] = []
    row_updates: list[dict[str, Any]] = []
    matched = 0

    for les, start_0, end_1 in zip(lessons, starts_full, ends_full, strict=True):
        detail: dict[str, Any] = {
            "lesson_uid": les.lesson_uid,
            "lesson_name": les.lesson_name,
            "page_start": None,
            "page_end": None,
            "matched": False,
        }
        if is_unit_summary_lesson(les):
            match_details.append(detail)
            continue
        if start_0 is not None and end_1 is not None:
            page_start = start_0 + 1
            page_end = end_1
            if page_end >= page_start:
                row_updates.append(
                    {
                        "lesson_id": les.id,
                        "page_start": page_start,
                        "page_end": page_end,
                        "body_text": None,
                    }
                )
                matched += 1
                detail.update(
                    {"page_start": page_start, "page_end": page_end, "matched": True}
                )
        match_details.append(detail)

    meta = {
        "matched": matched,
        "lesson_count": len(lessons),
        "text_mode": "diff_ocr_refine",
        "total_pages": total_pages,
        "page_layout": layout,
        "catalog_lines": len(entries),
        "warnings": warnings,
    }
    _log.info(
        "教材对比 %s OCR 精修：匹配 %d/%d 课",
        pdf_path.name,
        matched,
        len(lessons),
    )
    return meta, row_updates, match_details
