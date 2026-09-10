"""各课粗分验收状态：供 intake 表展示（来自 annotate 预判断 + 确认课时）。"""
from __future__ import annotations

from sqlalchemy import desc

from ...models import Lesson, LessonContentScan, LessonMatch
from .annotate.content_prescan import _agreement_label
from .annotate.pair_review import (
    PAIR_REVIEW_CONFIRMED,
    PAIR_REVIEW_HEAVY_CHANGE,
    PAIR_REVIEW_NO_OLD,
    PAIR_REVIEW_PENDING,
    get_primary_lesson_match,
    pair_review_status,
)

_PAIR_LABELS = {
    PAIR_REVIEW_PENDING: "待确认对照",
    PAIR_REVIEW_CONFIRMED: "已确认对照",
    PAIR_REVIEW_HEAVY_CHANGE: "同课改动大",
    PAIR_REVIEW_NO_OLD: "无旧课参照",
}


def _latest_scan_by_lesson(lesson_ids: list[str]) -> dict[str, LessonContentScan]:
    if not lesson_ids:
        return {}
    rows = (
        LessonContentScan.query.filter(
            LessonContentScan.new_lesson_id.in_(lesson_ids),
        )
        .order_by(LessonContentScan.new_lesson_id, desc(LessonContentScan.created_at))
        .all()
    )
    out: dict[str, LessonContentScan] = {}
    for row in rows:
        if row.new_lesson_id not in out:
            out[row.new_lesson_id] = row
    return out


def _prescan_match_ready(scan: LessonContentScan | None) -> bool:
    if not scan:
        return False
    step = (scan.result_json or {}).get("prescan_step")
    return scan.status == "done" or step == "match_done"


def _prescan_catalog_ready(scan: LessonContentScan | None) -> bool:
    if not scan:
        return False
    step = (scan.result_json or {}).get("prescan_step")
    return scan.status in ("catalog_done", "done") or step in ("catalog_done", "match_done")


def lesson_validation_dict(
    *,
    lesson_id: str,
    scan: LessonContentScan | None,
    primary_match: LessonMatch | None,
) -> dict:
    pair_status = pair_review_status(primary_match)
    pair_label = _PAIR_LABELS.get(pair_status, pair_status)
    match_ready = _prescan_match_ready(scan)
    catalog_ready = _prescan_catalog_ready(scan)

    agreement = (scan.coarse_agreement or "").strip() if match_ready and scan else None
    agreement_label = _agreement_label(agreement) if agreement else None

    needs_review = False
    validation_stage = "none"
    validation_label = ""

    if pair_status == PAIR_REVIEW_CONFIRMED:
        validation_stage = "confirmed"
        validation_label = pair_label
    elif pair_status == PAIR_REVIEW_HEAVY_CHANGE:
        validation_stage = "heavy_change"
        validation_label = pair_label
    elif pair_status == PAIR_REVIEW_NO_OLD:
        validation_stage = "no_old"
        validation_label = pair_label
    elif match_ready and agreement in ("review_needed", "suggest_swap"):
        validation_stage = "prescan_review"
        validation_label = agreement_label or "建议复核"
        needs_review = pair_status == PAIR_REVIEW_PENDING
    elif match_ready and agreement == "consistent":
        validation_stage = "prescan_ok"
        validation_label = "预判一致·待确认对照" if pair_status == PAIR_REVIEW_PENDING else pair_label
    elif catalog_ready and not match_ready:
        validation_stage = "await_prescan_match"
        validation_label = "待对照预判"
    elif primary_match and pair_status == PAIR_REVIEW_PENDING:
        validation_stage = "await_prescan"
        validation_label = "待 OCR/对照预判"
    elif not primary_match:
        validation_stage = "no_primary"
        validation_label = "无粗分主参照"

    return {
        "pair_review_status": pair_status,
        "pair_review_label": pair_label,
        "prescan_agreement": agreement,
        "prescan_agreement_label": agreement_label,
        "prescan_summary": (scan.summary_text or "").strip() if match_ready and scan else None,
        "prescan_match_ready": match_ready,
        "needs_review": needs_review,
        "validation_stage": validation_stage,
        "validation_label": validation_label,
    }


def lesson_validation_for_lesson(lesson_id: str) -> dict:
    scan = _latest_scan_by_lesson([lesson_id]).get(lesson_id)
    primary = get_primary_lesson_match(lesson_id)
    return lesson_validation_dict(
        lesson_id=lesson_id,
        scan=scan,
        primary_match=primary,
    )


def lesson_validation_map(lessons: list[Lesson]) -> dict[str, dict]:
    lesson_ids = [les.id for les in lessons]
    scans = _latest_scan_by_lesson(lesson_ids)
    out: dict[str, dict] = {}
    for les in lessons:
        primary = get_primary_lesson_match(les.id)
        out[les.lesson_uid] = lesson_validation_dict(
            lesson_id=les.id,
            scan=scans.get(les.id),
            primary_match=primary,
        )
    return out


def summarize_validation(map_by_uid: dict[str, dict]) -> dict:
    counts = {
        "needs_review": 0,
        "await_prescan": 0,
        "prescan_ok_pending": 0,
        "confirmed": 0,
        "other_done": 0,
    }
    for row in map_by_uid.values():
        stage = row.get("validation_stage")
        if row.get("needs_review"):
            counts["needs_review"] += 1
        elif stage in ("await_prescan", "await_prescan_match"):
            counts["await_prescan"] += 1
        elif stage == "prescan_ok":
            counts["prescan_ok_pending"] += 1
        elif stage == "confirmed":
            counts["confirmed"] += 1
        elif stage in ("heavy_change", "no_old"):
            counts["other_done"] += 1
    return {
        "lesson_count": len(map_by_uid),
        **counts,
    }
