"""目录印刷页码 → PDF 物理页码：按册首课/次课锚点自动校准偏移。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Lesson
from .llm_toc_cache import _cache_key, _disk_file
from .pdf_pages import (
    LazyPageTexts,
    PdfOcrCache,
    has_lesson_header,
    is_toc_like_page,
    page_likely_lesson_start,
)
from .pdf_toc import TocEntry, pick_toc_page_hint
from .text_norm import norm_text, strip_lesson_seq

_log = logging.getLogger(__name__)

_CN_UNIT_NUM = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class CatalogPdfOffset:
    """一册教材的目录页码 → PDF 物理页 偏移（首课可与其余课不同）。"""

    offset_first: int
    offset_default: int
    first_match_key: str
    source: str


def is_xiangke_edition(edition_label: str | None) -> bool:
    if not edition_label:
        return False
    e = edition_label.strip()
    return "湘科" in e or "湘教" in e


def _offset_disk_path(pdf_path: Path, *, content_hash: str | None = None) -> Path:
    return _disk_file(_cache_key(pdf_path, content_hash=content_hash)).with_suffix(
        ".offset.json"
    )


def get_cached_catalog_pdf_offset(
    pdf_path: Path,
    *,
    content_hash: str | None = None,
) -> CatalogPdfOffset | None:
    path = _offset_disk_path(pdf_path, content_hash=content_hash)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        offsets = CatalogPdfOffset(
            offset_first=int(raw["offset_first"]),
            offset_default=int(raw["offset_default"]),
            first_match_key=str(raw["first_match_key"]),
            source=str(raw.get("source") or "cached"),
        )
        if offsets.offset_first < 1 or offsets.offset_default < 1:
            _log.warning("忽略无效目录偏移缓存 %s", path.name)
            return None
        return offsets
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        _log.warning("读取目录偏移缓存失败 %s: %s", path.name, exc)
        return None


def put_cached_catalog_pdf_offset(
    pdf_path: Path,
    offsets: CatalogPdfOffset,
    *,
    content_hash: str | None = None,
) -> None:
    path = _offset_disk_path(pdf_path, content_hash=content_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "offset_first": offsets.offset_first,
        "offset_default": offsets.offset_default,
        "first_match_key": offsets.first_match_key,
        "source": offsets.source,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def clear_cached_catalog_pdf_offset(
    pdf_path: Path,
    *,
    content_hash: str | None = None,
) -> bool:
    """删除目录偏移缓存，强制下次重新校准。返回是否删除了文件。"""
    path = _offset_disk_path(pdf_path, content_hash=content_hash)
    if not path.is_file():
        return False
    try:
        path.unlink()
        _log.info("已清除目录偏移缓存 %s", path.name)
        return True
    except OSError as exc:
        _log.warning("清除目录偏移缓存失败 %s: %s", path.name, exc)
        return False


def _xiangke_fallback_offset(
    catalog_page: int,
    *,
    unit_no: int,
    first_lesson_in_unit: bool,
    first_in_book: bool,
) -> int:
    """校准失败时的湘科经验回退（非首选）。"""
    if first_in_book and unit_no == 1 and first_lesson_in_unit:
        return catalog_page + 1
    return catalog_page + 2


def _collect_pdf_start_candidates(
    pages: LazyPageTexts,
    tgt: dict[str, Any],
    catalog_page: int,
    *,
    search_from_0: int = 0,
    window_before: int = 4,
    window_after: int = 16,
    center_shifts: tuple[int, ...] = (1, 2, 0, 3, -1),
    all_targets: list[dict[str, Any]] | None = None,
) -> list[int]:
    """收集目录提示附近所有可能的课节起始 PDF 页（1-based，去重排序）。"""
    total = len(pages)
    found: set[int] = set()
    header_hits: set[int] = set()
    targets_for_toc = all_targets or [tgt]
    for shift in center_shifts:
        center = max(0, int(catalog_page) - 1 + shift)
        lo = max(search_from_0, center - window_before)
        hi = min(total, center + window_after + 1)
        for p in range(lo, hi):
            if p < search_from_0:
                continue
            text = pages[p]
            if is_toc_like_page(text, targets_for_toc):
                continue
            if has_lesson_header(text, tgt):
                header_hits.add(p + 1)
                found.add(p + 1)
            elif page_likely_lesson_start(text, tgt):
                found.add(p + 1)
    # 有标题行命中时优先只用它们，避免课中复述课题名抢走锚点
    if header_hits:
        return sorted(header_hits)
    return sorted(found)


def _find_pdf_page_for_lesson(
    pages: LazyPageTexts,
    tgt: dict[str, Any],
    catalog_page: int,
    *,
    search_from_0: int = 0,
    window_before: int = 4,
    window_after: int = 16,
    center_shifts: tuple[int, ...] = (1, 2, 0, 3, -1),
    all_targets: list[dict[str, Any]] | None = None,
) -> int | None:
    """在目录提示页附近 OCR 定位课节真实 PDF 起始页（1-based）。"""
    candidates = _collect_pdf_start_candidates(
        pages,
        tgt,
        catalog_page,
        search_from_0=search_from_0,
        window_before=window_before,
        window_after=window_after,
        center_shifts=center_shifts,
        all_targets=all_targets,
    )
    if not candidates:
        return None
    center = max(0, int(catalog_page) - 1 + 1)
    return min(candidates, key=lambda p: abs(p - 1 - center))


def _score_offset_pair(offset_first: int, offset_default: int) -> tuple[int, int, int]:
    """偏移组合得分（越小越好）。"""
    consistency = abs(offset_first - offset_default)
    magnitude = abs(offset_first) + abs(offset_default)
    penalty = 0 if offset_first >= 0 and offset_default >= 0 else 1
    return (consistency, penalty, magnitude)


def calibrate_catalog_pdf_offset(
    pdf_path: Path,
    entries: list[TocEntry],
    lessons: list[Lesson],
    targets: list[dict[str, Any]],
    *,
    ocr_dpi: int = 96,
) -> CatalogPdfOffset | None:
    """
    用第 1、2 课在 PDF 中的真实起始页与目录页码之差，推算整册偏移。
    首课偏移与后续课偏移可不同（如 +1 / +2）。
    """
    from ..services.lesson_filters import is_unit_summary_lesson

    work: list[tuple[Lesson, dict[str, Any]]] = [
        (les, tgt)
        for les, tgt in zip(lessons, targets, strict=False)
        if not is_unit_summary_lesson(les)
    ]
    if not work:
        return None

    les1, tgt1 = work[0]
    cat1 = pick_toc_page_hint(
        entries,
        les1.unit_title or "",
        les1.lesson_name or "",
        str(les1.lesson_no or ""),
    )
    if cat1 is None:
        return None

    ocr = PdfOcrCache(pdf_path, dpi=min(ocr_dpi, 96))
    pages = LazyPageTexts(ocr)
    pdf1: int | None = None
    offset_first = 0
    offset_default = 0
    try:
        first_mk = norm_text(strip_lesson_seq(les1.lesson_name or "")) or norm_text(
            les1.lesson_name or ""
        )
        all_targets = [t for _, t in work]
        pdf1_candidates = _collect_pdf_start_candidates(
            pages, tgt1, cat1, all_targets=all_targets
        )
        if not pdf1_candidates:
            return None

        best: tuple[tuple[int, int, int], int, int, int] | None = None
        if len(work) >= 2:
            les2, tgt2 = work[1]
            cat2 = pick_toc_page_hint(
                entries,
                les2.unit_title or "",
                les2.lesson_name or "",
                str(les2.lesson_no or ""),
            )
            if cat2 is not None:
                for cand1 in pdf1_candidates[:6]:
                    off1 = cand1 - cat1
                    search_from = max(0, cand1 - 1)
                    pdf2 = _find_pdf_page_for_lesson(
                        pages,
                        tgt2,
                        cat2 + off1,
                        search_from_0=search_from,
                        center_shifts=(0, 1, -1, 2),
                        all_targets=all_targets,
                    )
                    if pdf2 is None or pdf2 <= cand1:
                        continue
                    off2 = pdf2 - cat2
                    score = _score_offset_pair(off1, off2)
                    if best is None or score < best[0]:
                        best = (score, cand1, off1, off2)

        if best is not None:
            _, pdf1, offset_first, offset_default = best
        else:
            pdf1 = _find_pdf_page_for_lesson(
                pages, tgt1, cat1, all_targets=all_targets
            )
            if pdf1 is None:
                return None
            offset_first = pdf1 - cat1
            offset_default = offset_first
            if len(work) >= 2:
                les2, tgt2 = work[1]
                cat2 = pick_toc_page_hint(
                    entries,
                    les2.unit_title or "",
                    les2.lesson_name or "",
                    str(les2.lesson_no or ""),
                )
                if cat2 is not None:
                    search_from = max(0, pdf1 - 1)
                    pdf2 = _find_pdf_page_for_lesson(
                        pages,
                        tgt2,
                        cat2 + offset_first,
                        search_from_0=search_from,
                        all_targets=all_targets,
                    )
                    if pdf2 is not None and pdf2 > pdf1:
                        offset_default = pdf2 - cat2
    finally:
        ocr.close()

    if offset_first < 1 or offset_first > 30 or offset_default < 1 or offset_default > 30:
        _log.warning(
            "目录偏移异常（首课 %+d，其余 %+d），放弃校准",
            offset_first,
            offset_default,
        )
        return None

    result = CatalogPdfOffset(
        offset_first=offset_first,
        offset_default=offset_default,
        first_match_key=first_mk,
        source="anchor_scan",
    )
    _log.info(
        "目录锚点校准 %s：首课 %+d，其余 %+d（PDF p%d / 目录 p%d）",
        pdf_path.name,
        offset_first,
        offset_default,
        pdf1,
        cat1,
    )
    return result


def resolve_catalog_pdf_offset(
    pdf_path: Path | None,
    entries: list[TocEntry],
    lessons: list[Lesson],
    targets: list[dict[str, Any]],
    *,
    edition_label: str | None = None,
    ocr_dpi: int = 96,
    scan_anchors: bool = False,
    content_hash: str | None = None,
) -> CatalogPdfOffset | None:
    if pdf_path is not None:
        cached = get_cached_catalog_pdf_offset(pdf_path, content_hash=content_hash)
        if cached is not None:
            return cached

    offsets: CatalogPdfOffset | None = None
    if scan_anchors and pdf_path is not None and entries and lessons:
        offsets = calibrate_catalog_pdf_offset(
            pdf_path, entries, lessons, targets, ocr_dpi=ocr_dpi
        )
        if offsets is not None:
            put_cached_catalog_pdf_offset(pdf_path, offsets, content_hash=content_hash)
            return offsets

    return None


def _entry_uses_first_offset(entry: TocEntry, offsets: CatalogPdfOffset) -> bool:
    if not offsets.first_match_key:
        return False
    if entry.match_key == offsets.first_match_key:
        return True
    title_norm = norm_text(entry.title_raw)
    if title_norm.startswith("1 ") or title_norm.startswith("①"):
        mk = norm_text(strip_lesson_seq(entry.title_raw))
        if mk == offsets.first_match_key:
            return True
    return False


def offset_toc_entries_to_pdf(
    entries: list[TocEntry],
    *,
    pdf_path: Path | None = None,
    lessons: list[Lesson] | None = None,
    targets: list[dict[str, Any]] | None = None,
    edition_label: str | None = None,
    ocr_dpi: int = 96,
    scan_anchors: bool = False,
    content_hash: str | None = None,
) -> list[TocEntry]:
    """将 TocEntry.page_1 从目录印刷页码转为 PDF 物理页码。"""
    if not entries:
        return entries

    offsets: CatalogPdfOffset | None = None
    if lessons and targets:
        offsets = resolve_catalog_pdf_offset(
            pdf_path,
            entries,
            lessons,
            targets,
            edition_label=edition_label,
            ocr_dpi=ocr_dpi,
            scan_anchors=scan_anchors or pdf_path is not None,
            content_hash=content_hash,
        )

    if offsets is not None:
        x = offsets.offset_default
        if offsets.offset_first != offsets.offset_default:
            _log.warning(
                "目录偏移非统一（%+d / %+d），按首课 x=%d 转换",
                offsets.offset_first,
                offsets.offset_default,
                offsets.offset_first,
            )
            x = offsets.offset_first
        out: list[TocEntry] = []
        for entry in entries:
            out.append(
                TocEntry(
                    unit_norm=entry.unit_norm,
                    title_raw=entry.title_raw,
                    page_1=int(entry.page_1) + x,
                    match_key=entry.match_key,
                )
            )
        return out

    if is_xiangke_edition(edition_label):
        out = []
        prev_unit: str | None = None
        for entry in entries:
            first_in_unit = entry.unit_norm != (prev_unit or "")
            unit_no = _unit_no_from_title(entry.unit_norm)
            first_in_book = prev_unit is None and first_in_unit
            pdf_page = _xiangke_fallback_offset(
                int(entry.page_1),
                unit_no=unit_no,
                first_lesson_in_unit=first_in_unit,
                first_in_book=first_in_book,
            )
            out.append(
                TocEntry(
                    unit_norm=entry.unit_norm,
                    title_raw=entry.title_raw,
                    page_1=pdf_page,
                    match_key=entry.match_key,
                )
            )
            prev_unit = entry.unit_norm
        return out

    return entries


def _unit_no_from_title(unit: str) -> int:
    from .unit_title import unit_no_from_title

    n = unit_no_from_title(unit)
    return n if n is not None else 1


def xiangke_catalog_page_to_pdf(
    catalog_page: int,
    *,
    unit_no: int,
    first_lesson_in_unit: bool,
) -> int:
    return _xiangke_fallback_offset(
        catalog_page,
        unit_no=unit_no,
        first_lesson_in_unit=first_lesson_in_unit,
        first_in_book=unit_no == 1 and first_lesson_in_unit,
    )
