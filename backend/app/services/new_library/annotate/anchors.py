"""新区块锚定旧区块（metadata_json.anchor_old_refs）。"""
from __future__ import annotations

from ....models import Block, Lesson, Volume
from .pair_review import get_primary_lesson_match


def build_anchor_old_ref(
    *,
    new_lesson_id: str,
    old_block_code: str,
    old_page_index: int | None = None,
) -> dict | None:
    code = str(old_block_code or "").strip()
    if not code:
        return None
    match = get_primary_lesson_match(new_lesson_id)
    if not match or not match.old_lesson_id:
        return None
    old_les = Lesson.query.get(match.old_lesson_id)
    if not old_les:
        return None
    old_block = Block.query.filter_by(
        lesson_id=old_les.id, block_code=code
    ).first()
    if not old_block:
        return None
    old_vol = Volume.query.get(old_les.volume_id)
    return {
        "old_lesson_id": old_les.id,
        "old_lesson_uid": old_les.lesson_uid,
        "old_block_code": old_block.block_code,
        "old_block_name": old_block.block_name,
        "old_page_index": old_page_index,
        "volume_label": old_vol.display_title if old_vol else "",
        "unit_title": old_les.unit_title or "",
        "lesson_name": old_les.lesson_name or "",
        "corpus_key": f"{old_les.id}:{old_block.block_code}",
        "cw_pgs": sorted(int(x) for x in (old_block.course_slide_indices or [])),
    }


def corpus_row_to_anchor_ref(row: dict) -> dict:
    """将锚定推荐结果转为 metadata.anchor_old_refs 条目。"""
    old_pgs = row.get("old_tb_pgs") or []
    page = int(old_pgs[0]) if old_pgs else row.get("old_page_index")
    lesson_id = str(row.get("lesson_id") or "")
    block_code = str(row.get("block_code") or row.get("block_id") or "")
    return {
        "old_lesson_id": lesson_id,
        "old_lesson_uid": row.get("lesson_uid") or "",
        "old_block_code": block_code,
        "old_block_name": row.get("block_name") or block_code,
        "old_page_index": page,
        "volume_label": row.get("volume_label") or "",
        "unit_title": row.get("unit_title") or "",
        "lesson_name": row.get("lesson_name") or "",
        "corpus_key": row.get("corpus_key") or f"{lesson_id}:{block_code}",
        "cw_pgs": row.get("cw_pgs") or [],
    }


def apply_anchor_to_metadata(
    *,
    lesson_id: str,
    metadata_json: dict | None,
    anchor_old_block_code: str | None,
    old_page_index: int | None = None,
) -> dict | None:
    code = str(anchor_old_block_code or "").strip()
    if not code:
        return metadata_json
    ref = build_anchor_old_ref(
        new_lesson_id=lesson_id,
        old_block_code=code,
        old_page_index=old_page_index,
    )
    if not ref:
        raise ValueError(f"未找到粗分旧区块 {code}，无法锚定")
    meta = dict(metadata_json or {})
    meta["anchor_old_refs"] = [ref]
    return meta


def clear_anchor_from_metadata(metadata_json: dict | None) -> dict | None:
    meta = dict(metadata_json or {})
    meta.pop("anchor_old_refs", None)
    return meta or None
