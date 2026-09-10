"""课时行过滤（解析 / 导入用）。"""
from __future__ import annotations

import re

from ..models import Lesson
from ..parsers.benchmark_xlsx import BenchmarkLessonRow


def is_unit_summary_label(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    return s == "单元小结" or "单元小结" in s


def is_unit_summary_lesson(lesson: Lesson) -> bool:
    return is_unit_summary_label(lesson.lesson_name or "")


def is_unit_summary_benchmark_row(row: BenchmarkLessonRow) -> bool:
    return is_unit_summary_label(row.lesson_name) or is_unit_summary_label(row.lesson_raw)


def is_master_class_unit(unit_title: str) -> bool:
    return "大师课" in (unit_title or "").strip()


def is_master_class_lesson(lesson: Lesson) -> bool:
    return is_master_class_unit(lesson.unit_title or "")


def is_master_class_benchmark_row(row: BenchmarkLessonRow) -> bool:
    return is_master_class_unit(row.unit_title)


def filter_benchmark_rows(rows: list[BenchmarkLessonRow]) -> list[BenchmarkLessonRow]:
    return [r for r in rows if not is_unit_summary_benchmark_row(r)]


def filter_master_class_benchmark_rows(
    rows: list[BenchmarkLessonRow],
) -> list[BenchmarkLessonRow]:
    return [r for r in rows if not is_master_class_benchmark_row(r)]


def filter_master_class_lessons(lessons: list[Lesson]) -> list[Lesson]:
    return [les for les in lessons if not is_master_class_lesson(les)]


def is_subtitle_lesson_no(lesson_no: str | None, *, subject: str | None = None) -> bool:
    """主课挂接的副标题课号，如语文 8.1、习作.2（粗分应跳过）。

    数学课号本身就是「12.1」形态，不得当副标题过滤。
    """
    s = (lesson_no or "").strip()
    if not s:
        return False
    sub = (subject or "").strip()
    if sub and sub not in ("语文", "Test"):
        return False
    return bool(re.search(r"\.\d+$", s))


def is_subtitle_lesson(lesson: Lesson, *, subject: str | None = None) -> bool:
    sub = subject
    if sub is None:
        vol = getattr(lesson, "volume", None)
        if vol is not None:
            sub = getattr(vol, "subject", None)
    return is_subtitle_lesson_no(lesson.lesson_no, subject=sub)


def is_catalog_noise_line(text: str) -> bool:
    """明显非目录行（前言/版权等）；所见即所得模式仅剔这类噪声。"""
    t = (text or "").strip()
    if not t:
        return True
    noise = (
        "前言",
        "编者的话",
        "编者说明",
        "版权",
        "CIP",
        "ISBN",
        "定价",
        "责任编辑",
        "义务教育教科书",
        "绿色印刷",
    )
    compact = re.sub(r"\s+", "", t)
    if any(n in compact for n in noise):
        return True
    if compact.startswith("出版") and len(compact) < 24:
        return True
    return False


def catalog_filter_mode(
    subject: str | None = None,
    *,
    pipeline: str = "default",
) -> str:
    """按学科选择过滤策略；已调优学科走专用规则，其余默认所见即所得。

    化学与科学/数学一样走所见即所得：目录上有的课时行原样保留，
    不再按「课题/整理/实验」白名单严格过滤。
    pipeline 仅作入口标注；学科规则以 subject 为准。
    """
    sub = (subject or "").strip()
    _ = (pipeline or "default").strip()  # 入口隔离保留参数，策略以学科为准

    if sub in ("语文", "Test"):
        return "yuwen"
    # 化学 / 科学 / 数学 / 未单独调优：所见即所得（仅剔噪声与单元小结）
    return "as_seen"


def filter_catalog_rows(
    catalog: list[dict],
    *,
    keep_yuwen_subtitles: bool = False,
    subject: str | None = None,
    pipeline: str = "default",
) -> list[dict]:
    from ..parsers.catalog_lesson_line import (
        is_catalog_lesson_line,
        is_yuwen_subtitle_line,
        looks_like_exercise_line,
    )

    mode = catalog_filter_mode(subject, pipeline=pipeline)
    if keep_yuwen_subtitles or mode == "yuwen":
        mode = "yuwen"

    out: list[dict] = []
    for row in catalog:
        lesson = str(row.get("lesson") or "")
        if mode == "as_seen":
            if is_catalog_noise_line(lesson):
                continue
            # 单元小结仍跳过（非对比主路径）；其余目录行原样保留
            if is_unit_summary_label(lesson):
                continue
            out.append(dict(row))
            continue

        if is_unit_summary_label(lesson):
            continue
        if looks_like_exercise_line(lesson):
            continue
        if is_catalog_lesson_line(lesson):
            out.append(dict(row))
            continue
        if mode == "yuwen" and is_yuwen_subtitle_line(lesson):
            item = dict(row)
            item["_subtitle"] = True
            out.append(item)
            continue
    return out
