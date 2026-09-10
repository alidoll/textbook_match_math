"""生成 lesson_uid：{edition}-{grade}-{semester}-{book_type}-U{unit}-L{lesson}"""
from __future__ import annotations

import re


def _slug(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s)
    return s or "unknown"


def make_lesson_uid(
    *,
    edition: str,
    grade: int,
    semester: str,
    book_type: str,
    unit_no: int,
    lesson_no: str,
    copy_version: int = 1,
) -> str:
    sem = _slug(semester)
    if sem in ("上", "上学期", "1"):
        sem_key = "上"
    elif sem in ("下", "下学期", "2"):
        sem_key = "下"
    else:
        sem_key = sem
    # 选读课号 3* → 3x，避免路径非法字符
    ln = re.sub(r"[*＊]", "x", str(lesson_no or "").strip()) or "unknown"
    bt = str(book_type or "").strip() or "old"
    ver = int(copy_version or 1)
    # 教材库同格多副本：v2+ 写入 book_type 段，避免 uk_lessons_uid 与 v1 冲突
    if ver > 1:
        bt = f"{bt}-v{ver}"
    return f"{_slug(edition)}-{grade}-{sem_key}-{bt}-U{unit_no}-L{ln}"


def make_volume_code(
    *,
    edition: str,
    grade: int,
    semester: str,
    book_type: str,
) -> str:
    sem = "S" if _slug(semester) in ("上", "上学期", "1") else "X"
    ed = _slug(edition)[:8].upper()
    return f"{ed}-{grade}{sem}-{book_type.upper()}"
