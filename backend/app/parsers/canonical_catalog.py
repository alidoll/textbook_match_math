"""定稿页码表（由整册 OCR 人工校对，对齐 courseware-migration-sync canonical 脚本）。"""
from __future__ import annotations

import re
from typing import Any

from ..models import Lesson
from .text_norm import norm_text, strip_lesson_seq

# 湘科版 4 上 新教材：PDF 1-based 起始页（sync: canonical_xiangke_4_up.py）
_XIANGKE_4_UP: tuple[tuple[str, str, int], ...] = (
    ("第一单元 变与不变", "1 陶泥变形记", 4),
    ("第一单元 变与不变", "2 蜡的有趣变化", 8),
    ("第一单元 变与不变", "3 混合与分离", 12),
    ("第一单元 变与不变", "4 做盐花", 15),
    ("第一单元 变与不变", "单元小结", 18),
    ("第二单元 消化与呼吸", "5 我们的消化", 20),
    ("第二单元 消化与呼吸", "6 认识营养成分", 25),
    ("第二单元 消化与呼吸", "7 保护消化器官", 29),
    ("第二单元 消化与呼吸", "8 我们的呼吸", 33),
    ("第二单元 消化与呼吸", "9 保护呼吸器官", 37),
    ("第二单元 消化与呼吸", "单元小结", 40),
    ("第三单元 位置与运动", "10 谁在运动", 42),
    ("第三单元 位置与运动", "11 它们是怎样运动的", 46),
    ("第三单元 位置与运动", "12 怎样比较运动的快慢", 50),
    ("第三单元 位置与运动", "13 运动与能量", 54),
    ("第三单元 位置与运动", "14 做个溜溜球", 58),
    ("第三单元 位置与运动", "单元小结", 64),
    ("第四单元 声音", "15 各种各样的声音", 66),
    ("第四单元 声音", "16 声音的产生", 69),
    ("第四单元 声音", "17 声音的变化", 73),
    ("第四单元 声音", "18 声音的传播", 77),
    ("第四单元 声音", "19 噪声的控制", 81),
    ("第四单元 声音", "单元小结", 86),
    ('第五单元 "小星星"演奏会', '20 筹备"小星星"演奏会', 88),
    ('第五单元 "小星星"演奏会', "21 设计我的小乐器", 91),
    ('第五单元 "小星星"演奏会', "22 制作我的小乐器", 94),
    ('第五单元 "小星星"演奏会', '23 "小星星"演奏会', 98),
    ('第五单元 "小星星"演奏会', "单元小结", 101),
)

_UNIT_PREFIX_RE = re.compile(r"^第[一二三四五六七八九十百千\d]+单元")


def _norm_edition(edition: str) -> str:
    e = norm_text(edition)
    if "湘科" in e or "湘教" in e:
        return "xiangke"
    return edition.strip()


def _norm_semester(semester: str) -> str:
    s = (semester or "").strip()
    if "下" in s:
        return "下"
    return "上"


def _unit_context_match(canon_unit: str, table_unit: str) -> bool:
    a = norm_text(canon_unit)
    b = norm_text(table_unit)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ma = _UNIT_PREFIX_RE.match(a)
    mb = _UNIT_PREFIX_RE.match(b)
    if ma and mb and ma.group(0) == mb.group(0):
        return True
    if len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4]:
        return True
    return False


def _canon_lesson_no(lesson_label: str) -> str:
    m = re.match(r"^(\d{1,2})\s*", (lesson_label or "").strip())
    return m.group(1) if m else ""


def _canon_is_summary(lesson_label: str) -> bool:
    return (lesson_label or "").strip() == "单元小结"


def _lesson_body_key(lesson_name: str) -> str:
    return norm_text(strip_lesson_seq(lesson_name))


def _match_lesson_to_canon_row(les: Lesson, row: tuple[str, str, int]) -> bool:
    unit, lesson_label, _ = row
    if not _unit_context_match(unit, les.unit_title or ""):
        return False
    if _canon_is_summary(lesson_label):
        return "小结" in (les.lesson_name or "")
    canon_no = _canon_lesson_no(lesson_label)
    if canon_no and str(les.lesson_no).strip() != canon_no:
        return False
    body = _lesson_body_key(les.lesson_name or "")
    canon_body = _lesson_body_key(lesson_label)
    if not body or not canon_body:
        return False
    return body == canon_body or body in canon_body or canon_body in body


def get_canonical_rows(
    edition_label: str,
    grade: int,
    semester: str,
) -> list[tuple[str, str, int]] | None:
    if _norm_edition(edition_label) != "xiangke":
        return None
    if grade != 4 or _norm_semester(semester) != "上":
        return None
    return list(_XIANGKE_4_UP)


def resolve_canonical_page_hints(
    lessons: list[Lesson],
    *,
    edition_label: str,
    grade: int,
    semester: str,
) -> tuple[list[int | None], str] | None:
    """按定稿表为每节课返回 PDF 1-based 起始页提示；无法匹配册次时返回 None。"""
    rows = get_canonical_rows(edition_label, grade, semester)
    if not rows:
        return None

    hints: list[int | None] = []
    for les in lessons:
        page: int | None = None
        for row in rows:
            if _match_lesson_to_canon_row(les, row):
                page = row[2]
                break
        hints.append(page)

    if not any(hints):
        return None
    return hints, "canonical"


def resolve_page_hints_for_volume(
    lessons: list[Lesson],
    *,
    edition_label: str,
    grade: int,
    semester: str,
    pdf_path: Any | None = None,
) -> tuple[list[int | None], str] | None:
    """定稿页码优先，其次 PDF 前部目录 OCR。"""
    canon = resolve_canonical_page_hints(
        lessons,
        edition_label=edition_label,
        grade=grade,
        semester=semester,
    )
    if canon and sum(1 for h in canon[0] if h is not None) >= max(1, len(lessons) * 0.8):
        return canon

    if pdf_path is not None:
        from .pdf_toc import resolve_toc_page_hints

        toc = resolve_toc_page_hints(pdf_path, lessons, edition_label=edition_label)
        if toc and sum(1 for h in toc[0] if h is not None) >= max(1, len(lessons) * 0.5):
            return toc

    return canon
