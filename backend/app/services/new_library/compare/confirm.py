"""教研确认 compare-v2。"""
from __future__ import annotations

from datetime import datetime

from ....extensions import db
from ....models import BlockMatch, ReuseReport
from .export import refresh_compare_artifacts
from .resolve import get_lesson_match_for_new_lesson
from .workspace import build_compare_workspace


def confirm_compare(
    *,
    new_lesson_uid: str,
    confirmed_by: str | None = None,
    lesson_match_id: str | None = None,
    force: bool = False,
) -> dict:
    lesson_match, _, _ = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    if lesson_match.compare_confirmed_at and not force:
        raise ValueError("本课对比已确认")

    matches = BlockMatch.query.filter_by(lesson_match_id=lesson_match.id).all()
    if not matches:
        raise ValueError("尚无区块配对，无法确认")

    pending = [
        m
        for m in matches
        if not (m.reuse_action or "").strip() or not (m.teacher_note or "").strip()
    ]
    if pending and not force:
        raise ValueError(
            f"还有 {len(pending)} 条配对未填写复用判定或教研说明"
        )

    lesson_match.compare_confirmed_at = datetime.utcnow()
    lesson_match.compare_confirmed_by = (confirmed_by or "教研").strip() or "教研"
    db.session.commit()

    artifacts = refresh_compare_artifacts(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match.id
    )
    data = build_compare_workspace(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match.id
    )
    data["reuse_report"] = artifacts.get("reuse_report")
    data["export_path"] = artifacts.get("export_path")
    return data


def unlock_compare(
    *,
    new_lesson_uid: str,
    lesson_match_id: str | None = None,
) -> dict:
    lesson_match, _, _ = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    lesson_match.compare_confirmed_at = None
    lesson_match.compare_confirmed_by = None
    db.session.commit()
    ReuseReport.query.filter_by(match_id=lesson_match.id).delete()
    db.session.commit()
    return build_compare_workspace(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match.id
    )
