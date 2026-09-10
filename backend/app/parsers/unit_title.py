"""从单元标题解析排序序号（绪论、第一单元…）。"""
from __future__ import annotations

import re
from typing import Any

from .catalog_lesson_line import catalog_lesson_sort_key
from .text_norm import norm_text, strip_lesson_seq

_CN_UNIT_NUM = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_RE_UNIT = re.compile(r"第([一二三四五六七八九十百零\d]+)(?:单元|章)")


def cn_unit_token_to_int(token: str) -> int | None:
    """中文/阿拉伯单元号：十二 → 12，二十三 → 23。"""
    t = (token or "").strip()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if t in _CN_UNIT_NUM:
        return _CN_UNIT_NUM[t]
    if t == "十":
        return 10
    if t.startswith("十") and len(t) == 2 and t[1] in _CN_UNIT_NUM:
        return 10 + _CN_UNIT_NUM[t[1]]
    if "十" in t:
        left, _, right = t.partition("十")
        if left and left not in _CN_UNIT_NUM:
            return None
        if right and right not in _CN_UNIT_NUM:
            return None
        tens = _CN_UNIT_NUM[left] if left else 1
        ones = _CN_UNIT_NUM[right] if right else 0
        return tens * 10 + ones
    return None


def unit_no_from_title(unit_title: str) -> int | None:
    """绪论 → 0；第一单元/第十二章 → 1/12；无法识别 → None。"""
    s = (unit_title or "").strip()
    if not s:
        return None
    if s == "绪论" or (s.startswith("绪论") and "单元" not in s and "章" not in s):
        return 0
    m = _RE_UNIT.search(s)
    if not m:
        return None
    return cn_unit_token_to_int(m.group(1))


def sort_catalog_by_unit_and_lesson(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 绪论→第一单元→… 再按节号排序（勿用全局 lesson_no 排序打乱单元）。"""
    indexed = list(enumerate(catalog))

    def key(item: tuple[int, dict[str, Any]]) -> tuple[Any, ...]:
        i, row = item
        unit = str(row.get("unit") or "")
        u = unit_no_from_title(unit)
        if u is None:
            u = 9000 + i
        return (u, *catalog_lesson_sort_key(str(row.get("lesson") or "")), i)

    return [row for _, row in sorted(indexed, key=key)]


def sort_catalog_by_unit_preserving_order(
    catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """只按单元归位，单元内保持目录原文顺序（所见即所得，避免错课号再排乱）。"""
    indexed = list(enumerate(catalog))

    def key(item: tuple[int, dict[str, Any]]) -> tuple[Any, ...]:
        i, row = item
        unit = str(row.get("unit") or "")
        u = unit_no_from_title(unit)
        if u is None:
            u = 9000 + i
        return (u, i)

    return [row for _, row in sorted(indexed, key=key)]


def _lesson_dedup_key(row: dict[str, Any]) -> str:
    return norm_text(strip_lesson_seq(str(row.get("lesson") or "")))


def filter_spurious_xulun_rows(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    去掉误标在「绪论」下、且与正式单元重复的课时。
    若 LLM 把绪论课误并入第一单元，保留 unit=绪论 行并去掉第一单元中的重复行。
    """
    numbered = [
        r
        for r in catalog
        if (unit_no_from_title(str(r.get("unit") or "")) or -1) > 0
    ]
    if not numbered:
        return catalog

    xulun_rows = [
        r for r in catalog if unit_no_from_title(str(r.get("unit") or "")) == 0
    ]
    xulun_keys = {_lesson_dedup_key(r) for r in xulun_rows if _lesson_dedup_key(r)}

    work = list(catalog)
    if xulun_rows and xulun_keys:
        unit1_rows = [
            r
            for r in work
            if unit_no_from_title(str(r.get("unit") or "")) == 1
        ]
        unit1_has_other_lessons = any(
            _lesson_dedup_key(r) not in xulun_keys for r in unit1_rows
        )
        if unit1_has_other_lessons:
            work = [
                r
                for r in work
                if not (
                    unit_no_from_title(str(r.get("unit") or "")) == 1
                    and _lesson_dedup_key(r) in xulun_keys
                )
            ]

    numbered = [
        r
        for r in work
        if (unit_no_from_title(str(r.get("unit") or "")) or -1) > 0
    ]
    numbered_keys = {_lesson_dedup_key(r) for r in numbered if _lesson_dedup_key(r)}

    out = []
    for r in work:
        u = unit_no_from_title(str(r.get("unit") or ""))
        if u == 0:
            lk = _lesson_dedup_key(r)
            if not lk or lk in numbered_keys:
                continue
        out.append(r)

    if not any(unit_no_from_title(str(r.get("unit") or "")) == 0 for r in out):
        return out
    return out
