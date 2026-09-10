"""教材对比：目录逻辑页码 → PDF 物理页码（自动校准偏移）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...models import Lesson
from ...parsers.catalog_page_offset import (
    CatalogPdfOffset,
    get_cached_catalog_pdf_offset,
    put_cached_catalog_pdf_offset,
)
from ...parsers.llm_toc_cache import get_llm_toc_entries
from ...parsers.pdf_pages import (
    extract_page_texts_pdfplumber,
    find_last_toc_page_index,
    is_toc_like_page,
    page_likely_lesson_start,
    trim_backmatter_pages,
)
from ...parsers.pdf_toc import compute_page_ends_from_toc_entries, pick_toc_page_hint
from ...parsers.text_norm import norm_text, strip_lesson_seq
from ...services.lesson_filters import is_unit_summary_lesson
from ...services.old_library.pdf.lesson_targets import build_lesson_targets

_log = logging.getLogger(__name__)

_MIN_OFFSET = 1
_MAX_OFFSET = 30
_MAX_OFFSET_SPREAD = 60


def store_diff_catalog_offset(
    pdf_path: Path,
    *,
    content_hash: str | None = None,
    offset: int,
    source: str = "auto_calibrated",
    first_match_key: str = "",
) -> CatalogPdfOffset:
    off = CatalogPdfOffset(
        offset_first=offset,
        offset_default=offset,
        first_match_key=first_match_key or "diff_catalog_offset",
        source=source,
    )
    put_cached_catalog_pdf_offset(pdf_path, off, content_hash=content_hash)
    return off


def _extract_printed_page_number(text: str) -> int | None:
    """从页面文字开头提取印刷页码（数学教材页眉常见："2 数学 八年级上册" 或 "第十二章 ... 3"）。"""
    if not text:
        return None
    # 取前 3 行
    head = text.strip()[:120]
    for line in head.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 行首纯数字（如 "2  数学 八年级上册"）
        import re
        m = re.match(r"^(\d{1,3})\s", line)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 999:
                return n
        # 行尾纯数字（如 "第十二章 分式和分式方程 3"）
        m = re.search(r"(\d{1,3})\s*$", line)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 999:
                return n
    return None


def calibrate_diff_catalog_offset(
    pdf_path: Path,
    entries: list,
    lessons: list[Lesson],
    *,
    layout: str = "single",
) -> CatalogPdfOffset | None:
    """
    自动推算目录逻辑页 → 系统页（single=sheet / spread=view）的固定偏移 x。

    两阶段策略：
    A) 页码直读法（优先，快且准）：从 PDF 每页文字开头提取印刷页码，
       对前几课的目录页码 + 候选偏移范围，看印刷页码是否匹配
    B) 课名锚点法（回退）：在窗口内搜索课名标题行
    """
    from ...parsers.pdf_spread import sheet_side_to_view, view_to_sheet_side

    max_off = _MAX_OFFSET_SPREAD if layout == "spread" else _MAX_OFFSET

    def _sheet_to_system(sheet_1: int) -> int:
        if layout == "spread":
            return sheet_side_to_view(sheet_1, "left")
        return sheet_1

    def _system_to_sheet0(system_1: int) -> int:
        if layout == "spread":
            sheet_1, _side = view_to_sheet_side(system_1)
            return sheet_1 - 1
        return system_1 - 1

    targets = build_lesson_targets(lessons)
    work = [
        (les, tgt)
        for les, tgt in zip(lessons, targets, strict=False)
        if not is_unit_summary_lesson(les)
    ]
    if len(work) < 2:
        return None

    import fitz

    doc = fitz.open(str(pdf_path))
    total_sheets = len(doc)

    # 取前 5 课做锚点
    anchor_lessons = work[:min(5, len(work))]
    cat_hints: list[int | None] = []
    for les, _ in anchor_lessons:
        cat_hints.append(
            pick_toc_page_hint(
                entries,
                les.unit_title or "",
                les.lesson_name or "",
                str(les.lesson_no or ""),
            )
        )

    les1, tgt1 = anchor_lessons[0]
    cat1 = cat_hints[0]
    if cat1 is None:
        _log.warning("首课目录页码缺失：%s", pdf_path.name)
        doc.close()
        return None

    # ---- 阶段 A：页码直读法 ----
    # 对每个候选偏移 x，检查 cat_i + x 处的印刷页码是否 == cat_i
    best_x: int | None = None
    best_hits = 0

    for x in range(_MIN_OFFSET, max_off + 1):
        hits = 0
        for idx, (les, tgt) in enumerate(anchor_lessons):
            cat_i = cat_hints[idx]
            if cat_i is None:
                continue
            sys_page = cat_i + x  # 1-based 系统页
            sheet_0 = _system_to_sheet0(sys_page)
            if 0 <= sheet_0 < total_sheets:
                text = doc.load_page(sheet_0).get_text() or ""
                printed = _extract_printed_page_number(text)
                if printed == cat_i:
                    hits += 1

        if hits > best_hits:
            best_hits = hits
            best_x = x

    if best_x is not None and best_hits >= 2:
        mk = norm_text(
            strip_lesson_seq(les1.lesson_name or "")
        ) or norm_text(strip_lesson_seq(str(les1.lesson_no or "")))
        _log.info(
            "目录偏移自动校准（页码直读）%s：x=%d（%d/%d 锚点匹配；layout=%s）",
            pdf_path.name,
            best_x,
            best_hits,
            len(anchor_lessons),
            layout,
        )
        doc.close()
        return CatalogPdfOffset(
            offset_first=best_x,
            offset_default=best_x,
            first_match_key=mk or les1.lesson_uid,
            source="auto_calibrated",
        )

    _log.info(
        "页码直读法未命中（best_x=%s, hits=%d/%d），回退课名锚点法：%s",
        best_x,
        best_hits,
        len(anchor_lessons),
        pdf_path.name,
    )

    # ---- 阶段 B：课名锚点法（回退）----
    # 只提取前 20 页用于 toc 边界检测
    head_pages: list[str] = []
    for i in range(min(20, total_sheets)):
        head_pages.append(doc.load_page(i).get_text() or "")

    toc_end = find_last_toc_page_index(head_pages, list(targets))
    search_from = (toc_end + 1) if toc_end >= 0 else 0

    def _is_start(p0: int, tgt: dict) -> bool:
        if 0 <= p0 < len(head_pages):
            text = head_pages[p0]
        elif 0 <= p0 < total_sheets:
            text = doc.load_page(p0).get_text() or ""
        else:
            return False
        if not text or len(text.strip()) < 4:
            return False
        if p0 < len(head_pages):
            page_text = head_pages[p0]
        else:
            page_text = text
        if is_toc_like_page(page_text, list(targets)):
            return False
        return page_likely_lesson_start(text, tgt)

    window_lo = max(search_from, cat1 - 1)
    window_hi = min(total_sheets, cat1 + max_off + 2)
    p1_candidates = [
        p0 + 1
        for p0 in range(window_lo, window_hi)
        if _is_start(p0, tgt1)
    ]

    for p1_sheet in p1_candidates:
        p1 = _sheet_to_system(p1_sheet)
        x = p1 - cat1
        if x < _MIN_OFFSET or x > max_off:
            continue

        hits = 0
        for idx, (les, tgt) in enumerate(anchor_lessons):
            cat_i = cat_hints[idx]
            if cat_i is None:
                continue
            p_i = cat_i + x
            if 1 <= p_i <= total_sheets:
                if _is_start(_system_to_sheet0(p_i), tgt):
                    hits += 1

        if hits > best_hits:
            best_hits = hits
            best_x = x

    doc.close()

    if best_x is not None and best_hits >= 2:
        mk = norm_text(
            strip_lesson_seq(les1.lesson_name or "")
        ) or norm_text(strip_lesson_seq(str(les1.lesson_no or "")))
        _log.info(
            "目录偏移自动校准（课名锚点）%s：x=%d（%d/%d 锚点匹配；layout=%s）",
            pdf_path.name,
            best_x,
            best_hits,
            len(anchor_lessons),
            layout,
        )
        return CatalogPdfOffset(
            offset_first=best_x,
            offset_default=best_x,
            first_match_key=mk or les1.lesson_uid,
            source="auto_calibrated",
        )

    _log.warning(
        "目录偏移自动校准失败（best_x=%s, hits=%d/%d）：%s",
        best_x,
        best_hits,
        len(anchor_lessons),
        pdf_path.name,
    )
    return None


def resolve_diff_catalog_offset(
    pdf_path: Path,
    entries: list,
    lessons: list[Lesson],
    *,
    content_hash: str | None = None,
    force: bool = False,
    layout: str = "single",
) -> int | None:
    """优先读缓存；否则从 PDF 文字层自动校准并写入缓存。"""
    max_off = _MAX_OFFSET_SPREAD if layout == "spread" else _MAX_OFFSET
    if not force:
        cached = get_cached_catalog_pdf_offset(pdf_path, content_hash=content_hash)
        if cached is not None and cached.offset_first == cached.offset_default:
            if _MIN_OFFSET <= cached.offset_first <= max_off:
                return cached.offset_first

    calibrated = calibrate_diff_catalog_offset(
        pdf_path, entries, lessons, layout=layout
    )
    if calibrated is None:
        return None
    store_diff_catalog_offset(
        pdf_path,
        content_hash=content_hash,
        offset=calibrated.offset_first,
        source=calibrated.source,
        first_match_key=calibrated.first_match_key,
    )
    return calibrated.offset_first


def _lesson_label(les: Lesson) -> str:
    no = str(les.lesson_no or "").strip()
    name = str(les.lesson_name or "").strip()
    if no and name:
        return f"{no} {name}"
    return no or name or les.lesson_uid


def compute_diff_lesson_page_plan(
    *,
    lessons: list[Lesson],
    entries: list,
    total_pages: int,
    offset: int,
    pdf_path: Path | None = None,
    layout: str = "single",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] | None:
    """
    按「系统页 = 逻辑页 + offset」划分（single=PDF sheet，spread=view）。
    单元末课止于下一课逻辑起始 −2。
    全书最后一节止于附录/后记前一页。
    返回 (row_updates, match_details, plan_rows)。
    """
    work_indices = [
        i for i, les in enumerate(lessons) if not is_unit_summary_lesson(les)
    ]
    if not work_indices:
        return None

    logical_starts: list[int | None] = []
    for i in work_indices:
        les = lessons[i]
        logical_starts.append(
            pick_toc_page_hint(
                entries,
                les.unit_title or "",
                les.lesson_name or "",
                str(les.lesson_no or ""),
            )
        )

    if not all(h is not None for h in logical_starts):
        missing = sum(1 for h in logical_starts if h is None)
        have = len(work_indices) - missing
        _log.warning("目录逻辑页码缺失 %d/%d 课", missing, len(work_indices))
        # 允许少量缺失：用相邻已匹配课的页码做占位，避免整册失败
        if have < max(3, int(len(work_indices) * 0.65)):
            return None
        last = None
        for j, h in enumerate(logical_starts):
            if h is not None:
                last = h
                continue
            nxt = next((x for x in logical_starts[j + 1 :] if x is not None), None)
            if last is not None and nxt is not None:
                logical_starts[j] = max(last, min(nxt, last + 1))
            elif last is not None:
                logical_starts[j] = last + 1
            elif nxt is not None:
                logical_starts[j] = max(1, nxt - 1)
            else:
                return None
        if any(h is None for h in logical_starts):
            return None
        _log.warning("已对 %d 课用相邻页码占位，请人工核对页码", missing)

    lessons_work = [lessons[i] for i in work_indices]
    logical_total = max(1, total_pages - offset)
    logical_ends = compute_page_ends_from_toc_entries(
        [int(h) for h in logical_starts],
        entries,
        total_pages=logical_total,
        lessons_work=lessons_work,
    )

    row_updates: list[dict[str, Any]] = []
    match_details: list[dict[str, Any]] = []
    plan_rows: list[dict[str, Any]] = []

    starts_full: list[int | None] = [None] * len(lessons)
    ends_full: list[int | None] = [None] * len(lessons)

    for j, orig_i in enumerate(work_indices):
        logical_start = int(logical_starts[j])
        logical_end = logical_ends[j]
        if logical_end is None:
            continue
        physical_start = logical_start + offset
        physical_end = min(max(physical_start, int(logical_end) + offset), total_pages)

        starts_full[orig_i] = physical_start - 1
        ends_full[orig_i] = physical_end

        les = lessons[orig_i]
        plan_rows.append(
            {
                "lesson_uid": les.lesson_uid,
                "unit_title": les.unit_title,
                "lesson_label": _lesson_label(les),
                "logical_start": logical_start,
                "physical_start": physical_start,
                "physical_end": physical_end,
            }
        )

    # 对开扫描：文字层按 sheet 索引，不能直接用 view 坐标做后记裁剪
    if pdf_path is not None and layout != "spread":
        # 用 fitz 提取文字层（比 pdfplumber 快 10 倍以上）
        import fitz as _fitz

        pages_text: list[str] = []
        with _fitz.open(str(pdf_path)) as _doc:
            for _i in range(len(_doc)):
                pages_text.append(_doc.load_page(_i).get_text() or "")
        if pages_text:
            ends_full = trim_backmatter_pages(pages_text, starts_full, ends_full)
            for row in plan_rows:
                uid = row["lesson_uid"]
                idx = next(
                    i for i, les in enumerate(lessons) if les.lesson_uid == uid
                )
                end_1 = ends_full[idx]
                if end_1 is not None:
                    row["physical_end"] = end_1

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
                detail.update(
                    {
                        "page_start": page_start,
                        "page_end": page_end,
                        "matched": True,
                    }
                )
        match_details.append(detail)

    return row_updates, match_details, plan_rows


def compute_diff_lesson_pages(
    *,
    lessons: list[Lesson],
    pdf_path: Path,
    content_hash: str | None = None,
    total_pages: int,
    force_offset: bool = True,
    layout: str = "single",
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    entries = get_llm_toc_entries(pdf_path, content_hash=content_hash)
    if not entries:
        return None

    offset = resolve_diff_catalog_offset(
        pdf_path,
        entries,
        lessons,
        content_hash=content_hash,
        force=force_offset,
        layout=layout,
    )
    if offset is None:
        from .spread_offset import calibrate_spread_offset_via_ocr

        ocr_x = calibrate_spread_offset_via_ocr(
            pdf_path, entries, layout=layout
        )
        if ocr_x is not None:
            offset = ocr_x
            store_diff_catalog_offset(
                pdf_path,
                content_hash=content_hash,
                offset=offset,
                source=f"{layout}_ocr",
                first_match_key=f"{layout}_ocr",
            )
            _log.info(
                "OCR 校准偏移 x=%d（系统页=印刷页+%d，layout=%s）：%s",
                offset,
                offset,
                layout,
                pdf_path.name,
            )
        elif layout == "spread":
            # 小学语文对开常见前言约 3 view（封面/目录后接印刷 p1/p2）
            offset = 3
            store_diff_catalog_offset(
                pdf_path,
                content_hash=content_hash,
                offset=offset,
                source="spread_default",
                first_match_key="spread_default",
            )
            _log.warning(
                "对开册无法自动校准偏移，使用默认 x=%d（view=印刷页+%d）：%s",
                offset,
                offset,
                pdf_path.name,
            )
    if offset is None:
        return None

    plan = compute_diff_lesson_page_plan(
        lessons=lessons,
        entries=entries,
        total_pages=total_pages,
        offset=offset,
        pdf_path=pdf_path,
        layout=layout,
    )
    if plan is None:
        return None

    row_updates, match_details, plan_rows = plan
    matched = sum(1 for d in match_details if d.get("matched"))

    page_kind = "view页" if layout == "spread" else "PDF物理页"
    meta = {
        "matched": matched,
        "lesson_count": len(lessons),
        "text_mode": "diff_logical_auto_offset",
        "total_pages": total_pages,
        "page_layout": layout,
        "catalog_lines": len(entries),
        "catalog_offset": offset,
        "warnings": [
            f"自动校准偏移 x={offset}：{page_kind} = 目录逻辑页 + {offset}；"
            f"单元末课止于下一课起始 − 2"
            + ("（对开按左右半页计）" if layout == "spread" else ""),
        ],
        "page_plan": plan_rows,
    }
    _log.info(
        "教材对比 %s：逻辑页+ %d 划分 %d/%d 课（layout=%s）",
        pdf_path.name,
        offset,
        matched,
        len(lessons),
        layout,
    )
    return meta, row_updates, match_details
