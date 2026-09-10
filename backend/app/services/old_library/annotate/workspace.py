"""annotate 工作台数据。"""
from __future__ import annotations

from ....models import Block, CoursewareSlide, Lesson, LessonMatch, LessonPage, TextbookAtom, Volume
from ....services.dictionary import list_dictionary_entries
from ...lesson_lookup import get_lesson_by_uid
from ..edition_registry import resolve_edition_id
from .atom_lineage import ocr_source_count
from .block_stage import read_block_fields
from .lesson_ocr_v2 import lesson_ocr_phase_stats
from .lesson_pipeline_profile import get_lesson_pipeline_profile



def _is_placeholder_atom(atom: TextbookAtom) -> bool:
    t = (atom.content or atom.ocr_text or "").strip()
    if t.startswith("[未拆分") or t.startswith("[整页未识别") or t == "[整页图像]":
        return True
    return "-GAP-" in (atom.atom_code or "") or atom.atom_code.endswith("-FULL-001")


def _atom_dict(
    atom: TextbookAtom,
    *,
    baseline: list | None = None,
    lineage: dict | None = None,
) -> dict:
    bbox = atom.bbox_json or {}
    ocr_count = 0
    if baseline:
        ocr_count = ocr_source_count(
            lineage or {},
            atom.atom_code,
            baseline,
            bbox,
        )
    return {
        "id": atom.id,
        "atom_code": atom.atom_code,
        "page_index": atom.page_index,
        "atom_type": atom.atom_type,
        "bbox": bbox,
        "content": atom.content or "",
        "ocr_text": atom.ocr_text or "",
        "is_placeholder": _is_placeholder_atom(atom),
        "ocr_source_count": ocr_count,
        "can_unmerge_ocr": ocr_count >= 2,
        "metadata_json": atom.metadata_json or {},
    }


def _block_dict(block: Block, *, book_type: str = "old") -> dict:
    fields = read_block_fields(block, book_type=book_type)
    return {
        "id": block.id,
        "block_code": block.block_code,
        "block_name": fields["block_name"],
        "stage_ref": fields["stage_ref"],
        "textbook_page_start": block.textbook_page_start,
        "textbook_page_end": block.textbook_page_end,
        "atom_codes": block.atom_codes or [],
        "course_slide_indices": block.course_slide_indices or [],
        "sort_order": block.sort_order or 0,
        "metadata_json": block.metadata_json or {},
    }


def build_annotate_workspace(*, lesson_uid: str, book_type: str = "old") -> dict:
    from .ai_bootstrap import assess_lesson_bootstrap_readiness, compute_lesson_build_pipeline
    from .atom_prematch_curate import build_prematch_review

    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    lesson_id = str(les.id)
    volume = Volume.query.get(les.volume_id)

    lesson_meta = {
        "lesson_uid": les.lesson_uid,
        "lesson_name": les.lesson_name,
        "unit_title": les.unit_title,
        "lesson_no": les.lesson_no,
        "old_course_id": les.old_course_id,
        "page_start": les.page_start,
        "page_end": les.page_end,
        "page_count": les.page_count,
        "slides_fetch_status": les.slides_fetch_status,
        "blocks_locked": bool(les.blocks_locked_at),
        "blocks_locked_at": (
            les.blocks_locked_at.isoformat() if les.blocks_locked_at else None
        ),
        "volume_code": volume.volume_code if volume else None,
        "display_title": volume.display_title if volume else None,
        "edition": volume.edition if volume else None,
        "edition_id": resolve_edition_id(volume.edition) if volume else None,
        "book_type": book_type,
    }
    page_start = les.page_start

    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    slides = (
        []
        if book_type == "new"
        else CoursewareSlide.query.filter_by(lesson_id=lesson_id)
        .order_by(CoursewareSlide.slide_index)
        .all()
    )
    atoms = (
        TextbookAtom.query.filter_by(lesson_id=lesson_id)
        .order_by(TextbookAtom.page_index, TextbookAtom.atom_code)
        .all()
    )
    blocks = (
        Block.query.filter_by(lesson_id=lesson_id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )

    page_baseline: dict[int, list] = {
        p.page_index: list(p.ocr_atoms_json or []) for p in pages
    }
    page_lineage: dict[int, dict] = {
        p.page_index: dict(p.atom_lineage_json or {}) for p in pages
    }

    visible_atoms = [a for a in atoms if not _is_placeholder_atom(a)]
    hidden_atoms = [a for a in atoms if _is_placeholder_atom(a)]
    page_atom_counts: dict[int, int] = {}
    page_hidden_counts: dict[int, int] = {}
    for a in visible_atoms:
        page_atom_counts[a.page_index] = page_atom_counts.get(a.page_index, 0) + 1
    for a in hidden_atoms:
        page_hidden_counts[a.page_index] = page_hidden_counts.get(a.page_index, 0) + 1

    textbook_pages = [
        {
            "page_index": p.page_index,
            "blob_id": p.blob_id,
            "url": f"/api/file-blobs/{p.blob_id}",
            "pdf_page": (
                page_start + p.page_index - 1 if page_start else None
            ),
            "has_ocr_baseline": bool(p.ocr_atoms_json),
            "atom_count": page_atom_counts.get(p.page_index, 0),
            "recommended_extract_first": not (
                bool(p.ocr_atoms_json)
                and page_atom_counts.get(p.page_index, 0) > 0
            ),
        }
        for p in pages
    ]
    courseware_slides = [
        {
            "slide_index": s.slide_index,
            "blob_id": s.blob_id,
            "url": f"/api/file-blobs/{s.blob_id}" if s.blob_id else None,
        }
        for s in slides
    ]
    atoms_payload = [
        _atom_dict(
            a,
            baseline=page_baseline.get(a.page_index),
            lineage=page_lineage.get(a.page_index),
        )
        for a in visible_atoms
    ]
    blocks_payload = [_block_dict(b, book_type=book_type) for b in blocks]
    stats = {
        "atom_count": len(visible_atoms),
        "block_count": len(blocks),
        "slide_count": len(slides),
        "page_count": len(pages),
    }

    profile = get_lesson_pipeline_profile(lesson_uid)
    ocr_phase = lesson_ocr_phase_stats(lesson_uid=lesson_uid, book_type=book_type)
    bootstrap_readiness = assess_lesson_bootstrap_readiness(
        lesson_uid=lesson_uid, book_type=book_type
    )
    blocks_locked = bool(les.blocks_locked_at)
    build_pipeline = compute_lesson_build_pipeline(
        readiness=bootstrap_readiness,
        block_count=len(blocks),
        blocks_locked=blocks_locked,
        slide_count=len(slides),
        lesson_page_count=len(pages),
        atom_count=len(visible_atoms),
        lesson_id=lesson_id,
    )
    prematch_review = build_prematch_review(lesson_uid=lesson_uid, book_type=book_type)

    return {
        "ok": True,
        "lesson": lesson_meta,
        "textbook_pages": textbook_pages,
        "courseware_slides": courseware_slides,
        "atoms": atoms_payload,
        "hidden_placeholder_count": len(hidden_atoms),
        "page_atom_counts": page_atom_counts,
        "page_hidden_placeholder_counts": page_hidden_counts,
        "blocks": blocks_payload,
        "block_stage_options": list_dictionary_entries(
            category="block_stage_new" if book_type == "new" else "block_stage"
        ),
        "stats": stats,
        "bootstrap_readiness": bootstrap_readiness,
        "build_pipeline": build_pipeline,
        "prematch_review": prematch_review,
        "pipeline_profile": profile,
        "ocr_phase": ocr_phase,
    }
