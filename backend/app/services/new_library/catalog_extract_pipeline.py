"""新库建设专用目录识别（与 textbook_diff 管线隔离）。

原则：
- 小学科学是新库最早学科，走所见即所得，不套化学「课题N」语法
- 不调用 textbook_diff.intake_catalog_pipeline / run_diff_catalog_extract
- OCR 兜底禁止对非化学科目优先 parse_chemistry_toc_lines
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...parsers.benchmark_xlsx import sort_catalog_rows
from ...parsers.pdf_catalog import CatalogLineParser, _catalog_score
from ...parsers.pdf_catalog_step1 import (
    extract_catalog_lines_step1_ocr,
    locate_catalog_page_range,
)
from ...parsers.pdf_pages import is_pdf_text_sparse

_log = logging.getLogger(__name__)

# 新库科学目录允许保留的栏目词（勿当噪声剔除）
_SCIENCE_TOC_KEEP_HINTS = (
    "专题研究",
    "生涯链接",
    "能力增长",
    "评价表",
    "科技擂台",
    "科学思维",
)

# 新库通用噪声（不含「科学探究/指南车…」等科学栏目词）
_NEW_LIBRARY_EXCLUDE = (
    "前言",
    "编者的话",
    "版权",
    "图书在版编目",
    "定价",
    "出版社",
    "后记",
    "索引",
    "词汇表",
    "义务教育",
    "教科书",
    "ISBN",
    "CIP",
    "责任编辑",
)


class NewLibraryCatalogLineParser(CatalogLineParser):
    """新库目录行解析：排除词不含科学栏目，避免误删专题研究等。"""

    EXCLUDE_KEYWORDS = _NEW_LIBRARY_EXCLUDE


def _accept_llm_catalog(rows: list[dict[str, Any]], subject: str | None) -> bool:
    """是否采纳 LLM 目录结果。

    科学/小科 LLM 有时省略课序号（lesson 仅课名），_catalog_score 编号计数为 0，
    旧逻辑会整表丢弃并回退 OCR，导致只剩少数「带数字空格」的脏行。
    """
    if not rows:
        return False
    _units, numbered, _max_no = _catalog_score(rows)
    if numbered >= 3:
        return True
    sub = (subject or "").strip()
    if sub not in ("科学", "小科"):
        return False
    if len(rows) < 8:
        return False
    units = {str(r.get("unit") or "").strip() for r in rows}
    units.discard("")
    return len(units) >= 2


def _ocr_catalog_for_new_library(
    pdf_path: Path,
    *,
    subject: str | None,
    ocr_max_pages: int = 9,
) -> tuple[list[dict[str, Any]], str]:
    """新库 OCR 兜底：仅化学才走化学目录语法。"""
    start, end = locate_catalog_page_range(pdf_path)
    lines = extract_catalog_lines_step1_ocr(
        pdf_path,
        start_page=start,
        end_page=end,
    )
    sub = (subject or "").strip()
    if sub == "化学":
        from ...parsers.catalog_lesson_line import parse_chemistry_toc_lines

        chem = parse_chemistry_toc_lines(lines)
        if len(chem) >= 3:
            return (
                sort_catalog_rows(chem, subject=sub),
                f"new_library_ocr_p{start + 1}-{end + 1}+chemistry",
            )

    parser = NewLibraryCatalogLineParser()
    rows = sort_catalog_rows(parser.parse_lines(lines), subject=sub)
    tag = f"new_library_ocr_p{start + 1}-{end + 1}"
    if len(rows) >= 3:
        return rows, tag
    return rows, tag


def extract_new_library_catalog(
    pdf_path: Path,
    *,
    edition: str | None = None,
    grade: int | None = None,
    semester: str | None = None,
    subject: str | None = None,
    force_refresh: bool = False,
    content_hash: str | None = None,
    ocr_max_pages: int = 9,
) -> tuple[list[dict[str, Any]], str]:
    """
    新库目录识别：优先 LLM（传入学科），失败再 OCR。
    与教材对比的 extract_catalog_with_fallback / chemistry-first 路径隔离。
    """
    sub = (subject or "").strip()
    sparse = is_pdf_text_sparse(pdf_path)

    llm_rows: list[dict[str, Any]] = []
    llm_tag = ""
    llm_exc: Exception | None = None
    try:
        from ..llm.catalog_extract import extract_catalog_with_llm_vision
        from ..llm.config import llm_enabled

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
                max_pages=max(ocr_max_pages, 6),
                page_start=page_start,
                page_end=page_end,
                force_refresh=force_refresh,
                content_hash=content_hash,
            )
            if _accept_llm_catalog(llm_rows, sub):
                tag = f"new_library+{llm_tag}"
                return sort_catalog_rows(llm_rows, subject=sub), tag
            _log.warning(
                "新库 LLM 目录未达采纳门槛（%d 行, score=%s），回退 OCR",
                len(llm_rows),
                _catalog_score(llm_rows),
            )
    except Exception as exc:
        llm_exc = exc
        _log.warning("新库 LLM 目录识别失败，回退 OCR：%s", exc)

    ocr_rows, ocr_tag = _ocr_catalog_for_new_library(
        pdf_path,
        subject=subject,
        ocr_max_pages=ocr_max_pages,
    )
    if len(ocr_rows) >= 3:
        return ocr_rows, ocr_tag

    if llm_rows and (
        _accept_llm_catalog(llm_rows, sub) or _catalog_score(llm_rows)[1] > 0 or len(llm_rows) >= 3
    ):
        return sort_catalog_rows(llm_rows, subject=sub), f"new_library+{llm_tag}+weak"

    if llm_exc is not None:
        raise ValueError(
            f"新库目录识别失败（LLM：{llm_exc}；OCR 仅 {len(ocr_rows)} 行）"
        ) from llm_exc
    raise ValueError(
        f"新库目录识别不足（{len(ocr_rows)} 行）。请确认 PDF 含完整目录页。"
    )


def looks_like_science_toc_row(lesson: str) -> bool:
    """调试/测试辅助：是否像科学目录栏目。"""
    t = (lesson or "").strip()
    return any(h in t for h in _SCIENCE_TOC_KEEP_HINTS)
