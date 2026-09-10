"""从 PDF 目录行创建课时（新教材无基准库）。"""
from __future__ import annotations

import re
from typing import Any

from ..lesson_filters import catalog_filter_mode, filter_catalog_rows
from ...parsers.benchmark_xlsx import sort_catalog_rows
from ...parsers.unit_title import unit_no_from_title
from ...extensions import db
from ...models import Lesson, Volume
from ...query.lesson_order import sort_order_for_lesson_no
from ..lesson_delete import delete_all_lessons_for_volume
from ..old_library.edition_registry import EDITIONS
from ..old_library.volume_codes import make_lesson_uid_for_volume, normalize_term

_LESSON_LINE_RE = re.compile(r"^(\d+)\s+(.+)$")


def _edition_for_volume(volume: Volume):
    """按 volumes.edition 标签或别名匹配；小科对照小学科学版本注册表。"""
    from ..old_library.edition_registry import (
        EDITIONS,
        get_edition,
        resolve_edition_id,
    )

    label = (volume.edition or "").strip()
    subject = (volume.subject or "").strip()
    eid = resolve_edition_id(label)
    if eid:
        return get_edition(eid)

    matches = [ed for ed in EDITIONS.values() if ed.label == label]
    if not matches:
        raise ValueError(f"未注册版本：{volume.edition}")
    if subject:
        subj_keys = {subject}
        if subject == "小科":
            subj_keys.add("科学")
        by_subject = [ed for ed in matches if (ed.subject or "").strip() in subj_keys]
        if by_subject:
            return by_subject[0]
    return matches[0]


def _parse_lesson_fields(lesson_raw: str, *, fallback_no: int) -> tuple[str, str]:
    raw = (lesson_raw or "").strip()
    if not raw:
        raise ValueError("空课时名")
    if "单元小结" in raw:
        return "0", "单元小结"
    m = _LESSON_LINE_RE.match(raw)
    if m:
        return m.group(1), m.group(2).strip()
    return str(fallback_no), raw


def bootstrap_lessons_from_catalog(
    *,
    volume: Volume,
    catalog: list[dict[str, Any]],
    replace: bool = False,
) -> int:
    """将 PDF 目录行写入 lessons 表；replace 时先删旧课时。"""
    if not catalog:
        raise ValueError("目录为空")

    catalog = sort_catalog_rows(catalog, subject=volume.subject)
    catalog = filter_catalog_rows(catalog, subject=volume.subject, pipeline="new_library")

    edition = _edition_for_volume(volume)
    existing = Lesson.query.filter_by(volume_id=volume.id).all()
    if existing and not replace:
        return len(existing)

    if existing:
        delete_all_lessons_for_volume(volume)

    unit_no_map: dict[str, int] = {}
    used_unit_nos: set[int] = set()
    lesson_counters: dict[int, int] = {}
    used_lesson_nos: dict[int, set[str]] = {}
    created = 0
    seq = 0

    def _alloc_unit_no(unit_title: str) -> int:
        if unit_title in unit_no_map:
            return unit_no_map[unit_title]
        parsed = unit_no_from_title(unit_title)
        if parsed is not None and parsed not in used_unit_nos:
            unit_no_map[unit_title] = parsed
            used_unit_nos.add(parsed)
            return parsed
        n = 1
        while n in used_unit_nos:
            n += 1
        unit_no_map[unit_title] = n
        used_unit_nos.add(n)
        return n

    def _unique_lesson_no(unit_no: int, preferred: str, *, fallback: int) -> str:
        used = used_lesson_nos.setdefault(unit_no, set())
        cand = (preferred or "").strip() or str(fallback)
        if cand not in used:
            used.add(cand)
            return cand
        # LLM 重复课号（如两个「6」）时改用单元内序号，避免 uk 冲突
        n = fallback
        while str(n) in used:
            n += 1
        used.add(str(n))
        return str(n)

    as_seen = catalog_filter_mode(volume.subject) == "as_seen"

    for row in catalog:
        unit_title = (row.get("unit") or "").strip() or "未命名单元"
        unit_no = _alloc_unit_no(unit_title)
        lesson_counters.setdefault(unit_no, 0)
        lesson_counters[unit_no] += 1
        seq += 1

        preferred_no, lesson_name = _parse_lesson_fields(
            row.get("lesson") or "",
            fallback_no=lesson_counters[unit_no],
        )
        if as_seen:
            # 单元内按目录顺序编号，避免 LLM 全局课号/重复课号
            lesson_no = str(lesson_counters[unit_no])
            raw = (row.get("lesson") or "").strip()
            m = _LESSON_LINE_RE.match(raw)
            if m:
                lesson_name = m.group(2).strip() or lesson_name
            elif raw:
                lesson_name = raw
            within = lesson_counters[unit_no] * 10
        else:
            lesson_no = _unique_lesson_no(
                unit_no,
                preferred_no,
                fallback=lesson_counters[unit_no],
            )
            within = sort_order_for_lesson_no(
                lesson_no, fallback=lesson_counters[unit_no] * 10
            )
        les = Lesson(
            volume_id=volume.id,
            lesson_uid=make_lesson_uid_for_volume(
                edition,
                grade=volume.grade,
                term=volume.semester,
                book_type=volume.book_type,
                unit_no=unit_no,
                lesson_no=lesson_no,
            ),
            unit_no=unit_no,
            unit_title=unit_title,
            lesson_no=lesson_no,
            lesson_name=lesson_name,
            sort_order=int(unit_no) * 1000 + within * 10 + seq,
            slides_fetch_status="not_uploaded",
        )
        db.session.add(les)
        created += 1

    db.session.flush()
    return created


def sync_lesson_unit_and_sort_by_pages(*, volume_id: str) -> int:
    """按单元标题纠正 unit_no，并按已划分页码重排 sort_order（修复课号乱序导致的列表跳页）。"""
    lessons = Lesson.query.filter_by(volume_id=volume_id).all()
    if not lessons:
        return 0

    used: set[int] = set()
    title_to_no: dict[str, int] = {}

    def alloc(unit_title: str) -> int:
        if unit_title in title_to_no:
            return title_to_no[unit_title]
        parsed = unit_no_from_title(unit_title)
        if parsed is not None and parsed not in used:
            title_to_no[unit_title] = parsed
            used.add(parsed)
            return parsed
        n = 1
        while n in used:
            n += 1
        title_to_no[unit_title] = n
        used.add(n)
        return n

    target_unit: dict[str, int] = {}
    for les in lessons:
        title = (les.unit_title or "").strip() or "未命名单元"
        target_unit[les.id] = alloc(title)

    # uk_lessons_volume_unit：(volume_id, unit_no, lesson_no) — 先挪到临时号避免互换冲突
    tmp_base = 9000
    for i, les in enumerate(lessons):
        les.unit_no = tmp_base + i
    db.session.flush()

    for les in lessons:
        les.unit_no = target_unit[les.id]

    with_pages = [les for les in lessons if les.page_start is not None]
    without = [les for les in lessons if les.page_start is None]
    with_pages.sort(
        key=lambda les: (int(les.unit_no), int(les.page_start or 0), les.lesson_uid)
    )
    without.sort(key=lambda les: (int(les.unit_no), str(les.lesson_no), les.lesson_uid))

    seq = 0
    for les in with_pages + without:
        seq += 1
        page_part = int(les.page_start or 0)
        les.sort_order = int(les.unit_no) * 100_000 + page_part * 10 + seq
    return len(lessons)
