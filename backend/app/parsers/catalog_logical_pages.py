"""目录逻辑页码 → 统一偏移 x → PDF 物理页：物理 = 逻辑 + x，止 = 下一课逻辑 + x − 1。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..models import Lesson
from ..services.lesson_filters import is_unit_summary_lesson
from .catalog_page_offset import (
    CatalogPdfOffset,
    calibrate_catalog_pdf_offset,
    clear_cached_catalog_pdf_offset,
    get_cached_catalog_pdf_offset,
    put_cached_catalog_pdf_offset,
)
from .pdf_toc import TocEntry, compute_page_ends_from_toc_entries, pick_toc_page_hint

_log = logging.getLogger(__name__)

MISSING_CATALOG_OFFSET_MSG = (
    "无法推算目录偏移 x：本册尚无偏移缓存，且 PDF 锚点 OCR 校准未成功（不会用固定 +2 猜测）。"
    "请按顺序：① 确认已点「识别目录」且课时表有数据；"
    "② 关闭 debug 热重载、只开一个后端，再点「划分页码」并等待约 5 分钟完成首次校准；"
    "③ 若仍失败，请重新上传 PDF 后重做①②，或在下方表中手工填写页码。"
)


def get_cached_uniform_x(
    pdf_path: Path,
    *,
    content_hash: str | None = None,
) -> int | None:
    """仅接受首课/后续一致（统一 x）的缓存。"""
    cached = get_cached_catalog_pdf_offset(pdf_path, content_hash=content_hash)
    if cached is None:
        return None
    if cached.offset_first != cached.offset_default:
        _log.info("忽略非统一目录偏移缓存 %s（%+d / %+d）", pdf_path.name, cached.offset_first, cached.offset_default)
        return None
    if cached.offset_first < 1 or cached.offset_first > 30:
        return None
    return cached.offset_first


def resolve_uniform_catalog_x(
    pdf_path: Path,
    entries: list[TocEntry],
    lessons: list[Lesson],
    targets: list[dict[str, Any]],
    *,
    edition_label: str | None,
    ocr_dpi: int,
    content_hash: str | None = None,
    force_recalibrate: bool = False,
) -> int | None:
    """显式求 x：PDF 物理页 = 目录逻辑页 + x。"""
    if force_recalibrate and pdf_path is not None:
        clear_cached_catalog_pdf_offset(pdf_path, content_hash=content_hash)

    if pdf_path is not None and not force_recalibrate:
        cached = get_cached_uniform_x(pdf_path, content_hash=content_hash)
        if cached is not None:
            return cached

    if pdf_path is not None and entries and lessons:
        calibrated = calibrate_catalog_pdf_offset(
            pdf_path,
            entries,
            lessons,
            targets,
            ocr_dpi=ocr_dpi,
        )
        if calibrated is not None:
            x = calibrated.offset_first
            if calibrated.offset_first != calibrated.offset_default:
                x = calibrated.offset_first
                _log.info(
                    "目录锚点首/后续偏移不一致（%+d / %+d），采用首课 x=%d 并校正目录",
                    calibrated.offset_first,
                    calibrated.offset_default,
                    x,
                )
            uniform = CatalogPdfOffset(
                offset_first=x,
                offset_default=x,
                first_match_key=calibrated.first_match_key,
                source="uniform_anchor",
            )
            put_cached_catalog_pdf_offset(pdf_path, uniform, content_hash=content_hash)
            return x

    _log.warning(
        "目录偏移未就绪（无缓存且锚点校准失败）%s",
        pdf_path.name if pdf_path else "",
    )
    return None


def logical_entries_to_physical(entries: list[TocEntry], x: int) -> list[TocEntry]:
    return [
        TocEntry(
            unit_norm=e.unit_norm,
            title_raw=e.title_raw,
            page_1=int(e.page_1) + x,
            match_key=e.match_key,
        )
        for e in entries
    ]


def build_uniform_catalog_page_plan(
    *,
    pdf_path: Path,
    entries: list[TocEntry],
    lessons: list[Lesson],
    targets: list[dict[str, Any]],
    edition_label: str | None,
    ocr_dpi: int,
    total_pages: int,
    content_hash: str | None = None,
    force_recalibrate: bool = False,
) -> tuple[int, list[TocEntry], list[int | None], list[int | None]] | None:
    """
    返回 (x, entries_physical, starts_0, ends_1) 与 lessons 等长（0-based start, 1-based end）。
    逻辑页直接采用 LLM 目录缓存，不再逐课 OCR 校正。
    """
    x = resolve_uniform_catalog_x(
        pdf_path,
        entries,
        lessons,
        targets,
        edition_label=edition_label,
        ocr_dpi=ocr_dpi,
        content_hash=content_hash,
        force_recalibrate=force_recalibrate,
    )
    if x is None:
        raise ValueError(MISSING_CATALOG_OFFSET_MSG)

    entries_physical = logical_entries_to_physical(list(entries), x)

    work_indices = [
        i for i, les in enumerate(lessons) if not is_unit_summary_lesson(les)
    ]
    lessons_work = [lessons[i] for i in work_indices]

    hints: list[int | None] = []
    for les in lessons_work:
        hints.append(
            pick_toc_page_hint(
                entries_physical,
                les.unit_title or "",
                les.lesson_name or "",
                str(les.lesson_no or ""),
            )
        )
    if not all(h is not None for h in hints):
        return None

    starts_work = [int(h) - 1 for h in hints]
    ends_work = compute_page_ends_from_toc_entries(
        hints,
        entries_physical,
        total_pages=total_pages,
    )

    starts_full: list[int | None] = [None] * len(lessons)
    ends_full: list[int | None] = [None] * len(lessons)
    for j, orig_i in enumerate(work_indices):
        starts_full[orig_i] = starts_work[j]
        ends_full[orig_i] = ends_work[j]

    return x, entries_physical, starts_full, ends_full
