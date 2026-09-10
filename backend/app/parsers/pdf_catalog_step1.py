"""Step1 风格目录 OCR 兜底（扫描版 PDF 前几页）。"""
from __future__ import annotations

import logging
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from typing import Any

from .benchmark_xlsx import sort_catalog_rows
from .pdf_catalog import CatalogLineParser, extract_catalog_from_pdf, _catalog_score, _catalog_suspect
from .pdf_pages import is_pdf_text_sparse

_log = logging.getLogger(__name__)


def _rapidocr_boxes_to_lines(ocr_result, line_bucket_px: float) -> list[str]:
    if not ocr_result:
        return []
    rows: list[tuple[float, float, str]] = []
    for it in ocr_result:
        try:
            box, txtpair = it[0], it[1]
            if isinstance(txtpair, (list, tuple)):
                txt = str(txtpair[0]).strip()
            else:
                txt = str(txtpair).strip()
            if not txt:
                continue
            ys = [float(p[1]) for p in box]
            xs = [float(p[0]) for p in box]
            cy = (min(ys) + max(ys)) / 2.0
            cx = (min(xs) + max(xs)) / 2.0
            rows.append((cy, cx, txt))
        except (TypeError, IndexError, KeyError, ValueError):
            continue
    buckets: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for cy, cx, txt in rows:
        k = int(round(cy / max(line_bucket_px, 8.0)))
        buckets[k].append((cx, txt))
    lines_out: list[str] = []
    for k in sorted(buckets.keys()):
        parts = sorted(buckets[k], key=lambda x: x[0])
        merged = " ".join(p[1] for p in parts if p[1])
        if merged.strip():
            lines_out.append(merged.strip())
    return lines_out


def extract_catalog_lines_step1_ocr(
    pdf_path: Path,
    *,
    max_pages: int = 9,
    start_page: int = 0,
    end_page: int | None = None,
    zoom: float = 2.0,
) -> list[str]:
    import fitz
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    doc = fitz.open(str(pdf_path))
    all_lines: list[str] = []
    bucket = 24.0 * zoom
    last = end_page if end_page is not None else min(start_page + max_pages, len(doc)) - 1
    last = min(last, len(doc) - 1)
    try:
        for i in range(max(0, start_page), last + 1):
            page = doc.load_page(i)
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.open(BytesIO(pix.tobytes("png")))
            arr = np.asarray(img)
            if arr.ndim == 2:
                arr = np.stack([arr, arr, arr], axis=-1)
            result, _ = ocr(arr)
            all_lines.extend(_rapidocr_boxes_to_lines(result, line_bucket_px=bucket))
    finally:
        doc.close()
    return all_lines


def _ocr_page_text(pdf_path: Path, page_index: int, *, zoom: float = 1.2) -> str:
    """单页 OCR，合并为换行文本（供目录页探测）。"""
    lines = extract_catalog_lines_step1_ocr(
        pdf_path,
        start_page=page_index,
        end_page=page_index,
        zoom=zoom,
    )
    return "\n".join(lines)


def _toc_span_with_padding(
    first_hit: int,
    last_hit: int,
    n_pages: int,
    *,
    first_text: str = "",
) -> tuple[int, int]:
    """目录探测窗：命中页已含第一单元则不再向前/向后垫，避免把正文课页送给视觉模型。"""
    n = max(int(n_pages), 1)
    start = int(first_hit)
    blob = first_text or ""
    if (
        "第一单元" not in blob
        and "第1单元" not in blob
        and "绪论" not in blob
    ):
        start = max(0, int(first_hit) - 1)
    end = min(max(int(last_hit), start), n - 1)
    return start, end


def toc_region_score(text: str) -> int:
    """目录半页得分：正文课页（大标题+插图）应明显低于目录。"""
    from .pdf_catalog import _lesson_line_count, collapse_vertical_cjk_spacing

    t = collapse_vertical_cjk_spacing(text or "")
    score = 0
    if "目录" in t:
        score += 8
    if "第一单元" in t or "第1单元" in t:
        score += 6
    score += _lesson_line_count(t) * 3
    score += min(t.count("单元"), 8)
    return score


def select_toc_regions(
    scored: list[tuple[int, object, str]],
) -> list:
    """有「目录」标题时只保留目录半页；其余半页即使有课名也丢掉。"""
    from .pdf_catalog import _lesson_line_count

    if not scored:
        return []
    titled = [item for _s, item, text in scored if "目录" in (text or "")]
    if titled:
        extra = [
            item
            for _s, item, text in scored
            if _lesson_line_count(text or "") >= 5
        ]
        out: list = []
        seen: set[int] = set()
        for item in titled + extra:
            key = id(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out
    max_s = max(s for s, _item, _t in scored)
    if max_s < 3:
        return [item for _s, item, _t in scored]
    return [item for s, item, _t in scored if s >= 3]


def filter_toc_like_bgrs(regions: list) -> list:
    """对开拆页后丢掉正文半页，只把目录半页送给视觉模型。"""
    if len(regions) <= 1:
        return regions
    try:
        from rapidocr_onnxruntime import RapidOCR
        import cv2
    except ImportError:
        return regions

    ocr = RapidOCR()
    scored: list[tuple[int, object, str]] = []
    for bgr in regions:
        text = ""
        try:
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            result, _ = ocr(rgb)
            lines = _rapidocr_boxes_to_lines(result, line_bucket_px=24.0)
            text = "\n".join(lines)
        except Exception:
            text = ""
        score = toc_region_score(text)
        scored.append((score, bgr, text))
        _log.info("目录半页得分 %s：%s", score, text[:80].replace("\n", " "))
    kept = select_toc_regions(scored)
    return kept or regions


def locate_catalog_page_range(
    pdf_path: Path,
    *,
    max_scan: int = 35,
) -> tuple[int, int]:
    """
    扫描版 PDF 前置页较多时，逐页探测目录区。
    返回 (start, end) 0-based 含端点页码。
    """
    import fitz

    from .pdf_catalog import _lesson_line_count, _toc_title_hit, find_catalog_page_span

    doc = fitz.open(str(pdf_path))
    try:
        n = min(max_scan, len(doc))
        if n <= 0:
            return 0, 0
        pages_text: list[str] = []
        first_hit = -1
        last_hit = -1
        for i in range(n):
            try:
                text = _ocr_page_text(pdf_path, i, zoom=1.2)
            except ImportError:
                _log.warning("RapidOCR 不可用，目录页探测回退前 9 页")
                return 0, min(8, len(doc) - 1)
            pages_text.append(text)
            lesson_n = _lesson_line_count(text)
            # 须目录标题，或「单元」+ 多条课时；禁止正文「1.步骤」页冒充目录
            strong = _toc_title_hit(text) or (lesson_n >= 3 and "单元" in (text or ""))
            if strong:
                if first_hit < 0:
                    first_hit = i
                last_hit = i
                continue
            if first_hit >= 0 and lesson_n == 0 and not _toc_title_hit(text):
                break
        if first_hit >= 0:
            first_text = pages_text[first_hit] if first_hit < len(pages_text) else ""
            return _toc_span_with_padding(
                first_hit, last_hit, len(doc), first_text=first_text
            )
        span = find_catalog_page_span(pages_text, max_scan=n)
        if span.stop > span.start:
            return span.start, span.stop - 1
        return 0, min(8, len(doc) - 1)
    finally:
        doc.close()


def _sparse_catalog_via_ocr(
    pdf_path: Path,
    *,
    ocr_max_pages: int = 9,
    subject: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """扫描版：定位目录页后 OCR；仅化学科目优先化学语法。"""
    from .catalog_lesson_line import parse_chemistry_toc_lines

    start, end = locate_catalog_page_range(pdf_path)
    _log.info(
        "PDF %s 扫描版 OCR 目录页 %d–%d",
        pdf_path.name,
        start + 1,
        end + 1,
    )
    lines = extract_catalog_lines_step1_ocr(
        pdf_path,
        start_page=start,
        end_page=end,
    )
    sub = (subject or "").strip()
    if sub == "化学":
        chem = parse_chemistry_toc_lines(lines)
        if len(chem) >= 3:
            return sort_catalog_rows(chem, subject=sub), f"step1_ocr_p{start + 1}-{end + 1}+chemistry"
    parser = CatalogLineParser()
    step1 = sort_catalog_rows(parser.parse_lines(lines), subject=sub)
    if len(step1) >= 3:
        return step1, f"step1_ocr_p{start + 1}-{end + 1}"
    if sub == "化学":
        chem = parse_chemistry_toc_lines(lines)
        if chem:
            return chem, f"step1_ocr_p{start + 1}-{end + 1}+chemistry_partial"
    return step1, f"step1_ocr_p{start + 1}-{end + 1}"


def extract_catalog_with_fallback(
    pdf_path: Path,
    *,
    ocr_max_pages: int = 9,
    edition: str | None = None,
    grade: int | None = None,
    semester: str | None = None,
    subject: str | None = None,
    force_refresh: bool = False,
    content_hash: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """
    从 PDF 目录区提取单元/节；扫描版直接去印章 + 大模型视觉，避免前置 OCR。
    返回 (catalog_rows, source_tag)。
    """
    sparse = is_pdf_text_sparse(pdf_path)

    basic: list[dict[str, Any]] = []
    if sparse:
        need_fallback = True
        _log.info("PDF %s 扫描版：跳过前置 OCR，直接 LLM 目录识别", pdf_path.name)
    else:
        basic = extract_catalog_from_pdf(pdf_path, ocr_max_pages=ocr_max_pages)
        units, count, max_no = _catalog_score(basic)
        if count >= 20 and units >= 4:
            return basic, "pdf_catalog"
        need_fallback = _catalog_suspect(basic) or count < 12 or units < 3

    if need_fallback:
        llm_rows: list[dict[str, Any]] = []
        llm_exc: Exception | None = None
        try:
            from ..services.llm.catalog_extract import extract_catalog_with_llm_vision
            from ..services.llm.config import llm_enabled

            if llm_enabled():
                page_start, page_end = (0, min(ocr_max_pages, 4) - 1)
                if sparse:
                    page_start, page_end = locate_catalog_page_range(pdf_path)
                llm_rows, llm_tag = extract_catalog_with_llm_vision(
                    pdf_path,
                    edition=edition,
                    grade=grade,
                    semester=semester,
                    subject=subject,
                    max_pages=ocr_max_pages,
                    page_start=page_start,
                    page_end=page_end,
                    force_refresh=force_refresh,
                    content_hash=content_hash,
                )
                if _catalog_score(llm_rows) >= _catalog_score(basic):
                    tag = f"stamp_clean+{llm_tag}" if sparse else llm_tag
                    return sort_catalog_rows(llm_rows, subject=subject), tag
        except Exception as exc:
            llm_exc = exc
            _log.warning("LLM 目录识别失败，回退 OCR：%s", exc)

        if sparse:
            ocr_rows, ocr_tag = _sparse_catalog_via_ocr(
                pdf_path,
                ocr_max_pages=ocr_max_pages,
                subject=subject,
            )
            if len(ocr_rows) >= 3:
                return ocr_rows, ocr_tag
            if llm_exc is not None:
                raise ValueError(
                    f"扫描版目录识别失败（LLM：{llm_exc}；OCR 仅 {len(ocr_rows)} 行）"
                ) from llm_exc
            raise ValueError(
                f"扫描版 PDF 目录识别失败（OCR 仅 {len(ocr_rows)} 行），请检查 PDF 是否含完整目录页"
            )

    lines = extract_catalog_lines_step1_ocr(
        pdf_path,
        max_pages=max(ocr_max_pages, 9),
    )
    parser = CatalogLineParser()
    step1 = sort_catalog_rows(parser.parse_lines(lines))
    if _catalog_score(step1) > _catalog_score(basic):
        return step1, "step1_ocr"
    if basic and _catalog_score(basic)[1] > 0:
        return basic, "pdf_catalog"
    return step1, "step1_ocr"
