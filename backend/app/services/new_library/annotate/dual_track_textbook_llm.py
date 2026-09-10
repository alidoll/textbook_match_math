"""双轨实验 · 教材轨豆包按页分配（dry-run，不写库）。"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from ....models import TextbookAtom
from ...llm.config import dual_track_textbook_llm_enabled, llm_new_block_mirror_model
from ...llm.new_block_mirror_suggest import suggest_mirror_assignments_with_llm
from ..block_pipeline_prepare import ensure_page_prepare, format_prepare_hint, get_page_prepare
from .ai_seed_from_old_page import _atom_text_for_llm, _old_block_atoms_on_page, _page_blob
from .dual_track_experiment import (
    _build_borderline_followup,
    _build_track_textbook,
    _build_unassigned_followup,
    _candidate_blocks_from_assignments,
    _has_textbook_content,
    _split_block_name_sides,
    _textbook_query_for_block,
)
from .seed_from_old_page import (
    _block_touches_page,
    _build_rule_based_assignments,
    _dedupe_atom_assignments,
    _image_label_text,
    _is_image_like_atom,
    _score_new_atom_for_old_block,
    _suggest_new_block_name_from_old,
)

logger = logging.getLogger(__name__)


def _lesson_textbook_profiles(ctx: dict[str, Any]) -> list[dict]:
    """全课旧块教材侧 profile（含 Block 对象，供去重/规则回退）。"""
    old_blocks = ctx["old_blocks"]
    old_atoms_by_code = ctx["old_atoms_by_code"]
    profiles: list[dict] = []
    for ob in old_blocks:
        if not _has_textbook_content(ob, old_atoms_by_code):
            continue
        query, y_mid = _textbook_query_for_block(
            ob,
            old_atoms_by_code,
            old_lesson_uid=str(ctx["old_les"].lesson_uid),
            prefer_doubao=True,
        )
        if not query:
            continue
        _, tb_name = _split_block_name_sides(ob.block_name or "")
        profiles.append(
            {
                "block": ob,
                "block_code": ob.block_code,
                "query": query,
                "y_mid": y_mid,
                "label": tb_name or ob.block_name,
                "skip_reason": "",
            }
        )
    return profiles


def _profiles_on_page(
    profiles: list[dict],
    *,
    old_page_index: int,
    old_atoms_by_code: dict[str, TextbookAtom],
) -> list[dict]:
    out: list[dict] = []
    for prof in profiles:
        ob = prof["block"]
        if _block_touches_page(ob, old_page_index, old_atoms_by_code):
            out.append(prof)
    return out


def _mirror_old_blocks_payload(
    profiles: list[dict],
    *,
    old_page_index: int,
    old_atoms_by_code: dict[str, TextbookAtom],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for prof in profiles:
        ob = prof["block"]
        payload.append(
            {
                "block_code": ob.block_code,
                "block_name": prof.get("label") or ob.block_name,
                "skip_reason": prof.get("skip_reason") or "",
                "query_excerpt": (prof.get("query") or "")[:320],
                "atoms": _old_block_atoms_on_page(
                    ob, old_page_index, old_atoms_by_code
                ),
            }
        )
    return payload


def _new_atoms_payload(atoms: list[TextbookAtom]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for atom in atoms:
        label = _image_label_text(atom) if _is_image_like_atom(atom) else ""
        out.append(
            {
                "atom_code": atom.atom_code,
                "atom_type": (atom.atom_type or "text").strip().lower(),
                "text": _atom_text_for_llm(atom),
                "image_label": label,
            }
        )
    return out


def _atom_scores_from_assignments(
    new_atoms: list[TextbookAtom],
    profiles: list[dict],
) -> dict[str, list[dict]]:
    from collections import defaultdict as dd

    atom_scores: dict[str, list[dict]] = dd(list)
    for atom in new_atoms:
        for prof in profiles:
            score = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof.get("y_mid"),
            )
            if score > 0:
                atom_scores[atom.atom_code].append(
                    {
                        "source": prof["block_code"],
                        "score": round(score, 4),
                    }
                )
        atom_scores[atom.atom_code].sort(key=lambda x: -x["score"])
    return dict(atom_scores)


def build_track_textbook_with_llm(
    ctx: dict[str, Any],
    *,
    min_score: float,
) -> dict[str, Any]:
    """
    教材轨：按新教材页调用豆包 mirror 分配；单页失败回退规则；全课去重。
  返回结构与 _build_track_textbook 一致，并附加 ranker / llm_pages 等字段。
    """
    rule_track = _build_track_textbook(ctx, min_score=min_score)
    if not dual_track_textbook_llm_enabled():
        rule_track["ranker"] = "rules"
        rule_track["llm_note"] = "豆包未配置或未启用（DUAL_TRACK_LLM / NEW_BLOCK_MIRROR_LLM）"
        return rule_track

    new_les = ctx["new_les"]
    old_les = ctx["old_les"]
    new_atoms = ctx["new_atoms"]
    new_atoms_by_code = ctx["new_atoms_by_code"]
    old_atoms_by_code = ctx["old_atoms_by_code"]

    all_profiles = _lesson_textbook_profiles(ctx)
    if not all_profiles:
        rule_track["ranker"] = "rules"
        rule_track["llm_note"] = "无教材侧旧块参照，已用规则轨"
        return rule_track

    page_indices = sorted({int(a.page_index) for a in new_atoms})
    raw_assignments: dict[str, list[str]] = defaultdict(list)
    llm_pages: list[dict[str, Any]] = []
    llm_warnings: list[str] = []
    pages_llm_ok = 0
    pages_rule_fallback = 0

    for new_page in page_indices:
        old_page = int(new_page)
        page_atoms = [a for a in new_atoms if int(a.page_index) == new_page]
        if not page_atoms:
            continue

        page_profiles = _profiles_on_page(
            all_profiles,
            old_page_index=old_page,
            old_atoms_by_code=old_atoms_by_code,
        )
        page_entry: dict[str, Any] = {
            "new_page_index": new_page,
            "old_page_index": old_page,
            "atom_count": len(page_atoms),
            "old_block_count": len(page_profiles),
            "source": "skipped",
        }

        if not page_profiles:
            page_entry["source"] = "no_old_blocks"
            llm_pages.append(page_entry)
            continue

        ensure_page_prepare(
            lesson_uid=new_les.lesson_uid,
            lesson_id=new_les.id,
            page_index=new_page,
            lesson_name=new_les.lesson_name or "",
        )
        prepare_hint = format_prepare_hint(
            get_page_prepare(new_les.lesson_uid, new_page)
        )

        page_assignments: dict[str, list[str]] | None = None
        page_source = "doubao"

        try:
            result = suggest_mirror_assignments_with_llm(
                lesson_name=new_les.lesson_name or old_les.lesson_name or "",
                unit_title=new_les.unit_title or old_les.unit_title or "",
                old_page_index=old_page,
                new_page_index=new_page,
                old_blocks=_mirror_old_blocks_payload(
                    page_profiles,
                    old_page_index=old_page,
                    old_atoms_by_code=old_atoms_by_code,
                ),
                new_atoms=_new_atoms_payload(page_atoms),
                old_page=_page_blob(old_les.id, old_page),
                new_page=_page_blob(new_les.id, new_page),
                prepare_hint=prepare_hint,
            )
            page_assignments = result.get("assignments") or {}
            for w in result.get("warnings") or []:
                llm_warnings.append(f"p{new_page}: {w}")
            pages_llm_ok += 1
        except Exception as exc:
            logger.warning(
                "dual-track textbook LLM failed lesson=%s page=%s: %s",
                new_les.lesson_uid,
                new_page,
                exc,
            )
            llm_warnings.append(f"p{new_page}: {exc}")
            page_source = "rules_fallback"

        if page_assignments is None:
            page_assignments, _left = _build_rule_based_assignments(
                page_atoms,
                page_profiles,
                min_score=min_score,
            )
            pages_rule_fallback += 1

        assigned_on_page = 0
        for ob_code, codes in page_assignments.items():
            if codes:
                raw_assignments[ob_code].extend(codes)
                assigned_on_page += len(codes)

        page_entry["source"] = page_source
        page_entry["assigned_atoms"] = assigned_on_page
        llm_pages.append(page_entry)

    if pages_llm_ok == 0:
        logger.warning(
            "dual-track textbook LLM all pages failed lesson=%s, use rules",
            new_les.lesson_uid,
        )
        rule_track["ranker"] = "rules"
        rule_track["llm_note"] = "豆包各页均失败，已回退全课规则轨"
        rule_track["llm_warnings"] = llm_warnings
        return rule_track

    deduped, _ = _dedupe_atom_assignments(
        dict(raw_assignments),
        all_profiles,
        new_atoms_by_code,
        min_score=0.0,
    )
    assigned_codes = {ac for codes in deduped.values() for ac in codes}
    leftover = [a.atom_code for a in new_atoms if a.atom_code not in assigned_codes]

    out_profiles = rule_track.get("profiles") or []
    if not out_profiles:
        out_profiles = [
            {
                "block_code": p["block_code"],
                "label": p.get("label") or p["block_code"],
                "query": p["query"],
                "y_mid": p.get("y_mid"),
                "old_block_name": p["block"].block_name,
                "textbook_pages": [
                    p["block"].textbook_page_start,
                    p["block"].textbook_page_end,
                ],
            }
            for p in all_profiles
        ]

    atom_scores = _atom_scores_from_assignments(new_atoms, all_profiles)

    return {
        "label": "教材轨（豆包按页 + 教材侧块名）",
        "profiles": out_profiles,
        "assignments": deduped,
        "leftover_atoms": leftover,
        "candidate_blocks": _candidate_blocks_from_assignments(
            deduped,
            out_profiles,
            track="textbook",
            suggest_name_fn=lambda prof: _suggest_new_block_name_from_old(
                prof.get("label") or ""
            ),
        ),
        "coverage": {
            "profile_count": len(out_profiles),
            "assigned_atoms": sum(len(v) for v in deduped.values()),
            "leftover_count": len(leftover),
        },
        "unassigned_followup": _build_unassigned_followup(
            leftover_codes=leftover,
            atom_scores=atom_scores,
            new_atoms_by_code=new_atoms_by_code,
            profiles=out_profiles,
            min_score=min_score,
            track="textbook",
        ),
        "borderline_followup": _build_borderline_followup(
            assignments=deduped,
            profiles=out_profiles,
            new_atoms_by_code=new_atoms_by_code,
            min_score=min_score,
            track="textbook",
        ),
        "ranker": "doubao",
        "llm_model": llm_new_block_mirror_model(),
        "llm_pages": llm_pages,
        "llm_pages_ok": pages_llm_ok,
        "llm_pages_rule_fallback": pages_rule_fallback,
        "llm_warnings": llm_warnings[:24],
    }
