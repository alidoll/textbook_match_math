"""新旧对比工作台：按册次分组、状态筛选、课次队列。"""
from __future__ import annotations

from sqlalchemy import desc, func
from sqlalchemy.orm import joinedload

from ...extensions import db
from ...models import Block, Lesson, LessonMatch, Volume
from ...query.lesson_order import order_lessons_query
from ...services.volume_filters import sqlalchemy_exclude_test_volumes

COMPARE_MATCH_TIERS = ("exact", "high_similarity")


def _best_matches_by_lesson(volume_ids: set[str]) -> dict[str, LessonMatch]:
    matches = (
        LessonMatch.query.options(joinedload(LessonMatch.job))
        .filter(LessonMatch.old_lesson_id.isnot(None))
        .filter(LessonMatch.match_tier.in_(COMPARE_MATCH_TIERS))
        .join(Lesson, LessonMatch.new_lesson_id == Lesson.id)
        .filter(Lesson.volume_id.in_(volume_ids))
        .all()
    )
    best: dict[str, LessonMatch] = {}
    for m in matches:
        prev = best.get(m.new_lesson_id)
        if not prev or (m.match_rank or 99) < (prev.match_rank or 99):
            best[m.new_lesson_id] = m
    return best


def _block_counts(lesson_ids: set[str]) -> dict[str, int]:
    if not lesson_ids:
        return {}
    rows = (
        db.session.query(Block.lesson_id, func.count(Block.id))
        .filter(Block.lesson_id.in_(lesson_ids))
        .group_by(Block.lesson_id)
        .all()
    )
    return {lid: int(n) for lid, n in rows}


def _lesson_status(*, block_count: int, confirmed: bool) -> str:
    if confirmed:
        return "confirmed"
    if block_count > 0:
        return "pending"
    return "no_blocks"


def _lesson_row(
    les: Lesson,
    vol: Volume,
    match: LessonMatch,
    block_count: int,
) -> dict:
    confirmed = bool(match.compare_confirmed_at)
    status = _lesson_status(block_count=block_count, confirmed=confirmed)
    return {
        "lesson_uid": les.lesson_uid,
        "lesson_no": les.lesson_no,
        "lesson_name": les.lesson_name,
        "lesson_label": f"{les.lesson_no} {les.lesson_name}",
        "unit_title": les.unit_title or "",
        "volume_code": vol.volume_code,
        "volume_title": vol.display_title or vol.volume_code,
        "old_lesson_hint": match.old_lesson_hint or "—",
        "match_tier": match.match_tier,
        "block_count": block_count,
        "compare_status": status,
        "compare_confirmed": confirmed,
        "compare_confirmed_at": (
            match.compare_confirmed_at.isoformat()
            if match.compare_confirmed_at
            else None
        ),
        "compare_url": f"/new-library/lessons/{les.lesson_uid}/compare",
        "annotate_url": f"/new-library/lessons/{les.lesson_uid}/annotate",
    }


def list_compare_lesson_rows(
    *,
    volume_code: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """扁平课次列表（供 API / 旧模板）。"""
    data = build_compare_workbench(volume_code=volume_code, status=status)
    rows: list[dict] = []
    for vol in data["volumes"]:
        rows.extend(vol["lessons"])
    return rows


def build_compare_workbench(
    *,
    volume_code: str | None = None,
    status: str | None = None,
) -> dict:
    """
    对比工作台数据：按册次分组 + 汇总统计。
    status: pending | confirmed | no_blocks | all
    """
    q = Volume.query.filter_by(book_type="new").filter(
        sqlalchemy_exclude_test_volumes()
    )
    volumes = q.order_by(Volume.grade, Volume.semester, Volume.volume_code).all()
    if not volumes:
        return {
            "ok": True,
            "volumes": [],
            "volume_options": [],
            "summary": {"total": 0, "pending": 0, "confirmed": 0, "no_blocks": 0},
        }

    vol_by_id = {v.id: v for v in volumes}
    vol_ids = set(vol_by_id.keys())
    best_by_lesson = _best_matches_by_lesson(vol_ids)
    if not best_by_lesson:
        return {
            "ok": True,
            "volumes": [],
            "volume_options": [],
            "summary": {"total": 0, "pending": 0, "confirmed": 0, "no_blocks": 0},
        }

    lessons = order_lessons_query(
        Lesson.query.filter(Lesson.id.in_(best_by_lesson.keys()))
    ).all()
    block_counts = _block_counts({les.id for les in lessons})

    by_volume: dict[str, list[dict]] = {v.id: [] for v in volumes}
    all_by_volume: dict[str, list[dict]] = {v.id: [] for v in volumes}
    summary = {"total": 0, "pending": 0, "confirmed": 0, "no_blocks": 0}

    for les in lessons:
        vol = vol_by_id.get(les.volume_id)
        m = best_by_lesson.get(les.id)
        if not vol or not m:
            continue
        bc = block_counts.get(les.id, 0)
        row = _lesson_row(les, vol, m, bc)
        st = row["compare_status"]
        summary["total"] += 1
        summary[st] += 1
        all_by_volume.setdefault(vol.id, []).append(row)
        if status and status != "all" and st != status:
            continue
        by_volume.setdefault(vol.id, []).append(row)

    volume_payloads = []
    for vol in volumes:
        all_rows = all_by_volume.get(vol.id) or []
        lesson_rows = by_volume.get(vol.id) or []
        if status and status != "all" and not lesson_rows:
            continue
        if not all_rows:
            continue
        stats = {"total": 0, "pending": 0, "confirmed": 0, "no_blocks": 0}
        for row in all_rows:
            stats["total"] += 1
            stats[row["compare_status"]] += 1
        volume_payloads.append(
            {
                "volume_code": vol.volume_code,
                "volume_title": vol.display_title or vol.volume_code,
                "grade": vol.grade,
                "semester": vol.semester,
                "stats": stats,
                "lessons": lesson_rows,
            }
        )

    volume_options = [
        {
            "volume_code": v["volume_code"],
            "volume_title": v["volume_title"],
            "stats": v["stats"],
        }
        for v in volume_payloads
    ]
    if volume_code:
        code = volume_code.strip()
        volume_payloads = [v for v in volume_payloads if v["volume_code"] == code]

    return {
        "ok": True,
        "volumes": volume_payloads,
        "volume_options": volume_options,
        "summary": summary,
    }


def list_compare_queue_for_volume(volume_id: str) -> list[Lesson]:
    """同册可对比课次（有粗分配对 + 至少一个新区块），按课序。"""
    best = _best_matches_by_lesson({volume_id})
    if not best:
        return []
    lessons = order_lessons_query(
        Lesson.query.filter(Lesson.id.in_(best.keys()), Lesson.volume_id == volume_id)
    ).all()
    block_counts = _block_counts({les.id for les in lessons})
    return [les for les in lessons if block_counts.get(les.id, 0) > 0]
