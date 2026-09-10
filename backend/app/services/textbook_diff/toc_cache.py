"""教材对比：目录页码缓存（化学文字层优先，否则沿用 LLM 视觉目录页码）。"""
from __future__ import annotations

import logging
from pathlib import Path

from ...models import Lesson
from ...parsers.catalog_lesson_line import parse_chemistry_toc_entries
from ...parsers.llm_toc_cache import get_llm_toc_entries, put_llm_toc_entries
from .catalog import _extract_fitz_text_lines
from .page_plan import resolve_diff_catalog_offset

_log = logging.getLogger(__name__)


def ensure_diff_toc_page_cache(
    pdf_path: Path,
    *,
    lessons: list[Lesson] | None = None,
    content_hash: str | None = None,
    force: bool = False,
    layout: str = "single",
) -> int:
    """
    确保 llm_toc 缓存含目录印刷逻辑页码，并尝试校准偏移 x。
    返回 toc 条目数。

    - 化学等有文字层：可从 PDF 正文解析补充/覆盖
    - 语文扫描版：文字层常为空，保留识别目录时 LLM 写入的 toc
    """
    existing = get_llm_toc_entries(pdf_path, content_hash=content_hash) or []

    if not force and len(existing) >= 3:
        if lessons:
            resolve_diff_catalog_offset(
                pdf_path,
                existing,
                lessons,
                content_hash=content_hash,
                force=False,
                layout=layout,
            )
        return len(existing)

    lines = _extract_fitz_text_lines(pdf_path, max_pages=20)
    chem_entries = parse_chemistry_toc_entries(lines)

    if len(chem_entries) >= 3:
        put_llm_toc_entries(pdf_path, chem_entries, content_hash=content_hash)
        entries = chem_entries
        _log.info(
            "目录页码来自 PDF 文字层：%d 条（%s）",
            len(entries),
            pdf_path.name,
        )
    elif len(existing) >= 3:
        entries = existing
        _log.info(
            "目录页码沿用 LLM 缓存：%d 条（文字层不足，%s）",
            len(entries),
            pdf_path.name,
        )
    else:
        _log.warning(
            "目录页码条目不足：文字层 %d，LLM 缓存 %d（%s）",
            len(chem_entries),
            len(existing),
            pdf_path.name,
        )
        return 0

    if lessons:
        x = resolve_diff_catalog_offset(
            pdf_path,
            entries,
            lessons,
            content_hash=content_hash,
            force=True,
            layout=layout,
        )
        if x is not None:
            _log.info(
                "教材对比目录偏移 x=%d（%s，%d 条逻辑页码，layout=%s）",
                x,
                pdf_path.name,
                len(entries),
                layout,
            )
        else:
            _log.warning("教材对比目录偏移自动校准失败：%s", pdf_path.name)
    return len(entries)
