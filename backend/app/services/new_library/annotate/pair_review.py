"""建块页课时对照（主参照课 + Step 0 确认）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc

from ....extensions import db
from ....models import Block, Lesson, LessonMatch, Volume
from ...course_match.engine import TIER_LABELS
from ...lesson_lookup import get_lesson_by_uid

PAIR_REVIEW_PENDING = "pending"
PAIR_REVIEW_CONFIRMED = "confirmed"
PAIR_REVIEW_HEAVY_CHANGE = "heavy_change"
PAIR_REVIEW_NO_OLD = "no_old"

VALID_PAIR_REVIEW_STATUSES = frozenset(
    {
        PAIR_REVIEW_PENDING,
        PAIR_REVIEW_CONFIRMED,
        PAIR_REVIEW_HEAVY_CHANGE,
        PAIR_REVIEW_NO_OLD,
    }
)

PRIMARY_TIERS = ("exact", "high_similarity")
CANDIDATE_TIERS = ("exact", "high_similarity", "traceability")


def get_primary_lesson_match(new_lesson_id: str) -> LessonMatch | None:
    return (
        LessonMatch.query.filter_by(new_lesson_id=new_lesson_id)
        .filter(LessonMatch.old_lesson_id.isnot(None))
        .filter(LessonMatch.match_tier.in_(PRIMARY_TIERS))
        .order_by(
            desc(LessonMatch.annotate_primary),
            LessonMatch.match_rank,
            desc(LessonMatch.created_at),
        )
        .first()
    )


def pair_review_status(match: LessonMatch | None) -> str:
    if not match:
        return PAIR_REVIEW_NO_OLD
    status = (match.pair_review_status or "").strip()
    if status in VALID_PAIR_REVIEW_STATUSES:
        return status
    return PAIR_REVIEW_PENDING


def show_primary_old_panel(status: str) -> bool:
    return status in (PAIR_REVIEW_PENDING, PAIR_REVIEW_CONFIRMED, PAIR_REVIEW_HEAVY_CHANGE)


def allows_in_lesson_auto_pairing(status: str) -> bool:
    return status == PAIR_REVIEW_CONFIRMED


def _old_lesson_brief(les: Lesson) -> dict:
    vol = Volume.query.get(les.volume_id) if les.volume_id else None
    return {
        "lesson_uid": les.lesson_uid,
        "lesson_id": les.id,
        "lesson_no": les.lesson_no,
        "lesson_name": les.lesson_name,
        "unit_title": les.unit_title,
        "unit_no": les.unit_no,
        "volume_code": vol.volume_code if vol else None,
        "display_title": vol.display_title if vol else None,
        "page_count": les.page_count,
    }


def list_primary_candidates(*, new_lesson_id: str, job_id: str) -> list[dict]:
    rows = (
        LessonMatch.query.filter_by(new_lesson_id=new_lesson_id, job_id=job_id)
        .filter(LessonMatch.old_lesson_id.isnot(None))
        .filter(LessonMatch.match_tier.in_(CANDIDATE_TIERS))
        .order_by(LessonMatch.match_rank, LessonMatch.created_at)
        .all()
    )
    out: list[dict] = []
    for m in rows:
        old_les = Lesson.query.get(m.old_lesson_id) if m.old_lesson_id else None
        if not old_les:
            continue
        brief = _old_lesson_brief(old_les)
        out.append(
            {
                "lesson_match_id": m.id,
                "match_tier": m.match_tier,
                "match_label": TIER_LABELS.get(m.match_tier, m.match_tier),
                "similarity_score": m.similarity_score,
                "match_rank": m.match_rank,
                "annotate_primary": bool(m.annotate_primary),
                "old_lesson_hint": m.old_lesson_hint,
                **brief,
            }
        )
    return out


def collect_referenced_old_lessons(*, new_lesson_id: str) -> list[dict]:
    """从新区块锚定汇总跨课来源（块级多源）。"""
    blocks = Block.query.filter_by(lesson_id=new_lesson_id).all()
    primary = get_primary_lesson_match(new_lesson_id)
    primary_old_id = primary.old_lesson_id if primary else None
    by_lesson: dict[str, dict] = {}
    for block in blocks:
        refs = (block.metadata_json or {}).get("anchor_old_refs") or []
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            old_id = str(ref.get("old_lesson_id") or "").strip()
            if not old_id:
                continue
            entry = by_lesson.setdefault(
                old_id,
                {
                    "old_lesson_id": old_id,
                    "old_lesson_uid": ref.get("old_lesson_uid") or "",
                    "lesson_name": ref.get("lesson_name") or "",
                    "unit_title": ref.get("unit_title") or "",
                    "volume_label": ref.get("volume_label") or "",
                    "block_codes": [],
                    "is_primary": old_id == str(primary_old_id or ""),
                },
            )
            code = ref.get("old_block_code")
            if code and code not in entry["block_codes"]:
                entry["block_codes"].append(code)
    if primary_old_id and str(primary_old_id) not in by_lesson:
        old_les = Lesson.query.get(primary_old_id)
        if old_les:
            brief = _old_lesson_brief(old_les)
            by_lesson[str(primary_old_id)] = {
                **brief,
                "old_lesson_id": old_les.id,
                "block_codes": [],
                "is_primary": True,
            }
    return sorted(
        by_lesson.values(),
        key=lambda x: (not x.get("is_primary"), x.get("lesson_name") or ""),
    )


def build_pair_review_context(*, new_lesson_id: str) -> dict:
    match = get_primary_lesson_match(new_lesson_id)
    if not match:
        return {
            "status": PAIR_REVIEW_NO_OLD,
            "status_label": "无粗分旧课",
            "reviewed_at": None,
            "lesson_match_id": None,
            "show_old_panel": False,
            "allows_in_lesson_auto": False,
            "candidates": [],
            "referenced_old_lessons": collect_referenced_old_lessons(
                new_lesson_id=new_lesson_id
            ),
            "hint": "本课粗分为「无匹配」，请在新教材上独立建块；跨课旧块可用「跨课找旧块」。",
        }

    status = pair_review_status(match)
    new_les = Lesson.query.get(new_lesson_id)
    candidates = list_primary_candidates(new_lesson_id=new_lesson_id, job_id=match.job_id)
    old_les = Lesson.query.get(match.old_lesson_id) if match.old_lesson_id else None

    page_hint = ""
    if new_les and old_les:
        np = new_les.page_count or 0
        op = old_les.page_count or 0
        if np and op and abs(np - op) >= 2:
            page_hint = f"页数差较大（新 {np} / 旧 {op}），内容可能重组，宜选「同课改动大」。"

    status_labels = {
        PAIR_REVIEW_PENDING: "待确认对照",
        PAIR_REVIEW_CONFIRMED: "已确认对照",
        PAIR_REVIEW_HEAVY_CHANGE: "同课改动大",
        PAIR_REVIEW_NO_OLD: "不使用旧课参照",
    }

    hints = {
        PAIR_REVIEW_PENDING: (
            "左栏为<strong>主参照旧课</strong>（非唯一来源）。请翻页对照："
            "主题一致点「确认对照」；改动大点「同课改动大」；配错点「无对应/配错」。"
            "单块来自其他旧课请用「跨课找旧块」。"
        ),
        PAIR_REVIEW_CONFIRMED: "已确认主参照课，可使用课内「AI 建整课」。跨课区块仍请用「跨课找旧块」。",
        PAIR_REVIEW_HEAVY_CHANGE: (
            "已标记改动较大：请在新教材上手动建块，勿依赖课内一键建块；"
            "需要时用「跨课找旧块」按正文找来源。"
        ),
        PAIR_REVIEW_NO_OLD: "已隐藏主参照旧课，请独立建块；跨课检索仍可用于个别块的来源标注。",
    }

    return {
        "status": status,
        "status_label": status_labels.get(status, status),
        "reviewed_at": (
            match.pair_reviewed_at.isoformat() if match.pair_reviewed_at else None
        ),
        "lesson_match_id": match.id,
        "match_tier": match.match_tier,
        "match_label": TIER_LABELS.get(match.match_tier, match.match_tier),
        "similarity_score": match.similarity_score,
        "show_old_panel": show_primary_old_panel(status),
        "allows_in_lesson_auto": allows_in_lesson_auto_pairing(status),
        "primary_old_lesson": _old_lesson_brief(old_les) if old_les else None,
        "candidates": candidates,
        "referenced_old_lessons": collect_referenced_old_lessons(
            new_lesson_id=new_lesson_id
        ),
        "page_mismatch_hint": page_hint,
        "hint": hints.get(status, hints[PAIR_REVIEW_PENDING]),
    }


def set_pair_review(*, lesson_uid: str, status: str) -> dict:
    status = (status or "").strip()
    if status not in VALID_PAIR_REVIEW_STATUSES:
        raise ValueError(f"无效对照状态：{status}")

    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    match = get_primary_lesson_match(new_les.id)
    if not match and status != PAIR_REVIEW_NO_OLD:
        raise ValueError("本课尚无粗分旧课配对")

    if match:
        match.pair_review_status = status
        match.pair_reviewed_at = datetime.utcnow()
        db.session.commit()

    from .workspace import build_new_annotate_workspace

    return build_new_annotate_workspace(lesson_uid=lesson_uid)


def swap_primary_old_lesson(*, lesson_uid: str, old_lesson_uid: str) -> dict:
    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    old_les = get_lesson_by_uid(old_lesson_uid, book_type="old")
    match = get_primary_lesson_match(new_les.id)
    if not match:
        raise ValueError("本课尚无粗分配对")

    target = (
        LessonMatch.query.filter_by(
            job_id=match.job_id,
            new_lesson_id=new_les.id,
            old_lesson_id=old_les.id,
        )
        .filter(LessonMatch.match_tier.in_(CANDIDATE_TIERS))
        .first()
    )
    if not target:
        raise ValueError("所选旧课不在粗分候选中")

    siblings = LessonMatch.query.filter_by(
        job_id=match.job_id, new_lesson_id=new_les.id
    ).all()
    for row in siblings:
        row.annotate_primary = row.id == target.id
        if row.id == target.id:
            row.pair_review_status = PAIR_REVIEW_PENDING
            row.pair_reviewed_at = None

    db.session.commit()

    from .workspace import build_new_annotate_workspace

    return build_new_annotate_workspace(lesson_uid=lesson_uid)
