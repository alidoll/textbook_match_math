from __future__ import annotations

import re
from typing import Any

from ...parsers.catalog_lesson_line import normalize_huaxue_lesson_for_storage
from ...parsers.unit_title import unit_no_from_title

_BARE_SECTION_RE = re.compile(
    r"^(\d{1,2})\s+([\u4e00-\u9fff「」《》A-Za-z].+)$"
)
_ORPHAN_CHAPTER_DOT_RE = re.compile(r"^(\d{1,2})\.\s*$")
# LLM 常见误写：把「实验活动1 标题」改成「课题6 实验活动」
_BAD_HUAXUE_KETI_REWRITE_RE = re.compile(
    r"^课题\s*\d+\s+(实验活动|跨学科实践活动)\s*$"
)
_HUAXUE_ACTIVITY_FULL_RE = re.compile(
    r"^(实验活动|跨学科实践活动)\s*(\d+)\s*(.*)$"
)
_HUAXUE_KETI_WITH_TITLE_RE = re.compile(r"^课题\s*\d+\s+(.+)$")
_HUAXUE_APPENDIX_HINT_RE = re.compile(
    r"附录\s*[ⅠⅡⅢⅣVVⅠI1-4]?|元素周期表"
)
# 无序号、无标题的残行（LLM 丢掉「1 真实标题」后的残骸）
_BARE_HUAXUE_ACTIVITY_LABEL_RE = re.compile(
    r"^(实验活动|跨学科实践活动)\s*$"
)


def _repair_math_split_section_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """修复粘连拆分把「16.5 标题」拆成「16.」+「5 标题」的情况。"""
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(rows):
        row = dict(rows[i])
        lesson = str(row.get("lesson") or "").strip()
        m_dot = _ORPHAN_CHAPTER_DOT_RE.match(lesson)
        if m_dot and i + 1 < len(rows):
            nxt = rows[i + 1]
            n_lesson = str(nxt.get("lesson") or "").strip()
            m_bare = _BARE_SECTION_RE.match(n_lesson)
            same_unit = str(row.get("unit") or "") == str(nxt.get("unit") or "")
            if m_bare and same_unit:
                chapter = m_dot.group(1)
                section = m_bare.group(1)
                title = m_bare.group(2).strip()
                row["lesson"] = f"{chapter}.{section} {title}"
                if nxt.get("pdf_page") and not row.get("pdf_page"):
                    row["pdf_page"] = nxt.get("pdf_page")
                out.append(row)
                i += 2
                continue
        out.append(row)
        i += 1
    return out


def _prefix_bare_section_with_chapter(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """第十六章下裸「5 标题」→「16.5 标题」（目录原文应为章.节）。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        unit = str(item.get("unit") or "")
        lesson = str(item.get("lesson") or "").strip()
        chapter = unit_no_from_title(unit)
        m = _BARE_SECTION_RE.match(lesson)
        if chapter and m and "." not in lesson.split()[0]:
            section = int(m.group(1))
            title = m.group(2).strip()
            # 节号通常为 1–20；避免误伤其它裸数字课
            if 1 <= section <= 30:
                item["lesson"] = f"{chapter}.{section} {title}"
        out.append(item)
    return out


def _drop_bad_huaxue_keti_rewrites(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉伪目录行：课题N 实验活动 / 无序号无标题的「实验活动」。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        lesson = str(row.get("lesson") or "").strip()
        if _BAD_HUAXUE_KETI_REWRITE_RE.match(lesson):
            continue
        if _BARE_HUAXUE_ACTIVITY_LABEL_RE.match(lesson):
            continue
        out.append(dict(row))
    return out


def _merge_huaxue_split_activity_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并同一单元内「课题N 跨学科/实验活动」+ 下一行「课题M 标题」。

    例：课题5 跨学科实践活动 + 课题4 基于特定需求… → 跨学科实践活动 基于特定需求…
    若单元内已有正式「跨学科实践活动N 同标题」，则两行都丢弃。
    """
    numbered_cover: dict[str, set[str]] = {}
    for row in rows:
        unit = str(row.get("unit") or "")
        lesson = str(row.get("lesson") or "").strip()
        m = _HUAXUE_ACTIVITY_FULL_RE.match(lesson)
        if m and (m.group(3) or "").strip():
            numbered_cover.setdefault(unit, set()).add(re.sub(r"\s+", "", m.group(3)))

    out: list[dict[str, Any]] = []
    i = 0
    n = len(rows)
    while i < n:
        row = dict(rows[i])
        lesson = str(row.get("lesson") or "").strip()
        unit = str(row.get("unit") or "")
        m_cat = _BAD_HUAXUE_KETI_REWRITE_RE.match(lesson)
        if m_cat and i + 1 < n:
            nxt = rows[i + 1]
            n_unit = str(nxt.get("unit") or "")
            n_lesson = str(nxt.get("lesson") or "").strip()
            m_title = _HUAXUE_KETI_WITH_TITLE_RE.match(n_lesson)
            if (
                n_unit == unit
                and m_title
                and not _BAD_HUAXUE_KETI_REWRITE_RE.match(n_lesson)
                and not _HUAXUE_ACTIVITY_FULL_RE.match(n_lesson)
            ):
                title = m_title.group(1).strip()
                if title and title not in ("实验活动", "跨学科实践活动"):
                    compact = re.sub(r"\s+", "", title)
                    if compact in numbered_cover.get(unit, set()):
                        i += 2
                        continue
                    kind = m_cat.group(1)
                    merged = dict(row)
                    merged["lesson"] = f"{kind} {title}".strip()
                    if nxt.get("pdf_page") and not merged.get("pdf_page"):
                        merged["pdf_page"] = nxt.get("pdf_page")
                    out.append(merged)
                    i += 2
                    continue
        out.append(row)
        i += 1
    return out


def _drop_huaxue_orphan_activity_titles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢掉已被正式「跨学科/实验活动N 标题」覆盖的「课题N 同标题」/无序号重复行。"""
    numbered_titles: dict[str, set[str]] = {}
    for row in rows:
        unit = str(row.get("unit") or "")
        lesson = str(row.get("lesson") or "").strip()
        m = _HUAXUE_ACTIVITY_FULL_RE.match(lesson)
        if m and (m.group(3) or "").strip():
            numbered_titles.setdefault(unit, set()).add(re.sub(r"\s+", "", m.group(3)))

    out: list[dict[str, Any]] = []
    for row in rows:
        unit = str(row.get("unit") or "")
        lesson = str(row.get("lesson") or "").strip()
        m = _HUAXUE_KETI_WITH_TITLE_RE.match(lesson)
        if m:
            title = m.group(1).strip()
            if title in ("实验活动", "跨学科实践活动"):
                continue
            if re.sub(r"\s+", "", title) in numbered_titles.get(unit, set()):
                continue
        m2 = re.match(r"^(实验活动|跨学科实践活动)\s+(.+)$", lesson)
        if m2:
            rest = (m2.group(2) or "").strip()
            if rest and not re.match(r"^\d+", rest):
                if re.sub(r"\s+", "", rest) in numbered_titles.get(unit, set()):
                    continue
        out.append(dict(row))
    return out


def _drop_huaxue_appendix_catalog_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """附录不进课时目录（与 PDF TOC 跳过「附录」一致；避免课题N 附录 + 错页码）。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        unit = str(row.get("unit") or "")
        lesson = str(row.get("lesson") or "")
        if "附录" in unit:
            continue
        if _HUAXUE_APPENDIX_HINT_RE.search(lesson):
            continue
        out.append(dict(row))
    return out


def postprocess_catalog_rows(subject: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """行级后处理；未知学科原样返回。语文副标题合并仍在 bootstrap 阶段处理。"""
    out = list(rows or [])
    sub = (subject or "").strip()
    if sub == "数学":
        out = _repair_math_split_section_rows(out)
        out = _prefix_bare_section_with_chapter(out)
    elif sub == "化学":
        out = _merge_huaxue_split_activity_pairs(out)
        out = _drop_bad_huaxue_keti_rewrites(out)
        out = _drop_huaxue_orphan_activity_titles(out)
        out = _drop_huaxue_appendix_catalog_rows(out)
    return out


def normalize_lesson_for_storage(
    subject: str,
    *,
    unit_no: int,
    lesson_no: str,
    lesson_name: str,
) -> tuple[str, str, str]:
    sub = (subject or "").strip()
    if sub == "化学":
        return normalize_huaxue_lesson_for_storage(
            unit_no=unit_no, lesson_no=lesson_no, lesson_name=lesson_name
        )
    return str(lesson_no or ""), str(lesson_name or ""), str(lesson_no or "")
