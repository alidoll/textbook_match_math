"""有序多锚定：一个新区块 ↔ 多个连续旧区块（方案 1）+ 原子级 1:1 汇总（方案 2）。"""
from __future__ import annotations

from typing import Any

from ....models import Block, TextbookAtom
from ..match_groups import anchor_ref_with_role, old_codes_contiguous
from ...lesson_lookup import get_lesson_by_uid
from .anchors import build_anchor_old_ref
from .pair_review import get_primary_lesson_match
from .seed_from_old_page import (
    _atom_plain_text,
    _profile_block_match_score,
    _score_new_atom_for_old_block,
)


def old_refs_are_contiguous(
    refs: list[dict[str, Any]],
    *,
    old_blocks_by_code: dict[str, Block],
) -> bool:
    codes = [
        str(ref.get("old_block_code") or "").strip()
        for ref in refs
        if ref.get("old_block_code")
    ]
    return old_codes_contiguous(codes, blocks_by_code=old_blocks_by_code)


def combined_match_score_for_refs(
    atom_codes: list[str],
    refs: list[dict[str, Any]],
    *,
    new_atoms_by_code: dict[str, TextbookAtom],
    old_profiles: dict[str, dict],
) -> float:
    """多旧块并集：各 ref 对应 profile 得分取 max（近似并集重合）。"""
    scores: list[float] = []
    for ref in refs:
        code = str(ref.get("old_block_code") or "").strip()
        prof = old_profiles.get(code)
        if not prof:
            continue
        scores.append(
            _profile_block_match_score(atom_codes, prof, new_atoms_by_code)
        )
    return max(scores) if scores else 0.0


def _atom_best_old_block(
    atom: TextbookAtom,
    old_profiles: list[dict],
) -> tuple[str, float]:
    best_code = ""
    best_score = 0.0
    for prof in old_profiles:
        if prof.get("skip_reason"):
            continue
        ob = prof["block"]
        score = _score_new_atom_for_old_block(
            atom,
            query_text=prof["query"],
            old_y_mid=prof["y_mid"],
        )
        if score > best_score:
            best_score = score
            best_code = ob.block_code
    return best_code, best_score


def enrich_lesson_ordered_multi_anchors(
    *,
    lesson_uid: str,
    min_atom_score: float = 0.08,
) -> dict[str, Any]:
    """
    方案 2 + 1：原子级 1:1 匹配旧块，区块层汇总为有序 anchor_old_refs（主/辅）。
    """
    from ...old_library.annotate.blocks import _ensure_blocks_editable
    from .seed_from_old_page import _block_query_and_ymid, _primary_old_lesson

    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)
    paired = _primary_old_lesson(new_les.id)
    if not paired:
        return {"updated_blocks": 0}
    old_les, _ = paired

    old_atoms_by_code = {
        a.atom_code: a
        for a in TextbookAtom.query.filter_by(lesson_id=old_les.id).all()
    }
    new_atoms_by_code = {
        a.atom_code: a
        for a in TextbookAtom.query.filter_by(lesson_id=new_les.id).all()
    }
    old_blocks = (
        Block.query.filter_by(lesson_id=old_les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    old_blocks_by_code = {b.block_code: b for b in old_blocks}
    old_profiles_list: list[dict] = []
    old_profiles_map: dict[str, dict] = {}
    for ob in old_blocks:
        query, y_mid = _block_query_and_ymid(ob, old_atoms_by_code)
        prof = {"block": ob, "query": query, "y_mid": y_mid, "skip_reason": ""}
        old_profiles_list.append(prof)
        old_profiles_map[ob.block_code] = prof

    updated = 0
    blocks = Block.query.filter_by(lesson_id=new_les.id).order_by(Block.sort_order).all()
    for block in blocks:
        codes = list(block.atom_codes or [])
        if not codes:
            continue
        ordered_old: list[str] = []
        seen_old: set[str] = set()
        for code in codes:
            atom = new_atoms_by_code.get(code)
            if not atom:
                continue
            old_code, score = _atom_best_old_block(atom, old_profiles_list)
            if not old_code or score < min_atom_score:
                continue
            meta = dict(atom.metadata_json or {})
            seg = dict(meta.get("anchor_segment") or {})
            seg["old_block_code"] = old_code
            seg["match_score"] = round(score, 4)
            meta["anchor_segment"] = seg
            atom.metadata_json = meta
            if old_code not in seen_old:
                ordered_old.append(old_code)
                seen_old.add(old_code)

        if not ordered_old:
            continue

        refs: list[dict] = []
        for i, old_code in enumerate(ordered_old):
            ref = build_anchor_old_ref(
                new_lesson_id=new_les.id,
                old_block_code=old_code,
                old_page_index=(
                    old_blocks_by_code[old_code].textbook_page_start
                    if old_code in old_blocks_by_code
                    else None
                ),
            )
            if not ref:
                continue
            role = "primary" if i == 0 else "secondary"
            refs.append(anchor_ref_with_role(ref, role=role))

        if not refs:
            continue
        if len(refs) > 1 and not old_refs_are_contiguous(
            refs, old_blocks_by_code=old_blocks_by_code
        ):
            refs = refs[:1]

        bmeta = dict(block.metadata_json or {})
        bmeta["anchor_old_refs"] = refs
        bmeta["anchor_match_mode"] = (
            "ordered_multi" if len(refs) > 1 else "single"
        )
        atom_codes_all = list(block.atom_codes or [])
        score = combined_match_score_for_refs(
            atom_codes_all,
            refs,
            new_atoms_by_code=new_atoms_by_code,
            old_profiles=old_profiles_map,
        )
        bmeta["match_score"] = round(score, 4)
        block.metadata_json = bmeta
        updated += 1

    return {"updated_blocks": updated, "lesson_uid": lesson_uid}
