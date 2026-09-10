"""区块配对 CRUD。"""
from __future__ import annotations

from datetime import datetime

from ....extensions import db
from ....models import Block, BlockMatch, LessonMatch
from .resolve import get_lesson_match_for_new_lesson
from .workspace import build_compare_workspace


def _ensure_editable(match: LessonMatch) -> None:
    if match.compare_confirmed_at:
        raise ValueError("本课对比已教研确认，无法修改。如需调整请先解锁。")


def _block_by_code(lesson_id: str, block_code: str | None) -> Block | None:
    code = (block_code or "").strip()
    if not code:
        return None
    return Block.query.filter_by(lesson_id=lesson_id, block_code=code).first()


def _normalize_reuse_action(value: str | None) -> str | None:
    from ....services.dictionary import list_dictionary_entries

    text = (value or "").strip()
    if not text:
        return None
    for row in list_dictionary_entries(category="reuse_action"):
        if text in (row["code"], row["label"]):
            return row["code"]
    return text


def _normalize_change_type(value: str | None) -> str | None:
    from ....services.dictionary import list_dictionary_entries

    text = (value or "").strip()
    if not text:
        return None
    for row in list_dictionary_entries(category="change_type"):
        if text in (row["code"], row["label"]):
            return row["code"]
    return text


def create_match(
    *,
    new_lesson_uid: str,
    old_block_code: str | None = None,
    new_block_code: str | None = None,
    match_type: str = "1:1",
    reuse_action: str | None = None,
    change_type: str | None = None,
    teacher_note: str | None = None,
    lesson_match_id: str | None = None,
) -> dict:
    lesson_match, old_les, new_les = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    _ensure_editable(lesson_match)

    old_block = _block_by_code(old_les.id, old_block_code)
    new_block = _block_by_code(new_les.id, new_block_code)
    if not old_block and not new_block:
        raise ValueError("请至少指定旧区块或新区块")

    existing = BlockMatch.query.filter_by(lesson_match_id=lesson_match.id).all()
    if old_block and any(m.old_block_id == old_block.id for m in existing):
        raise ValueError(f"旧区块 {old_block.block_code} 已有配对")

    if new_block and any(m.new_block_id == new_block.id for m in existing):
        resolved_type = "N:1" if match_type != "1:N" else match_type
    else:
        resolved_type = match_type or "1:1"

    bm = BlockMatch(
        lesson_match_id=lesson_match.id,
        old_block_id=old_block.id if old_block else None,
        new_block_id=new_block.id if new_block else None,
        match_type=resolved_type,
        reuse_action=_normalize_reuse_action(reuse_action),
        change_type=_normalize_change_type(change_type),
        teacher_note=(teacher_note or "").strip() or None,
    )
    db.session.add(bm)
    db.session.commit()
    return build_compare_workspace(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match.id
    )


def update_match(
    *,
    new_lesson_uid: str,
    match_id: str,
    fields: dict,
    lesson_match_id: str | None = None,
) -> dict:
    lesson_match, old_les, new_les = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    _ensure_editable(lesson_match)

    bm = BlockMatch.query.filter_by(
        id=match_id, lesson_match_id=lesson_match.id
    ).first()
    if not bm:
        raise ValueError("未找到该配对")

    if "old_block_code" in fields or "old_block_id" in fields:
        code = fields.get("old_block_code") or fields.get("old_block_id")
        old_block = _block_by_code(old_les.id, code)
        bm.old_block_id = old_block.id if old_block else None
    if "new_block_code" in fields or "new_block_id" in fields:
        code = fields.get("new_block_code") or fields.get("new_block_id")
        new_block = _block_by_code(new_les.id, code)
        bm.new_block_id = new_block.id if new_block else None
    if "match_type" in fields:
        bm.match_type = str(fields["match_type"] or "1:1")
    if "reuse_action" in fields:
        bm.reuse_action = _normalize_reuse_action(fields.get("reuse_action"))
    if "change_type" in fields:
        bm.change_type = _normalize_change_type(fields.get("change_type"))
    if "teacher_note" in fields:
        note = str(fields.get("teacher_note") or "").strip()
        bm.teacher_note = note or None
    if "feedback" in fields:
        meta = dict(bm.metadata_json or {})
        fb = fields.get("feedback")
        if isinstance(fb, dict) and fb:
            from datetime import datetime as dt

            meta["teacher_feedback"] = {
                **fb,
                "recorded_at": dt.utcnow().isoformat(),
            }
        bm.metadata_json = meta

    bm.updated_at = datetime.utcnow()
    db.session.commit()
    return build_compare_workspace(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match.id
    )


def delete_match(
    *,
    new_lesson_uid: str,
    match_id: str,
    lesson_match_id: str | None = None,
) -> dict:
    lesson_match, _, _ = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    _ensure_editable(lesson_match)

    bm = BlockMatch.query.filter_by(
        id=match_id, lesson_match_id=lesson_match.id
    ).first()
    if not bm:
        raise ValueError("未找到该配对")
    db.session.delete(bm)
    db.session.commit()
    return build_compare_workspace(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match.id
    )
