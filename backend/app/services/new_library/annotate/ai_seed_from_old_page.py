"""新库对照旧块建块：豆包视觉分配 + 校验。"""
from __future__ import annotations

import logging
from typing import Any

from ....models import Block, Lesson, LessonPage, TextbookAtom
from ...llm.config import new_block_mirror_llm_enabled
from ...llm.new_block_mirror_suggest import (
    suggest_anchor_matches_with_llm,
    suggest_mirror_assignments_with_llm,
)
from ..block_pipeline_prepare import format_prepare_hint, get_page_prepare
from .seed_from_old_page import (
    _block_query_and_ymid,
    _image_label_text,
    _is_image_like_atom,
)

logger = logging.getLogger(__name__)


def _atom_text_for_llm(atom: TextbookAtom) -> str:
    return (atom.content or atom.ocr_text or "").strip()


def _page_blob(lesson_id: str, page_index: int) -> dict[str, Any] | None:
    row = LessonPage.query.filter_by(
        lesson_id=lesson_id,
        page_index=int(page_index),
    ).first()
    if not row:
        return None
    return {
        "page_index": int(page_index),
        "blob_id": row.blob_id,
    }


def _old_block_atoms_on_page(
    block: Block,
    page_index: int,
    atoms_by_code: dict[str, TextbookAtom],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for code in block.atom_codes or []:
        atom = atoms_by_code.get(str(code).strip())
        if not atom or int(atom.page_index) != int(page_index):
            continue
        out.append(
            {
                "atom_code": atom.atom_code,
                "text": _atom_text_for_llm(atom)[:120],
            }
        )
    return out


def build_mirror_llm_context(
    *,
    new_les: Lesson,
    old_les: Lesson,
    new_page_index: int,
    old_page_index: int,
    old_profiles: list[dict],
    free_new_atoms: list[TextbookAtom],
    old_atoms_by_code: dict[str, TextbookAtom],
) -> dict[str, Any]:
    old_blocks_payload: list[dict[str, Any]] = []
    for prof in old_profiles:
        ob: Block = prof["block"]
        old_blocks_payload.append(
            {
                "block_code": ob.block_code,
                "block_name": ob.block_name,
                "skip_reason": prof.get("skip_reason") or "",
                "query_excerpt": (prof.get("query") or "")[:320],
                "atoms": _old_block_atoms_on_page(
                    ob, old_page_index, old_atoms_by_code
                ),
            }
        )

    new_atoms_payload: list[dict[str, Any]] = []
    for atom in free_new_atoms:
        label = _image_label_text(atom) if _is_image_like_atom(atom) else ""
        new_atoms_payload.append(
            {
                "atom_code": atom.atom_code,
                "atom_type": (atom.atom_type or "text").strip().lower(),
                "text": _atom_text_for_llm(atom),
                "image_label": label,
            }
        )

    return {
        "lesson_name": new_les.lesson_name or old_les.lesson_name or "",
        "unit_title": new_les.unit_title or old_les.unit_title or "",
        "old_page_index": int(old_page_index),
        "new_page_index": int(new_page_index),
        "old_blocks": old_blocks_payload,
        "new_atoms": new_atoms_payload,
        "old_page": _page_blob(old_les.id, old_page_index),
        "new_page": _page_blob(new_les.id, new_page_index),
    }


def try_llm_mirror_assignments(
    *,
    new_les: Lesson,
    old_les: Lesson,
    new_page_index: int,
    old_page_index: int,
    old_profiles: list[dict],
    free_new_atoms: list[TextbookAtom],
    old_atoms_by_code: dict[str, TextbookAtom],
) -> tuple[dict[str, list[str]] | None, str, list[str]]:
    """
    调用豆包分配新原子到旧块。
    返回 (assignments | None, source, warnings)。
    source: old_mirror_llm | rules
    """
    if not new_block_mirror_llm_enabled():
        return None, "rules", []

    if not free_new_atoms:
        return None, "rules", []

    ctx = build_mirror_llm_context(
        new_les=new_les,
        old_les=old_les,
        new_page_index=new_page_index,
        old_page_index=old_page_index,
        old_profiles=old_profiles,
        free_new_atoms=free_new_atoms,
        old_atoms_by_code=old_atoms_by_code,
    )
    page_ctx = get_page_prepare(new_les.lesson_uid, new_page_index)
    prepare_hint = format_prepare_hint(page_ctx)

    try:
        result = suggest_mirror_assignments_with_llm(
            lesson_name=ctx["lesson_name"],
            unit_title=ctx["unit_title"],
            old_page_index=ctx["old_page_index"],
            new_page_index=ctx["new_page_index"],
            old_blocks=ctx["old_blocks"],
            new_atoms=ctx["new_atoms"],
            old_page=ctx["old_page"],
            new_page=ctx["new_page"],
            prepare_hint=prepare_hint,
        )
        assignments = result.get("assignments") or {}
        warnings = list(result.get("warnings") or [])
        model = result.get("model") or ""
        if model:
            warnings.insert(0, f"model={model}")
        logger.info(
            "new mirror LLM ok lesson=%s page=%s blocks=%s atoms=%s",
            new_les.lesson_uid,
            new_page_index,
            sum(1 for v in assignments.values() if v),
            sum(len(v) for v in assignments.values()),
        )
        return assignments, "old_mirror_llm", warnings
    except Exception as exc:
        logger.warning(
            "new mirror LLM failed lesson=%s page=%s: %s",
            new_les.lesson_uid,
            new_page_index,
            exc,
        )
        return None, "rules", [str(exc)]


def _block_atoms_on_page(
    block: Block,
    page_index: int,
    atoms_by_code: dict[str, TextbookAtom],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for code in block.atom_codes or []:
        atom = atoms_by_code.get(str(code).strip())
        if not atom or int(atom.page_index) != int(page_index):
            continue
        label = _image_label_text(atom) if _is_image_like_atom(atom) else ""
        out.append(
            {
                "atom_code": atom.atom_code,
                "text": _atom_text_for_llm(atom)[:120],
                "image_label": label,
            }
        )
    return out


def build_anchor_llm_context(
    *,
    new_les: Lesson,
    old_les: Lesson,
    new_page_index: int,
    old_page_index: int,
    new_blocks: list[Block],
    old_blocks: list[Block],
    used_old_codes: set[str],
    new_atoms_by_code: dict[str, TextbookAtom],
    old_atoms_by_code: dict[str, TextbookAtom],
) -> dict[str, Any]:
    old_payload: list[dict[str, Any]] = []
    for ob in old_blocks:
        skip = "already_anchored" if ob.block_code in used_old_codes else ""
        query, _ = _block_query_and_ymid(ob, old_atoms_by_code)
        old_payload.append(
            {
                "block_code": ob.block_code,
                "block_name": ob.block_name,
                "skip_reason": skip,
                "query_excerpt": query[:320],
                "atoms": _block_atoms_on_page(
                    ob, old_page_index, old_atoms_by_code
                ),
            }
        )

    new_payload: list[dict[str, Any]] = []
    for nb in new_blocks:
        query, _ = _block_query_and_ymid(nb, new_atoms_by_code)
        new_payload.append(
            {
                "block_code": nb.block_code,
                "block_name": nb.block_name,
                "query_excerpt": query[:320],
                "atoms": _block_atoms_on_page(
                    nb, new_page_index, new_atoms_by_code
                ),
            }
        )

    return {
        "lesson_name": new_les.lesson_name or old_les.lesson_name or "",
        "unit_title": new_les.unit_title or old_les.unit_title or "",
        "old_page_index": int(old_page_index),
        "new_page_index": int(new_page_index),
        "old_blocks": old_payload,
        "new_blocks": new_payload,
        "old_page": _page_blob(old_les.id, old_page_index),
        "new_page": _page_blob(new_les.id, new_page_index),
    }


def try_llm_anchor_matches(
    *,
    new_les: Lesson,
    old_les: Lesson,
    new_page_index: int,
    old_page_index: int,
    new_blocks: list[Block],
    old_blocks: list[Block],
    used_old_codes: set[str],
    new_atoms_by_code: dict[str, TextbookAtom],
    old_atoms_by_code: dict[str, TextbookAtom],
) -> tuple[dict[str, str] | None, str, list[str]]:
    """
    豆包视觉：新区块 → 旧区块锚定。
    返回 (new_block_code→old_block_code | None, source, warnings)。
    """
    if not new_block_mirror_llm_enabled():
        return None, "rules", []

    if not new_blocks:
        return None, "rules", []

    ctx = build_anchor_llm_context(
        new_les=new_les,
        old_les=old_les,
        new_page_index=new_page_index,
        old_page_index=old_page_index,
        new_blocks=new_blocks,
        old_blocks=old_blocks,
        used_old_codes=used_old_codes,
        new_atoms_by_code=new_atoms_by_code,
        old_atoms_by_code=old_atoms_by_code,
    )

    page_ctx = get_page_prepare(new_les.lesson_uid, new_page_index)
    prepare_hint = format_prepare_hint(page_ctx)

    try:
        result = suggest_anchor_matches_with_llm(
            lesson_name=ctx["lesson_name"],
            unit_title=ctx["unit_title"],
            old_page_index=ctx["old_page_index"],
            new_page_index=ctx["new_page_index"],
            old_blocks=ctx["old_blocks"],
            new_blocks=ctx["new_blocks"],
            old_page=ctx["old_page"],
            new_page=ctx["new_page"],
            require_complete=True,
            prepare_hint=prepare_hint,
        )
        matches = result.get("matches") or {}
        warnings = list(result.get("warnings") or [])
        model = result.get("model") or ""
        if model:
            warnings.insert(0, f"model={model}")
        logger.info(
            "new anchor LLM ok lesson=%s page=%s matched=%s",
            new_les.lesson_uid,
            new_page_index,
            len(matches),
        )
        return matches, "anchor_llm", warnings
    except Exception as exc:
        logger.warning(
            "new anchor LLM failed lesson=%s page=%s: %s",
            new_les.lesson_uid,
            new_page_index,
            exc,
        )
        return None, "rules", [str(exc)]
