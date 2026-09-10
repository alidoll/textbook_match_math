"""课时列表按单元 + sort_order / 数字节号排序。"""
from __future__ import annotations

from sqlalchemy import Integer, cast, func

from ..models import Lesson

LESSON_LIST_ORDER = (
    Lesson.unit_no,
    func.coalesce(Lesson.sort_order, cast(Lesson.lesson_no, Integer) * 10),
)


def order_lessons_query(query):
    return query.order_by(*LESSON_LIST_ORDER)


def sort_order_for_lesson_no(lesson_no: str, *, fallback: int) -> int:
    s = str(lesson_no or "").strip()
    if s.isdigit():
        return int(s) * 10
    return fallback


def lesson_sort_key(les: Lesson) -> tuple[int, int]:
    unit = int(les.unit_no)
    if les.sort_order is not None:
        return unit, int(les.sort_order)
    no = str(les.lesson_no or "").strip()
    n = int(no) * 10 if no.isdigit() else 99990
    return unit, n


def sort_lessons_in_memory(lessons: list[Lesson]) -> list[Lesson]:
    return sorted(lessons, key=lesson_sort_key)


def ensure_lesson_sort_orders(lessons: list[Lesson]) -> None:
    """为 sort_order 为空的课时按当前列表顺序补序号（步长 10）。"""
    for i, les in enumerate(lessons):
        if les.sort_order is None:
            les.sort_order = (i + 1) * 10
