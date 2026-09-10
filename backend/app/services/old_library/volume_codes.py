"""册次 volume_code / lesson_uid 生成。"""
from __future__ import annotations

import re

from .edition_registry import EDITIONS, EditionDef, GRADE_LABELS, get_edition
from ...services.lesson_uid import make_lesson_uid

_VOLUME_CODE_RE = re.compile(r"^(.+)-(\d)([SX])-(NEW|OLD)$", re.I)


def normalize_term(term: str) -> str:
    return "下" if "下" in str(term or "") else "上"


def make_volume_code(edition: EditionDef, *, grade: int, term: str, book_type: str = "old") -> str:
    sem = "S" if normalize_term(term) == "上" else "X"
    return f"{edition.code_prefix}-{grade}{sem}-{book_type.upper()}"


def parse_volume_code(volume_code: str) -> tuple[EditionDef, int, str, str]:
    """从 volume_code 解析版本、年级、学期、book_type（new/old）。"""
    code = (volume_code or "").strip()
    match = _VOLUME_CODE_RE.match(code)
    if not match:
        raise ValueError(f"册次编码无效：{volume_code}")
    prefix, grade_s, sem, book_type = match.groups()
    grade = int(grade_s)
    term = "上" if sem.upper() == "S" else "下"
    book = book_type.lower()
    matches = [ed for ed in EDITIONS.values() if ed.code_prefix == prefix]
    if len(matches) != 1:
        raise ValueError(f"未识别的册次编码：{volume_code}")
    edition = matches[0]
    expected = make_volume_code(edition, grade=grade, term=term, book_type=book)
    if expected != code:
        raise ValueError(f"册次编码无效：{volume_code}")
    return edition, grade, term, book


def make_display_title(edition: EditionDef, *, grade: int, term: str) -> str:
    term_key = normalize_term(term)
    suffix = "上册" if term_key == "上" else "下册"
    return f"{edition.label}{GRADE_LABELS[grade]}{suffix}"


def make_lesson_uid_for_volume(
    edition: EditionDef,
    *,
    grade: int,
    term: str,
    book_type: str,
    unit_no: int,
    lesson_no: str,
    copy_version: int = 1,
) -> str:
    return make_lesson_uid(
        edition=edition.label,
        grade=grade,
        semester=normalize_term(term),
        book_type=book_type,
        unit_no=unit_no,
        lesson_no=lesson_no,
        copy_version=copy_version,
    )
