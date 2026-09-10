"""compare 工作台数据组装。"""
from __future__ import annotations

from ....models import Block, BlockMatch, CoursewareSlide, LessonPage, ReuseReport, TextbookAtom, Volume
from ....services.dictionary import list_dictionary_entries
from .lesson_grade import build_lesson_insights
from .resolve import get_lesson_match_for_new_lesson


def _textbook_page_list(block: Block, atoms_by_code: dict[str, TextbookAtom]) -> list[int]:
    if block.textbook_page_start is not None:
        start = int(block.textbook_page_start)
        end = int(block.textbook_page_end or start)
        return list(range(start, end + 1))
    pages: set[int] = set()
    for code in block.atom_codes or []:
        atom = atoms_by_code.get(str(code).strip())
        if atom:
            pages.add(int(atom.page_index))
    return sorted(pages)


def _block_payload(
    block: Block,
    *,
    side: str,
    atoms_by_code: dict[str, TextbookAtom],
) -> dict:
    tb_pages = _textbook_page_list(block, atoms_by_code)
    payload = {
        "id": block.id,
        "block_id": block.block_code,
        "block_code": block.block_code,
        "block_name": block.block_name,
        "atom_codes": list(block.atom_codes or []),
        "sort_order": block.sort_order or 0,
        "metadata": block.metadata_json or {},
    }
    if side == "old":
        payload["cw_pgs"] = sorted(int(x) for x in (block.course_slide_indices or []))
        payload["old_tb_pgs"] = tb_pages
        payload["new_tb_pgs"] = []
    else:
        payload["cw_pgs"] = []
        payload["old_tb_pgs"] = []
        payload["new_tb_pgs"] = tb_pages
    return payload


def _match_payload(
    bm: BlockMatch,
    *,
    old_by_id: dict[str, Block],
    new_by_id: dict[str, Block],
    reuse_labels: dict[str, str],
    change_labels: dict[str, str],
) -> dict:
    old_block = old_by_id.get(bm.old_block_id or "")
    new_block = new_by_id.get(bm.new_block_id or "")
    reuse_code = bm.reuse_action or ""
    change_code = bm.change_type or ""
    ai = (bm.metadata_json or {}).get("ai_suggestion") or {}
    ai_reuse = ai.get("reuse_action") or ""
    ai_change = ai.get("change_type") or ""
    return {
        "match_id": bm.id,
        "old_block_id": old_block.block_code if old_block else None,
        "new_block_id": new_block.block_code if new_block else None,
        "old_block_uuid": bm.old_block_id,
        "new_block_uuid": bm.new_block_id,
        "match_type": bm.match_type or "1:1",
        "reuse_action": reuse_code,
        "reuse_action_label": reuse_labels.get(reuse_code, reuse_code),
        "change_type": change_code,
        "change_type_label": change_labels.get(change_code, change_code),
        "teacher_note": bm.teacher_note or "",
        "ai_reuse_action": ai_reuse,
        "ai_reuse_action_label": reuse_labels.get(ai_reuse, ai_reuse),
        "ai_change_type": ai_change,
        "ai_change_type_label": change_labels.get(ai_change, ai_change),
        "ai_teacher_note": ai.get("teacher_note") or "",
        "ai_rationale": ai.get("rationale") or [],
        "ai_change_points": ai.get("change_points") or [],
        "ai_confidence": ai.get("confidence") or "",
        "ai_optimize_subtype": ai.get("optimize_subtype"),
        "ai_text_change": ai.get("text_change") or {},
        "ai_match_score": ai.get("match_score"),
        "ai_generated_at": ai.get("generated_at"),
        "ai_source": ai.get("source") or "",
        "ai_model": ai.get("model") or "",
        "feedback": (bm.metadata_json or {}).get("teacher_feedback"),
        "created_at": bm.created_at.isoformat() if bm.created_at else None,
        "updated_at": bm.updated_at.isoformat() if bm.updated_at else None,
    }


def _lesson_pages(lesson_id: str) -> list[dict]:
    rows = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    return [
        {
            "pg": p.page_index,
            "page_index": p.page_index,
            "url": f"/api/file-blobs/{p.blob_id}",
        }
        for p in rows
    ]


def _cw_pages(lesson_id: str) -> list[dict]:
    slides = (
        CoursewareSlide.query.filter_by(lesson_id=lesson_id)
        .order_by(CoursewareSlide.slide_index)
        .all()
    )
    return [
        {
            "pg": s.slide_index,
            "slide_index": s.slide_index,
            "url": f"/api/file-blobs/{s.blob_id}" if s.blob_id else None,
        }
        for s in slides
    ]


def build_compare_workspace(
    *,
    new_lesson_uid: str,
    lesson_match_id: str | None = None,
) -> dict:
    match, old_les, new_les = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    new_vol = Volume.query.get(new_les.volume_id)

    old_blocks = (
        Block.query.filter_by(lesson_id=old_les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    new_blocks = (
        Block.query.filter_by(lesson_id=new_les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    old_atoms = TextbookAtom.query.filter_by(lesson_id=old_les.id).all()
    new_atoms = TextbookAtom.query.filter_by(lesson_id=new_les.id).all()
    old_atoms_by_code = {a.atom_code: a for a in old_atoms}
    new_atoms_by_code = {a.atom_code: a for a in new_atoms}

    old_by_id = {b.id: b for b in old_blocks}
    new_by_id = {b.id: b for b in new_blocks}

    block_matches = (
        BlockMatch.query.filter_by(lesson_match_id=match.id)
        .order_by(BlockMatch.created_at)
        .all()
    )

    reuse_report = ReuseReport.query.filter_by(match_id=match.id).first()

    reuse_options = list_dictionary_entries(category="reuse_action")
    change_options = list_dictionary_entries(category="change_type")
    reuse_labels = {r["code"]: r["label"] for r in reuse_options}
    change_labels = {r["code"]: r["label"] for r in change_options}

    float_atoms = []
    for a in old_atoms:
        bbox = a.bbox_json or {}
        float_atoms.append(
            {
                "atom_id": a.atom_code,
                "atom_code": a.atom_code,
                "book_side": "old",
                "page": a.page_index,
                "page_index": a.page_index,
                "text": (a.content or a.ocr_text or "").strip(),
                "content": a.content or "",
                "ocr_text": a.ocr_text or "",
                "atom_type": a.atom_type,
                "x_start": bbox.get("x_start", 0),
                "y_start": bbox.get("y_start", 0),
                "x_end": bbox.get("x_end", 1),
                "y_end": bbox.get("y_end", 1),
            }
        )
    for a in new_atoms:
        bbox = a.bbox_json or {}
        float_atoms.append(
            {
                "atom_id": a.atom_code,
                "atom_code": a.atom_code,
                "book_side": "new",
                "page": a.page_index,
                "page_index": a.page_index,
                "text": (a.content or a.ocr_text or "").strip(),
                "content": a.content or "",
                "ocr_text": a.ocr_text or "",
                "atom_type": a.atom_type,
                "x_start": bbox.get("x_start", 0),
                "y_start": bbox.get("y_start", 0),
                "x_end": bbox.get("x_end", 1),
                "y_end": bbox.get("y_end", 1),
            }
        )

    match_payloads = [
        _match_payload(
            bm,
            old_by_id=old_by_id,
            new_by_id=new_by_id,
            reuse_labels=reuse_labels,
            change_labels=change_labels,
        )
        for bm in block_matches
    ]
    old_block_payloads = [
        _block_payload(b, side="old", atoms_by_code=old_atoms_by_code)
        for b in old_blocks
    ]
    new_block_payloads = [
        _block_payload(b, side="new", atoms_by_code=new_atoms_by_code)
        for b in new_blocks
    ]
    insights = build_lesson_insights(
        matches=match_payloads,
        new_blocks=new_block_payloads,
        old_blocks=old_block_payloads,
        float_atoms=float_atoms,
    )

    return {
        "ok": True,
        "lesson_match_id": match.id,
        "lesson": {
            "new_lesson_uid": new_les.lesson_uid,
            "new_lesson_name": new_les.lesson_name,
            "old_lesson_uid": old_les.lesson_uid,
            "old_lesson_name": old_les.lesson_name,
            "lesson_name": new_les.lesson_name,
            "volume_code": new_vol.volume_code if new_vol else None,
            "volume_title": (
                (new_vol.display_title or new_vol.volume_code) if new_vol else None
            ),
        },
        "old_blocks": old_block_payloads,
        "new_blocks": new_block_payloads,
        "matches": match_payloads,
        **insights,
        "float_atoms": float_atoms,
        "cw_pages": _cw_pages(old_les.id),
        "old_pages": _lesson_pages(old_les.id),
        "new_pages": _lesson_pages(new_les.id),
        "reuse_action_options": reuse_options,
        "change_type_options": change_options,
        "compare_confirmed_at": (
            match.compare_confirmed_at.isoformat() if match.compare_confirmed_at else None
        ),
        "compare_confirmed_by": match.compare_confirmed_by,
        "readonly": bool(match.compare_confirmed_at),
        "reuse_report": (
            {
                "reuse_ratio": reuse_report.reuse_ratio,
                "summary": reuse_report.summary,
                "detail_json": reuse_report.detail_json or {},
                "created_at": (
                    reuse_report.created_at.isoformat()
                    if reuse_report.created_at
                    else None
                ),
            }
            if reuse_report
            else None
        ),
    }
