"""双轨前置工序：执行 OCR（仅新教材写库；旧教材/旧课件只读旧库）。"""
from __future__ import annotations

import logging
from typing import Any

from ....extensions import db
from ....models import LessonPage, TextbookAtom
from ...old_library.annotate.workspace import _is_placeholder_atom
from .content_prescan import (
    lesson_ocr_phase_status,
    ocr_lesson_pages,
    refresh_lesson_body_text,
)
from ...llm.page_text_extract import dual_track_textbook_uses_doubao
from ...llm.slide_text_extract import (
    load_lesson_slide_text_cache,
    load_lesson_slide_jobs,
    ocr_lesson_slides_text,
    persist_slide_text_to_db,
)
from ...llm.slide_layout_extract import (
    load_lesson_slide_layout_cache,
    ocr_lesson_slides_layout,
    slide_layout_for_index,
)
from .doubao_old_textbook_cache import build_old_textbook_doubao_payload
from .doubao_textbook_ocr import ocr_lesson_pages_doubao_text
from .dual_track_prep_steps import build_old_courseware_text_rows
from .dual_track_ctx_snap import build_dual_track_ctx_snap, ctx_snap

logger = logging.getLogger(__name__)


def _new_textbook_page_total(lesson_id: str) -> int:
    return int(
        db.session.query(LessonPage.page_index)
        .filter_by(lesson_id=lesson_id)
        .count()
    )


def _old_lesson_ids(ctx: dict[str, Any]) -> tuple[str, str]:
    old = ctx_snap(ctx)["old_les"]
    return str(old["id"]), str(old["lesson_uid"])


def _new_lesson_ids(ctx: dict[str, Any]) -> tuple[str, str]:
    new = ctx_snap(ctx)["new_les"]
    return str(new["id"]), str(new["lesson_uid"])


def _old_library_ocr_stats(old_lesson_uid: str) -> dict[str, Any]:
    from ...old_library.annotate.lesson_ocr_v2 import lesson_ocr_phase_stats

    return lesson_ocr_phase_stats(lesson_uid=old_lesson_uid, book_type="old")


def _old_slide_text_snapshot(
    ctx: dict[str, Any],
    *,
    force_slides: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """旧课件文字：默认只读旧库 DB/豆包缓存；仅 force 且旧库未完成时补跑。"""
    old_lesson_id, old_lesson_uid = _old_lesson_ids(ctx)
    slide_jobs = load_lesson_slide_jobs(old_lesson_id)
    warnings: list[str] = []
    skipped = True

    old_stats = _old_library_ocr_stats(old_lesson_uid)
    slide_cache = load_lesson_slide_text_cache(old_lesson_uid)

    if force_slides and slide_jobs and not old_stats.get("slide_text_ocr_complete"):
        logger.info("dual-track text_ocr: force old slide text OCR lesson=%s", old_lesson_uid)
        slide_cache, slide_warnings = ocr_lesson_slides_text(
            lesson_uid=old_lesson_uid,
            lesson_id=old_lesson_id,
            force_refresh=True,
        )
        warnings.extend(slide_warnings)
        persist_slide_text_to_db(
            lesson_id=old_lesson_id,
            lesson_uid=old_lesson_uid,
            cache=slide_cache,
        )
        db.session.commit()
        old_stats = _old_library_ocr_stats(old_lesson_uid)
        skipped = False
    elif slide_jobs and not old_stats.get("slide_text_ocr_complete"):
        warnings.append(
            "旧课件文字 OCR 未齐全（"
            f"{old_stats.get('slides_text_done', 0)}/{old_stats.get('slide_count', 0)} 页），"
            "请先在旧库 intake 跑文字 OCR"
        )

    rows = build_old_courseware_text_rows(
        old_lesson_uid=old_lesson_uid,
        slides=slide_jobs,
        old_blocks=ctx.get("old_blocks"),
    )
    slides_with_text = sum(1 for r in rows if r.get("has_text"))

    return {
        "engine": "old_library_db",
        "slides_total": len(slide_jobs),
        "slides_with_text": slides_with_text,
        "cache_lesson_uid": old_lesson_uid,
        "skipped": skipped,
        "complete": bool(old_stats.get("slide_text_ocr_complete")),
    }, warnings


def _old_textbook_text_snapshot(
    ctx: dict[str, Any],
    *,
    force_textbook: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """旧教材文字：默认只读旧库 atoms；仅 force 且旧库未完成时补跑。"""
    old_lesson_id, old_lesson_uid = _old_lesson_ids(ctx)
    warnings: list[str] = []
    skipped = True

    old_stats = _old_library_ocr_stats(old_lesson_uid)

    if force_textbook and not old_stats.get("textbook_text_ocr_complete"):
        logger.info("dual-track text_ocr: force old textbook doubao OCR lesson=%s", old_lesson_uid)
        ocr_lesson_pages_doubao_text(
            lesson_uid=old_lesson_uid,
            book_type="old",
            force_refresh=True,
        )
        db.session.commit()
        old_stats = _old_library_ocr_stats(old_lesson_uid)
        skipped = False
    elif not old_stats.get("textbook_text_ocr_complete"):
        warnings.append(
            "旧教材文字 OCR 未齐全（"
            f"{old_stats.get('text_pages_done', 0)}/{old_stats.get('page_count', 0)} 页），"
            "请先在旧库 intake 跑文字 OCR"
        )

    doubao_payload = build_old_textbook_doubao_payload(
        lesson_uid=old_lesson_uid,
        pages=ctx_snap(ctx).get("old_pages") or [],
    )

    return {
        "engine": "old_library_atoms",
        "pages_done": int(old_stats.get("text_pages_done") or 0),
        "pages_total": int(old_stats.get("page_count") or 0),
        "text_atom_count": int(old_stats.get("text_atom_count") or 0),
        "cache_lesson_uid": old_lesson_uid,
        "skipped": skipped,
        "complete": bool(old_stats.get("textbook_text_ocr_complete")),
        "doubao_cache_atoms": int(doubao_payload.get("atom_count") or 0),
    }, warnings


def execute_prep_text_ocr(
    ctx: dict[str, Any],
    *,
    force_textbook: bool = False,
    force_slides: bool = False,
) -> dict[str, Any]:
    """工序1：新教材文字 OCR；旧教材/旧课件只读旧库（全库升级后应已就绪）。"""
    snap = ctx_snap(ctx)
    new_lesson_id, new_lesson_uid = _new_lesson_ids(ctx)
    old_lesson_uid = snap["old_les"]["lesson_uid"]
    warnings: list[str] = []

    logger.info(
        "dual-track text_ocr start new=%s old=%s force_tb=%s force_slides=%s",
        new_lesson_uid,
        old_lesson_uid,
        force_textbook,
        force_slides,
    )

    slide_meta, slide_warnings = _old_slide_text_snapshot(ctx, force_slides=force_slides)
    warnings.extend(slide_warnings)

    old_tb_meta, old_tb_warnings = _old_textbook_text_snapshot(
        ctx,
        force_textbook=force_textbook,
    )
    warnings.extend(old_tb_warnings)

    use_doubao_tb = dual_track_textbook_uses_doubao()
    tb_engine = "doubao_page_text" if use_doubao_tb else "rapidocr+pdf"

    phase = lesson_ocr_phase_status(lesson_id=new_lesson_id)
    total_pages = _new_textbook_page_total(new_lesson_id)
    textbook_skipped = bool(phase.get("text_ocr_complete")) and not force_textbook
    logger.info(
        "dual-track text_ocr new textbook engine=%s skipped=%s",
        tb_engine,
        textbook_skipped,
    )
    if textbook_skipped:
        done_text = int(phase.get("text_pages_done") or total_pages)
        warnings.append(
            f"新教材文字 OCR 已完成，跳过重复执行（引擎 {tb_engine}；确定重跑可强制刷新）"
        )
    elif use_doubao_tb:
        done_text, total_pages, ocr_warnings = ocr_lesson_pages_doubao_text(
            lesson_uid=new_lesson_uid,
            force_refresh=force_textbook,
        )
        warnings.extend(ocr_warnings)
        refresh_lesson_body_text(lesson_id=new_lesson_id)
    else:
        done_text, total_pages, ocr_warnings = ocr_lesson_pages(
            lesson_uid=new_lesson_uid,
            ocr_scope="lesson_all",
            ocr_phase="text",
        )
        warnings.extend(ocr_warnings)
        refresh_lesson_body_text(lesson_id=new_lesson_id)

    logger.info(
        "dual-track text_ocr complete new=%d/%d old_tb=%d/%d old_slides=%d/%d",
        done_text,
        total_pages,
        old_tb_meta["pages_done"],
        old_tb_meta["pages_total"],
        slide_meta["slides_with_text"],
        slide_meta["slides_total"],
    )

    db.session.commit()

    new_atoms = TextbookAtom.query.filter_by(lesson_id=new_lesson_id).all()
    text_atoms = [
        a
        for a in new_atoms
        if not _is_placeholder_atom(a)
        and (a.atom_type or "text").lower() not in ("image", "figure")
    ]

    return {
        "executed": True,
        "new_text_ocr": {
            "engine": tb_engine,
            "pages_done": done_text,
            "pages_total": total_pages,
            "text_atom_count": len(text_atoms),
            "skipped": textbook_skipped,
        },
        "slide_text_ocr": slide_meta,
        "old_textbook_ocr": old_tb_meta,
        # 兼容旧前端字段名
        "old_textbook_doubao_ocr": {
            "engine": old_tb_meta["engine"],
            "pages_done": old_tb_meta["pages_done"],
            "pages_total": old_tb_meta["pages_total"],
            "atom_count": old_tb_meta["text_atom_count"],
            "cache_lesson_uid": old_lesson_uid,
            "skipped": old_tb_meta["skipped"],
        },
        "warnings": warnings[:30],
    }


def _old_slide_layout_snapshot(
    ctx: dict[str, Any],
    *,
    force_slides: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """旧课件插图版面：默认只读旧库缓存；仅 force 且未完成时补跑。"""
    old_lesson_id, old_lesson_uid = _old_lesson_ids(ctx)
    slide_jobs = load_lesson_slide_jobs(old_lesson_id)
    warnings: list[str] = []
    skipped = True

    old_stats = _old_library_ocr_stats(old_lesson_uid)

    if force_slides and slide_jobs and not old_stats.get("slide_image_ocr_complete"):
        logger.info("dual-track image_ocr: force old slide layout OCR lesson=%s", old_lesson_uid)
        layout_cache, slide_warnings = ocr_lesson_slides_layout(
            lesson_uid=old_lesson_uid,
            lesson_id=old_lesson_id,
            force_refresh=True,
        )
        warnings.extend(slide_warnings)
        skipped = False
    elif slide_jobs and not old_stats.get("slide_image_ocr_complete"):
        warnings.append(
            "旧课件插图 OCR 未齐全（"
            f"{old_stats.get('slides_layout_done', 0)}/{old_stats.get('slide_count', 0)} 页），"
            "请先在旧库 intake 跑图片 OCR"
        )

    layout_cache = load_lesson_slide_layout_cache(old_lesson_uid)
    slide_layout_done = 0
    slide_regions = 0
    for job in slide_jobs:
        idx = int(job.get("slide_index") or 0)
        entry = slide_layout_for_index(old_lesson_uid, idx, cache=layout_cache)
        if entry and entry.get("layout_scanned"):
            slide_layout_done += 1
            slide_regions += int(
                entry.get("region_count") or len(entry.get("regions") or [])
            )

    return {
        "engine": "old_library_slide_layout_cache",
        "slides_total": len(slide_jobs),
        "slides_layout_done": slide_layout_done,
        "image_regions": slide_regions,
        "cache_lesson_uid": old_lesson_uid,
        "skipped": skipped,
        "complete": bool(old_stats.get("slide_image_ocr_complete")),
    }, warnings


def execute_prep_image_ocr(
    ctx: dict[str, Any],
    *,
    force_slides: bool = False,
    force_textbook: bool = False,
) -> dict[str, Any]:
    """工序2：新教材插图 OCR；旧教材/旧课件插图只读旧库。"""
    new_lesson_id, new_lesson_uid = _new_lesson_ids(ctx)
    old_lesson_uid = ctx_snap(ctx)["old_les"]["lesson_uid"]
    warnings: list[str] = []

    done_img, total_pages, img_warnings = ocr_lesson_pages(
        lesson_uid=new_lesson_uid,
        ocr_scope="lesson_all",
        ocr_phase="images",
    )
    warnings.extend(img_warnings)
    refresh_lesson_body_text(lesson_id=new_lesson_id)

    old_stats = _old_library_ocr_stats(old_lesson_uid)
    if force_textbook and not old_stats.get("textbook_image_ocr_complete"):
        from ...old_library.annotate.lesson_ocr_v2 import ocr_old_lesson_images

        logger.info("dual-track image_ocr: force old textbook image OCR lesson=%s", old_lesson_uid)
        ocr_old_lesson_images(lesson_uid=old_lesson_uid, force_refresh=True)
        db.session.commit()
        old_stats = _old_library_ocr_stats(old_lesson_uid)
    elif not old_stats.get("textbook_image_ocr_complete"):
        warnings.append(
            "旧教材插图 OCR 未齐全（"
            f"{old_stats.get('image_pages_done', 0)}/{old_stats.get('page_count', 0)} 页），"
            "请先在旧库 intake 跑图片 OCR"
        )

    slide_layout_meta, slide_warnings = _old_slide_layout_snapshot(
        ctx,
        force_slides=force_slides,
    )
    warnings.extend(slide_warnings)

    db.session.commit()

    image_atoms = [
        a
        for a in TextbookAtom.query.filter_by(lesson_id=new_lesson_id).all()
        if (a.atom_type or "").lower() == "image"
    ]

    return {
        "executed": True,
        "image_ocr": {
            "engine": "opencv+doubao_layout",
            "pages_done": done_img,
            "pages_total": total_pages,
            "image_atom_count": len(image_atoms),
        },
        "old_textbook_image_ocr": {
            "engine": "old_library_atoms",
            "pages_done": int(old_stats.get("image_pages_done") or 0),
            "pages_total": int(old_stats.get("page_count") or 0),
            "image_atom_count": int(old_stats.get("image_atom_count") or 0),
            "complete": bool(old_stats.get("textbook_image_ocr_complete")),
        },
        "slide_layout_ocr": slide_layout_meta,
        "warnings": warnings[:30],
    }


def build_old_slide_layout_payload(lesson_uid: str, slides: list[Any]) -> dict[str, Any]:
    """组装 workspace 用的旧课件插图版面快照（读 slide_layout 缓存）。"""
    slide_jobs = slides
    if slides and hasattr(slides[0], "slide_index"):
        slide_jobs = [
            {
                "slide_index": int(s.slide_index),
                "blob_id": getattr(s, "blob_id", None),
            }
            for s in slides
        ]
    cache = load_lesson_slide_layout_cache(lesson_uid)
    rows: list[dict[str, Any]] = []
    total_regions = 0
    for slide in slide_jobs or []:
        if isinstance(slide, dict):
            idx = int(slide.get("slide_index") or 0)
            blob_id = slide.get("blob_id")
        else:
            idx = int(slide.slide_index)
            blob_id = getattr(slide, "blob_id", None)
        entry = slide_layout_for_index(lesson_uid, idx, cache=cache) or {}
        regions = list(entry.get("regions") or [])
        total_regions += len(regions)
        rows.append(
            {
                "slide_index": idx,
                "blob_id": blob_id,
                "layout_scanned": bool(entry.get("layout_scanned")),
                "region_count": len(regions),
                "regions": regions,
                "source": entry.get("source") or "",
            }
        )
    layout_done = sum(1 for row in rows if row.get("layout_scanned"))
    return {
        "lesson_uid": lesson_uid,
        "engine": "doubao_slide_layout",
        "slides": rows,
        "slides_total": len(rows),
        "slides_layout_done": layout_done,
        "image_regions": total_regions,
    }


def slide_layout_status_for_lesson(lesson_uid: str, slides: list[Any]) -> dict[str, Any]:
    slide_jobs = slides
    if slides and hasattr(slides[0], "slide_index"):
        slide_jobs = [
            {"slide_index": int(s.slide_index), "blob_id": getattr(s, "blob_id", None)}
            for s in slides
        ]
    slide_count = len(slide_jobs or [])
    if slide_count == 0:
        return {
            "slide_count": 0,
            "slides_layout_done": 0,
            "image_regions": 0,
            "complete": True,
        }
    cache = load_lesson_slide_layout_cache(lesson_uid)
    done = 0
    regions = 0
    for slide in slide_jobs or []:
        idx = int(slide.get("slide_index") or 0)
        entry = slide_layout_for_index(lesson_uid, idx, cache=cache)
        if entry and entry.get("layout_scanned"):
            done += 1
            regions += int(entry.get("region_count") or len(entry.get("regions") or []))
    from ...llm.slide_layout_extract import slide_layout_cache_complete

    return {
        "slide_count": slide_count,
        "slides_layout_done": done,
        "image_regions": regions,
        "complete": slide_layout_cache_complete(lesson_uid, slide_jobs),
    }
