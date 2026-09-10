"""新教材建块工作台数据。"""
from __future__ import annotations

from ....models import Block, Lesson, LessonPage, TextbookAtom, Volume
from ...lesson_lookup import get_lesson_by_uid
from ...old_library.annotate.workspace import (
    _atom_dict,
    _block_dict,
    _is_placeholder_atom,
    build_annotate_workspace,
)
from .pair_review import build_pair_review_context, get_primary_lesson_match


def _paired_old_context(new_lesson_id: str) -> dict | None:
    match = get_primary_lesson_match(new_lesson_id)
    if not match or not match.old_lesson_id:
        return None

    review = build_pair_review_context(new_lesson_id=new_lesson_id)
    if not review.get("show_old_panel"):
        return {
            "lesson_match_id": match.id,
            "match_tier": match.match_tier,
            "match_label": review.get("match_label"),
            "similarity_score": match.similarity_score,
            "pair_review": review,
            "old_lesson": review.get("primary_old_lesson"),
            "old_textbook_pages": [],
            "old_blocks": [],
            "old_atoms": [],
            "hidden": True,
        }

    old_les = Lesson.query.get(match.old_lesson_id)
    if not old_les:
        return None
    old_vol = Volume.query.get(old_les.volume_id)
    old_blocks = (
        Block.query.filter_by(lesson_id=old_les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    old_pages = (
        LessonPage.query.filter_by(lesson_id=old_les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    old_atoms_raw = (
        TextbookAtom.query.filter_by(lesson_id=old_les.id)
        .order_by(TextbookAtom.page_index, TextbookAtom.atom_code)
        .all()
    )
    old_atoms = [
        _atom_dict(a) for a in old_atoms_raw if not _is_placeholder_atom(a)
    ]

    from ....models import CoursewareSlide

    old_slides = (
        CoursewareSlide.query.filter_by(lesson_id=old_les.id)
        .order_by(CoursewareSlide.slide_index)
        .all()
    )
    from .dual_track_prep_steps import build_old_courseware_text_rows

    slide_text_rows = build_old_courseware_text_rows(
        old_lesson_uid=old_les.lesson_uid,
        slides=old_slides,
        old_blocks=old_blocks,
    )
    from .doubao_old_textbook_cache import build_old_textbook_doubao_payload

    old_textbook_doubao = build_old_textbook_doubao_payload(
        lesson_uid=old_les.lesson_uid,
        pages=old_pages,
    )
    from .dual_track_prep_run import (
        build_old_slide_layout_payload,
        slide_layout_status_for_lesson,
    )

    old_slide_layout = build_old_slide_layout_payload(old_les.lesson_uid, old_slides)

    return {
        "lesson_match_id": match.id,
        "match_tier": match.match_tier or "none",
        "match_label": review.get("match_label"),
        "similarity_score": match.similarity_score,
        "pair_review": review,
        "hidden": False,
        "old_lesson": {
            "lesson_uid": old_les.lesson_uid,
            "lesson_no": old_les.lesson_no,
            "lesson_name": old_les.lesson_name,
            "unit_title": old_les.unit_title,
            "old_course_id": old_les.old_course_id,
            "volume_code": old_vol.volume_code if old_vol else None,
            "display_title": old_vol.display_title if old_vol else None,
            "page_start": old_les.page_start,
            "page_end": old_les.page_end,
            "annotate_url": f"/old-library/lessons/{old_les.lesson_uid}/annotate",
            "blocks_locked": bool(old_les.blocks_locked_at),
            "block_count": len(old_blocks),
            "page_count": len(old_pages),
            "is_primary_reference": True,
        },
        "old_textbook_pages": [
            {
                "page_index": p.page_index,
                "blob_id": p.blob_id,
                "url": f"/api/file-blobs/{p.blob_id}",
                "pdf_page": (
                    old_les.page_start + p.page_index - 1
                    if old_les.page_start
                    else None
                ),
            }
            for p in old_pages
        ],
        "old_blocks": [_block_dict(b) for b in old_blocks],
        "old_atoms": old_atoms,
        "old_courseware_slides": [
            {
                "slide_index": s.slide_index,
                "blob_id": s.blob_id,
                "url": f"/api/file-blobs/{s.blob_id}" if s.blob_id else None,
                "ocr_text": (s.ocr_text or "")[:200],
            }
            for s in old_slides
        ],
        "old_courseware_text_rows": slide_text_rows,
        "old_textbook_doubao": old_textbook_doubao,
        "old_slide_layout": old_slide_layout,
        "slide_layout_status": slide_layout_status_for_lesson(
            old_les.lesson_uid,
            old_slides,
        ),
    }


def build_new_annotate_workspace(*, lesson_uid: str) -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type="new")
    data = build_annotate_workspace(lesson_uid=lesson_uid, book_type="new")
    review = build_pair_review_context(new_lesson_id=les.id)
    data["pair_review"] = review
    data["referenced_old_lessons"] = review.get("referenced_old_lessons") or []
    from .content_prescan import (
        latest_content_scan_payload,
        lesson_atoms_status,
        lesson_ocr_phase_status,
    )
    from ..lesson_analysis import lesson_analysis_dict

    data["content_prescan"] = latest_content_scan_payload(new_lesson_id=les.id)
    data["prescan_ocr"] = lesson_ocr_phase_status(lesson_id=les.id)
    data["lesson_atoms_status"] = lesson_atoms_status(lesson_id=les.id)
    data["analysis"] = lesson_analysis_dict(lesson_id=les.id, lesson_uid=les.lesson_uid)
    from ..block_pipeline_run import lesson_block_pipeline_dict

    data["block_pipeline"] = lesson_block_pipeline_dict(
        lesson_id=les.id,
        lesson_uid=les.lesson_uid,
    )
    paired = _paired_old_context(les.id)
    if paired and paired.get("hidden"):
        data["paired_old"] = None
    else:
        data["paired_old"] = paired
    from .dual_track_pipeline_profile import get_dual_track_pipeline_profile

    data["dual_track_profile"] = get_dual_track_pipeline_profile(lesson_uid)
    return data
