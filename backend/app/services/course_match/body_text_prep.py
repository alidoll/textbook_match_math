"""粗分前：新课 OCR 汇总 lessons.body_text，供规则混合分与 LLM 正文摘要。"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ...extensions import db
from ...models import Lesson, Volume
from ...parsers.lesson_reuse_match import body_text_match_key
from ...parsers.pdf_pages import ocr_single_page_text
from ..lesson_filters import is_unit_summary_label

_log = logging.getLogger(__name__)

BODY_TEXT_MAX = 80_000
BODY_EXCERPT_FOR_LLM = 1000
OCR_DPI = 96


def body_excerpt_for_llm(body: str | None, *, max_len: int = BODY_EXCERPT_FOR_LLM) -> str:
    """压缩空白后截取前 max_len 字，供 LLM 批量 prompt。"""
    t = re.sub(r"\s+", " ", (body or "").strip())
    if not t:
        return ""
    return t[:max_len]


def _body_ready(body: str | None) -> bool:
    return len(body_text_match_key(body)) >= 40


def ocr_lesson_body_from_pdf(
    les: Lesson,
    pdf_path: Path,
    *,
    ocr_dpi: int = OCR_DPI,
) -> str:
    """按 page_start/end 对 PDF 物理页 OCR，拼接课级正文。"""
    start = les.page_start
    end = les.page_end
    if not start or not end or end < start:
        return ""
    parts: list[str] = []
    import fitz

    doc = fitz.open(str(pdf_path))
    try:
        for pdf_page in range(int(start), int(end) + 1):
            p0 = pdf_page - 1
            if p0 < 0 or p0 >= len(doc):
                continue
            text = ocr_single_page_text(
                pdf_path, p0, dpi=ocr_dpi, doc=doc
            ).strip()
            if text:
                parts.append(text)
    finally:
        doc.close()
    return "\n\n".join(parts)


def ensure_lessons_body_text_for_coarse_match(
    volume: Volume,
    lessons: list[Lesson],
    *,
    force: bool = False,
    ocr_dpi: int = OCR_DPI,
) -> dict[str, int | list[str]]:
    """
    粗分前补齐新课 body_text（已有足够汉字则跳过）。
    返回统计：ocr_lessons / skipped / empty / warnings。
    """
    from ..new_library.pdf.parse import _pdf_path_for_volume

    pdf_path: Path | None = None
    try:
        pdf_path = _pdf_path_for_volume(volume)
    except ValueError:
        pdf_path = None

    stats: dict[str, int | list[str]] = {
        "ocr_lessons": 0,
        "skipped": 0,
        "empty": 0,
        "warnings": [],
    }
    warnings = stats["warnings"]
    assert isinstance(warnings, list)

    for les in lessons:
        if is_unit_summary_label(les.lesson_name):
            continue
        if not force and _body_ready(les.body_text):
            stats["skipped"] = int(stats["skipped"]) + 1
            continue

        body = ""
        if pdf_path is not None:
            try:
                body = ocr_lesson_body_from_pdf(les, pdf_path, ocr_dpi=ocr_dpi)
            except OSError as exc:
                warnings.append(f"{les.lesson_name}: PDF OCR 失败 {exc}")

        if body.strip():
            les.body_text = body.strip()[:BODY_TEXT_MAX]
            stats["ocr_lessons"] = int(stats["ocr_lessons"]) + 1
            _log.info(
                "粗分 OCR 正文 %s · %s：%d 字",
                volume.volume_code,
                les.lesson_name,
                len(body_text_match_key(body)),
            )
        else:
            stats["empty"] = int(stats["empty"]) + 1
            warnings.append(
                f"{les.lesson_name}：无可用正文（请确认已划分页码且 PDF 可 OCR）"
            )

    db.session.flush()
    return stats
