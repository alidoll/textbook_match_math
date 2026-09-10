"""将新区块 anchor_old_refs 升格为 block_matches。"""
from __future__ import annotations

from datetime import datetime

from ....extensions import db
from ....models import Block, BlockMatch
from .resolve import get_lesson_match_for_new_lesson
from .workspace import build_compare_workspace


def apply_anchor_matches(
    *,
    new_lesson_uid: str,
    dry_run: bool = False,
    lesson_match_id: str | None = None,
) -> dict:
    lesson_match, old_les, new_les = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    if lesson_match.compare_confirmed_at:
        raise ValueError("本课对比已确认，无法导入锚定")

    old_blocks = {
        b.block_code: b
        for b in Block.query.filter_by(lesson_id=old_les.id).all()
    }
    new_blocks = (
        Block.query.filter_by(lesson_id=new_les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    existing = BlockMatch.query.filter_by(lesson_match_id=lesson_match.id).all()
    matched_old_ids = {m.old_block_id for m in existing if m.old_block_id and m.new_block_id}
    matched_new_ids = {m.new_block_id for m in existing if m.old_block_id and m.new_block_id}
    matched_pairs = {
        (m.old_block_id, m.new_block_id) for m in existing if m.old_block_id and m.new_block_id
    }

    def _drop_orphan_new_row(new_block_id: str, *, keep_id: str | None = None) -> None:
        for m in list(existing):
            if m.new_block_id != new_block_id or m.old_block_id:
                continue
            if keep_id and m.id == keep_id:
                continue
            db.session.delete(m)
            existing.remove(m)

    def _drop_orphan_old_row(old_block_id: str, *, keep_id: str | None = None) -> None:
        for m in list(existing):
            if m.old_block_id != old_block_id or m.new_block_id:
                continue
            if keep_id and m.id == keep_id:
                continue
            db.session.delete(m)
            existing.remove(m)

    created: list[dict] = []
    skipped: list[dict] = []

    for new_block in new_blocks:
        refs = (new_block.metadata_json or {}).get("anchor_old_refs") or []
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            old_code = str(ref.get("old_block_code") or ref.get("block_id") or "").strip()
            ref_lesson = str(ref.get("old_lesson_id") or "").strip()
            if ref_lesson and ref_lesson != old_les.id:
                skipped.append(
                    {
                        "new_block_code": new_block.block_code,
                        "old_block_code": old_code,
                        "reason": "external_old_lesson",
                    }
                )
                continue
            old_block = old_blocks.get(old_code)
            if not old_block:
                skipped.append(
                    {
                        "new_block_code": new_block.block_code,
                        "old_block_code": old_code,
                        "reason": "old_block_not_found",
                    }
                )
                continue
            if (old_block.id, new_block.id) in matched_pairs:
                skipped.append(
                    {
                        "new_block_code": new_block.block_code,
                        "old_block_code": old_code,
                        "reason": "pair_exists",
                    }
                )
                continue

            ref_count = len([r for r in refs if isinstance(r, dict)])
            n1 = new_block.id in matched_new_ids or any(
                m.new_block_id == new_block.id and m.old_block_id for m in existing
            )
            match_type = "1:N" if ref_count > 1 or n1 else "1:1"
            note = (
                f"锚定导入：{ref.get('volume_label') or ''} "
                f"{old_code} {ref.get('old_block_name') or ''}".strip()
            )
            row = {
                "old_block_code": old_code,
                "new_block_code": new_block.block_code,
                "match_type": match_type,
                "teacher_note": note,
            }

            if old_block.id in matched_old_ids and new_block.id in matched_new_ids:
                skipped.append(
                    {
                        "new_block_code": new_block.block_code,
                        "old_block_code": old_code,
                        "reason": "old_already_matched",
                    }
                )
                continue

            orphan_old = next(
                (
                    m
                    for m in existing
                    if m.old_block_id == old_block.id and not m.new_block_id
                ),
                None,
            )
            orphan_new = next(
                (
                    m
                    for m in existing
                    if m.new_block_id == new_block.id and not m.old_block_id
                ),
                None,
            )

            if orphan_old:
                if dry_run:
                    created.append({**row, "merged": "orphan_old"})
                    continue
                orphan_old.new_block_id = new_block.id
                orphan_old.match_type = match_type
                orphan_old.teacher_note = note
                _drop_orphan_new_row(new_block.id, keep_id=orphan_old.id)
                matched_old_ids.add(old_block.id)
                matched_new_ids.add(new_block.id)
                matched_pairs.add((old_block.id, new_block.id))
                created.append({**row, "merged": "orphan_old"})
                continue

            if orphan_new:
                if dry_run:
                    created.append({**row, "merged": "orphan_new"})
                    continue
                orphan_new.old_block_id = old_block.id
                orphan_new.match_type = match_type
                orphan_new.teacher_note = note
                _drop_orphan_old_row(old_block.id, keep_id=orphan_new.id)
                matched_old_ids.add(old_block.id)
                matched_new_ids.add(new_block.id)
                matched_pairs.add((old_block.id, new_block.id))
                created.append({**row, "merged": "orphan_new"})
                continue

            if old_block.id in matched_old_ids:
                skipped.append(
                    {
                        "new_block_code": new_block.block_code,
                        "old_block_code": old_code,
                        "reason": "old_already_matched",
                    }
                )
                continue

            if dry_run:
                created.append(row)
                continue

            bm = BlockMatch(
                lesson_match_id=lesson_match.id,
                old_block_id=old_block.id,
                new_block_id=new_block.id,
                match_type=row["match_type"],
                teacher_note=note,
            )
            db.session.add(bm)
            existing.append(bm)
            matched_old_ids.add(old_block.id)
            matched_new_ids.add(new_block.id)
            matched_pairs.add((old_block.id, new_block.id))
            created.append(row)

    if not dry_run and created:
        db.session.commit()

    out = build_compare_workspace(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match.id
    )
    out.update(
        {
            "dry_run": dry_run,
            "created": created,
            "created_count": len(created),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }
    )
    return out
