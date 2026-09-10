"""扫描版 PDF 前部目录 OCR + 页码提示（对齐 sync auto_lesson_page_compare_report）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Lesson
from .pdf_catalog_step1 import extract_catalog_lines_step1_ocr
from .text_norm import norm_text, strip_lesson_seq

RE_CN_UNIT = re.compile(r"^第[一二三四五六七八九十百千\d]+单元")
RE_TOC_LEADERS = re.compile(r"\.{2,}|…{2,}|·{4,}")


@dataclass(frozen=True)
class TocEntry:
    unit_norm: str
    title_raw: str
    page_1: int
    match_key: str


def _lesson_match_key(lesson: str) -> str:
    return norm_text(strip_lesson_seq(lesson))


def _parse_title_page(line: str) -> tuple[str, int] | None:
    s = (line or "").strip()
    if len(s) < 4 or not re.search(r"[\u4e00-\u9fff]", s):
        return None
    if RE_TOC_LEADERS.search(s):
        m = re.match(
            r"^(?P<title>.+?)\s*(?:\.{2,}|…{2,}|·{3,})\s*(?P<pg>\d{1,3})\s*$",
            s,
        )
        if m:
            t = norm_text(m.group("title"))
            pg = int(m.group("pg"))
            if len(t) >= 3 and 1 <= pg <= 400:
                return t, pg
    m2 = re.match(
        r"^\s*(?P<title>\d{1,2}\s*[\u4e00-\u9fff（][^\n]{1,48}?)\s+(?P<pg>\d{1,3})\s*$",
        s,
    )
    if m2:
        t = norm_text(m2.group("title"))
        pg = int(m2.group("pg"))
        if len(t) >= 3 and 1 <= pg <= 400:
            return t, pg
    m3 = re.match(r"^(.{4,48}?)\s{2,}(\d{1,3})\s*$", s)
    if m3 and re.search(r"[\u4e00-\u9fff]", m3.group(1)):
        t = norm_text(m3.group(1))
        pg = int(m3.group(2))
        if len(t) >= 3 and 3 <= pg <= 400:
            return t, pg
    # OCR 常见：「3 混合与分离 10」单空格
    m4 = re.match(r"^(?P<title>\d{1,2}\s+.+?)\s+(?P<pg>\d{1,3})\s*$", s)
    if m4:
        t = norm_text(m4.group("title"))
        pg = int(m4.group("pg"))
        if len(t) >= 3 and 1 <= pg <= 400:
            return t, pg
    if "单元小结" in s:
        m5 = re.search(r"(\d{1,3})\s*$", s)
        if m5:
            pg = int(m5.group(1))
            if 1 <= pg <= 400:
                return norm_text("单元小结"), pg
    return None


def parse_toc_entries_from_lines(lines: list[str]) -> list[TocEntry]:
    out: list[TocEntry] = []
    current_unit = ""
    for raw in lines:
        line = (raw or "").strip()
        if not line or line == "目录":
            continue
        if RE_CN_UNIT.match(line) or line.startswith("第一单元") or line.startswith("第二单元"):
            unit_part = line
            parsed_u = _parse_title_page(line)
            if parsed_u and RE_CN_UNIT.match(line.split()[0] if line.split() else line):
                current_unit = norm_text(parsed_u[0])[:48]
            else:
                current_unit = norm_text(line)[:48]
            if parsed_u and parsed_u[0] != current_unit:
                mk = _lesson_match_key(parsed_u[0])
                if mk and len(mk) >= 3:
                    out.append(
                        TocEntry(
                            unit_norm=current_unit,
                            title_raw=parsed_u[0],
                            page_1=parsed_u[1],
                            match_key=mk,
                        )
                    )
            continue
        parsed = _parse_title_page(line)
        if not parsed:
            if "单元小结" in line:
                parsed = _parse_title_page(line)
            if not parsed:
                continue
        title, pg = parsed
        mk = _lesson_match_key(title)
        if not mk or len(mk) < 2:
            if "小结" in title:
                mk = norm_text("单元小结")
            else:
                continue
        out.append(
            TocEntry(
                unit_norm=current_unit,
                title_raw=title,
                page_1=pg,
                match_key=mk,
            )
        )
    return out


def _unit_context_match(toc_unit: str, table_unit: str) -> bool:
    a = norm_text(toc_unit)
    b = norm_text(table_unit)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    if len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4]:
        return True
    return False


def pick_toc_page_hint(
    entries: list[TocEntry],
    table_unit: str,
    lesson_name: str,
    lesson_no: str,
) -> int | None:
    label = f"{lesson_no} {lesson_name}".strip()
    special_no = str(lesson_no or "").strip()
    if special_no.startswith(("实验活动", "跨学科", "综合实践", "微项目")) or special_no in (
        "整理与提升",
        "复习与提高",
    ):
        lk = _lesson_match_key(special_no)
        if not lk and special_no.startswith("跨学科"):
            lk = special_no
        # 全称「跨学科实践活动N」对齐目录 match_key「跨学科N」
        if not lk or special_no.startswith("跨学科实践活动"):
            m_x = re.match(r"^跨学科(?:实践活动)?(\d+)$", special_no)
            if m_x:
                lk = f"跨学科{m_x.group(1)}"
    elif special_no.startswith("课题"):
        lk = _lesson_match_key(lesson_name)
        if not lk:
            lk = _lesson_match_key(special_no)
    else:
        lk = _lesson_match_key(lesson_name)
        if not lk and special_no:
            lk = _lesson_match_key(special_no)
    body = norm_text(strip_lesson_seq(lesson_name)) or norm_text(strip_lesson_seq(special_no))
    cand = [e for e in entries if e.match_key == lk or body in e.match_key]
    if not cand and special_no.startswith("跨学科"):
        m_x = re.match(r"^跨学科(?:实践活动)?(\d+)$", special_no)
        if m_x:
            xk = f"跨学科{m_x.group(1)}"
            cand = [e for e in entries if e.match_key == xk]
    if not cand and body and len(body) >= 3:
        cand = [
            e
            for e in entries
            if body in norm_text(e.title_raw) or body in e.match_key
        ]
    # 语文目录常把多课挤进同一行名（如「观潮 8古诗三首」）：用条目 match_key 做子串匹配
    if not cand and body and len(body) >= 2:
        cand = [
            e
            for e in entries
            if e.match_key and len(e.match_key) >= 2 and e.match_key in body
        ]
    if not cand and special_no.isdigit():
        # 课序号 + 单元上下文兜底（同册多课共名时靠 unit 排序）
        cand = [
            e
            for e in entries
            if re.match(rf"^{re.escape(special_no)}[\s\*]", e.title_raw.strip())
            or re.match(rf"^{re.escape(special_no)}\s", e.title_raw.strip())
        ]
    if not cand and "小结" in lesson_name:
        cand = [e for e in entries if "小结" in e.match_key]
    if not cand:
        return None

    les_n = norm_text(label)
    if lesson_no.isdigit() and len(cand) > 1:
        num = lesson_no
        narrowed = [
            e
            for e in cand
            if norm_text(e.title_raw).startswith(num)
            or re.match(rf"^{num}\s", e.title_raw)
        ]
        if narrowed:
            cand = narrowed

    def sort_key(e: TocEntry) -> tuple[int, int, int]:
        u_bonus = 0 if _unit_context_match(e.unit_norm, table_unit) else 1
        t_raw = norm_text(e.title_raw)
        title_bonus = 0 if (les_n in t_raw or t_raw in les_n or body in t_raw) else 1
        return (u_bonus, title_bonus, e.page_1)

    cand.sort(key=sort_key)
    return cand[0].page_1


def sorted_toc_anchor_pages(entries: list[TocEntry]) -> list[int]:
    """目录中所有锚点页（课时、单元小结、后记等），去重升序。"""
    pages = {int(e.page_1) for e in entries if e.page_1 is not None and 1 <= int(e.page_1) <= 400}
    return sorted(pages)


def _is_unit_header_title(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return False
    if RE_CN_UNIT.match(t):
        return True
    if t == "绪论" or (
        t.startswith("绪论")
        and "单元" not in t
        and "章" not in t
        and not re.search(r"课题|第\d+节", t)
    ):
        return True
    return False


def pick_unit_toc_page_hint(entries: list[TocEntry], unit_title: str) -> int | None:
    """目录中单元扉页/绪论起始逻辑页（非该单元第一节课页码）。"""
    from .unit_title import unit_no_from_title

    table_unit = unit_title or ""
    cand: list[TocEntry] = []
    for e in entries:
        if not _is_unit_header_title(e.title_raw):
            continue
        if _unit_context_match(e.title_raw, table_unit) or _unit_context_match(
            e.unit_norm, table_unit
        ):
            cand.append(e)
    if cand:
        return min(int(e.page_1) for e in cand)

    target_no = unit_no_from_title(table_unit)
    if target_no is not None:
        for e in entries:
            if not _is_unit_header_title(e.title_raw):
                continue
            e_no = unit_no_from_title(e.title_raw) or unit_no_from_title(e.unit_norm)
            if e_no == target_no:
                cand.append(e)
        if cand:
            return min(int(e.page_1) for e in cand)
    return None


def _same_lesson_unit(a: str, b: str) -> bool:
    from .unit_title import unit_no_from_title

    ua = unit_no_from_title(a)
    ub = unit_no_from_title(b)
    if ua is not None and ub is not None:
        return ua == ub
    return _unit_context_match(a, b) or norm_text(a) == norm_text(b)


def _is_last_lesson_in_unit(lessons: list[Lesson], j: int) -> bool:
    if j + 1 >= len(lessons):
        return True
    cur = lessons[j].unit_title or ""
    nxt = lessons[j + 1].unit_title or ""
    return not _same_lesson_unit(cur, nxt)


def _next_lesson_is_yuwen_appendix(lessons: list[Lesson], j: int) -> bool:
    """下一课是语文附录表时，单元末不必再空一页扉页。"""
    if j + 1 >= len(lessons):
        return False
    nxt = lessons[j + 1]
    unit = (nxt.unit_title or "").strip()
    if unit == "附录":
        return True
    blob = f"{nxt.lesson_no or ''}{nxt.lesson_name or ''}"
    return any(t in blob for t in ("识字表", "写字表", "词语表"))


def _end_from_next_toc_anchor(
    start: int,
    anchors: list[int],
    *,
    total_pages: int,
) -> int:
    end = total_pages
    for pg in anchors:
        if pg > start:
            end = pg - 1
            break
    return max(start, end)


def compute_page_ends_from_toc_entries(
    starts_1: list[int | None],
    entries: list[TocEntry],
    *,
    total_pages: int,
    lessons_work: list[Lesson] | None = None,
) -> list[int | None]:
    """
    推算 1-based page_end。

    无 lessons_work：按目录下一锚点（含单元小结、后记）止页。
    有 lessons_work：单元内非末课 = 下一课逻辑起始 −1；
    单元末课 = 下一课逻辑起始 −2（跨单元多留 1 页给单元扉/过渡，如绪论、复习与提高）。
    """
    anchors = sorted_toc_anchor_pages(entries)
    ends: list[int | None] = []
    for j, start in enumerate(starts_1):
        if start is None:
            ends.append(None)
            continue
        start_i = int(start)

        if lessons_work is not None and j + 1 < len(starts_1):
            next_start = starts_1[j + 1]
            if next_start is not None and not _is_last_lesson_in_unit(lessons_work, j):
                ends.append(max(start_i, int(next_start) - 1))
                continue

            if next_start is not None and _is_last_lesson_in_unit(lessons_work, j):
                # 跨到下一教学单元通常 −2（留扉页）；跨到附录三表 −1
                gap = 1 if _next_lesson_is_yuwen_appendix(lessons_work, j) else 2
                ends.append(max(start_i, int(next_start) - gap))
                continue

        ends.append(_end_from_next_toc_anchor(start_i, anchors, total_pages=total_pages))
    return ends


def extract_toc_entries_from_pdf(
    pdf_path: Path,
    *,
    max_pages: int = 9,
) -> list[TocEntry]:
    from .pdf_pages import is_pdf_text_sparse
    from .pdf_stamp_remove import build_catalog_pdf_without_stamps

    work_pdf = pdf_path
    cleaned: Path | None = None
    if is_pdf_text_sparse(pdf_path):
        cleaned = build_catalog_pdf_without_stamps(
            pdf_path,
            max_pages=max_pages,
            force_sparse=True,
        )
        if cleaned is not None:
            work_pdf = cleaned
    try:
        lines = extract_catalog_lines_step1_ocr(work_pdf, max_pages=max_pages)
        return parse_toc_entries_from_lines(lines)
    finally:
        if cleaned is not None and cleaned != pdf_path:
            cleaned.unlink(missing_ok=True)


def resolve_toc_page_hints(
    pdf_path: Path,
    lessons: list[Lesson],
    *,
    max_pages: int = 9,
    edition_label: str | None = None,
) -> tuple[list[int | None], str] | None:
    from .llm_toc_cache import get_llm_toc_entries
    from .catalog_page_offset import offset_toc_entries_to_pdf

    entries = get_llm_toc_entries(pdf_path)
    source = "llm_vision"
    if not entries:
        entries = extract_toc_entries_from_pdf(pdf_path, max_pages=max_pages)
        source = "toc_ocr"
    if len(entries) < 3:
        return None
    from ..services.old_library.pdf.lesson_targets import build_lesson_targets

    targets = build_lesson_targets(lessons)
    entries_work = offset_toc_entries_to_pdf(
        entries,
        pdf_path=pdf_path,
        lessons=lessons,
        targets=targets,
        edition_label=edition_label,
    )
    hints: list[int | None] = []
    for les in lessons:
        hints.append(
            pick_toc_page_hint(
                entries_work,
                les.unit_title or "",
                les.lesson_name or "",
                str(les.lesson_no or ""),
            )
        )
    return hints, source
