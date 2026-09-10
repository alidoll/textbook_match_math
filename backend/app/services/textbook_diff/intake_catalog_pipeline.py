"""教材对比 intake：识别目录最高档编排（LLM-first + 交叉校验）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...parsers.catalog_lesson_line import dedupe_catalog_rows, merge_catalog_rows
from .catalog import (
    _MIN_CATALOG_LINES,
    _collect_raw_text,
    _extract_with_llm_direct,
    _raw_text_to_catalog,
)
from .subject_postprocess import postprocess_catalog_rows

_MIN = _MIN_CATALOG_LINES

_BAD_HUAXUE_KETI_REWRITE_RE = re.compile(
    r"^课题\s*\d+\s+(实验活动|跨学科实践活动)\s*$"
)
_HUAXUE_ACTIVITY_RE = re.compile(r"^(?:实验活动|跨学科实践活动)\s*\d+")


@dataclass
class CatalogExtractResult:
    rows: list[dict[str, Any]]
    source: str
    degraded: bool
    warnings: list[str] = field(default_factory=list)


def extract_llm_catalog(
    pdf_path,
    *,
    edition,
    grade,
    semester,
    force_refresh,
    content_hash,
    subject=None,
):
    """包装现有 LLM 视觉目录识别。"""
    return _extract_with_llm_direct(
        pdf_path,
        edition,
        grade,
        semester,
        force_refresh,
        content_hash,
        subject=subject,
    )


def extract_pdf_crosscheck_catalog(pdf_path: Path, *, subject: str) -> list[dict]:
    """文字层目录作交叉校验源；语文/数学/科学/Test 返回空列表以免化学 TOC parser 污染。"""
    if (subject or "").strip() in ("语文", "数学", "科学", "Test", "小科"):
        return []
    raw_lines = _collect_raw_text(pdf_path, max_pages=20)
    return _raw_text_to_catalog(raw_lines)


def _chemistry_prefer_pdf_text(pdf_rows: list[dict], llm_rows: list[dict]) -> bool:
    """文字层已有实验/跨学科原文，或 LLM 出现「课题N 实验活动」误写时，以 PDF 为主。"""
    if len(pdf_rows) < _MIN:
        return False
    pdf_act = sum(
        1
        for r in pdf_rows
        if _HUAXUE_ACTIVITY_RE.match(str(r.get("lesson") or "").strip())
    )
    llm_bad = sum(
        1
        for r in llm_rows
        if _BAD_HUAXUE_KETI_REWRITE_RE.match(str(r.get("lesson") or "").strip())
    )
    return pdf_act >= 1 or (llm_bad > 0 and len(pdf_rows) >= len(llm_rows))


def _chemistry_pdf_only(pdf_rows: list[dict]) -> bool:
    """文字层目录已完整（含多条实验/跨学科）时不再合并 LLM，避免伪「课题N 实验活动」。"""
    if len(pdf_rows) < 20:
        return False
    pdf_act = sum(
        1
        for r in pdf_rows
        if _HUAXUE_ACTIVITY_RE.match(str(r.get("lesson") or "").strip())
    )
    return pdf_act >= 4


def run_diff_catalog_extract(
    pdf_path,
    *,
    volume,
    content_hash,
    force_refresh,
    layout=None,
) -> CatalogExtractResult:
    """LLM 与文字层交叉校验；化学文字层完整时仅用 PDF 目录原文（所见即所得）。"""
    subject = (getattr(volume, "subject", None) or "").strip()
    pdf_path = Path(pdf_path)

    pdf_rows = extract_pdf_crosscheck_catalog(pdf_path, subject=subject)
    warnings: list[str] = []
    degraded = False

    # 化学 + 文字层目录完整：跳过 LLM，杜绝改写课名
    if subject == "化学" and _chemistry_pdf_only(pdf_rows):
        rows = postprocess_catalog_rows(subject, dedupe_catalog_rows(list(pdf_rows)))
        from ...parsers.catalog_lesson_line import expand_glued_catalog_rows

        rows = expand_glued_catalog_rows(rows)
        rows = postprocess_catalog_rows(subject, rows)
        rows = dedupe_catalog_rows(rows)
        return CatalogExtractResult(
            rows=rows,
            source="pdf_text",
            degraded=False,
            warnings=["化学目录仅用 PDF 文字层原文（所见即所得）"],
        )

    llm_rows, tag = extract_llm_catalog(
        pdf_path,
        edition=volume.edition,
        grade=volume.grade,
        semester=volume.semester,
        force_refresh=force_refresh,
        content_hash=content_hash,
        subject=subject or None,
    )

    llm_list = list(llm_rows) if llm_rows else []

    if llm_list:
        source = f"llm:{tag}" if tag else "llm"
        if pdf_rows:
            if subject == "化学" and _chemistry_prefer_pdf_text(pdf_rows, llm_list):
                rows = merge_catalog_rows(pdf_rows, llm_list)
                source = f"pdf_text+llm:{tag}" if tag else "pdf_text+llm"
                warnings.append("化学目录以 PDF 文字层原文为主（所见即所得）")
            else:
                rows = merge_catalog_rows(llm_list, pdf_rows)
                added = len(rows) - len(llm_list)
                if added > 0:
                    source = f"{source}+pdf_text({added})"
        else:
            rows = llm_list
    elif len(pdf_rows) >= _MIN:
        rows = list(pdf_rows)
        source = "pdf_text"
        degraded = True
        warnings.append("视觉目录失败，已降级文字层")
    else:
        n = len(pdf_rows)
        raise ValueError(_catalog_extract_failure_message(pdf_path, n, layout=layout))

    rows = postprocess_catalog_rows(subject, dedupe_catalog_rows(rows))
    from ...parsers.catalog_lesson_line import expand_glued_catalog_rows

    rows = expand_glued_catalog_rows(rows)
    # 数学：粘连拆分可能误切「16.5」；expand 后再修一次
    rows = postprocess_catalog_rows(subject, rows)
    rows = dedupe_catalog_rows(rows)
    return CatalogExtractResult(
        rows=rows,
        source=source,
        degraded=degraded,
        warnings=warnings,
    )


def _catalog_extract_failure_message(
    pdf_path: Path, catalog_len: int, *, layout=None
) -> str:
    """与现网 bootstrap「目录识别失败」同结构的可操作提示。"""
    raw_lines: list[str] = []
    try:
        raw_lines = _collect_raw_text(pdf_path, max_pages=20)
    except Exception:
        pass
    preview = raw_lines[:30] if raw_lines else ["<无文本>"]
    page_hint = ""
    try:
        import fitz

        with fitz.open(str(pdf_path)) as doc:
            pdf_pages = len(doc)
        if layout is not None:
            from ...parsers.pdf_spread import view_count_from_sheets

            view_pages = view_count_from_sheets(pdf_pages, layout)
            if layout == "spread" and pdf_pages < 40:
                page_hint = (
                    f" PDF 共 {pdf_pages} 张对开图（约 {view_pages} 印刷页），"
                    "可能未上传全书；请确认已上传完整扫描版 PDF。"
                )
            elif layout != "spread" and pdf_pages < 80:
                page_hint = (
                    f" PDF 共 {pdf_pages} 页，可能未上传全书；"
                    "请确认已上传完整扫描版 PDF。"
                )
    except Exception:
        pass
    return (
        f"目录识别失败：仅提取 {catalog_len} 行。{page_hint}"
        f"PDF 前 20 页原始文字共 {len(raw_lines)} 行，"
        f"预览（前30）：{' | '.join(preview[:30])}"
    )
