"""清除旧库单课一键全流程结果（OCR + 整理标记 + 建块 + 磁盘缓存）。"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from ....extensions import db
from ....models import CoursewareSlide, FileBlob, LessonPage, TextbookAtom
from ...blobs import read_blob_bytes
from ...lesson_lookup import get_lesson_by_uid
from ...repo_paths import app_root
from ...llm.page_text_extract import _cache_dir as page_text_cache_dir
from ...llm.slide_layout_extract import _lesson_cache_path as slide_layout_cache_path
from ...llm.slide_text_extract import _lesson_cache_path as slide_text_cache_path
from .blocks import blocks_locked

logger = logging.getLogger(__name__)


def _page_layout_cache_dir() -> Path:
    return app_root() / "data" / "base_data" / "cache" / "page_layout"


def _safe_lesson_key(lesson_uid: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", lesson_uid)


def _delete_file(path: Path) -> bool:
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError as exc:
        logger.warning("cache delete failed %s: %s", path, exc)
    return False


def _clear_page_text_cache(lesson_uid: str) -> int:
    prefix = f"{_safe_lesson_key(lesson_uid)}_p"
    removed = 0
    cache_dir = page_text_cache_dir()
    if not cache_dir.is_dir():
        return 0
    for path in cache_dir.glob(f"{prefix}*.json"):
        if _delete_file(path):
            removed += 1
    return removed


def _clear_page_layout_cache_for_pages(pages: list[LessonPage]) -> int:
    removed = 0
    for lp in pages:
        if not lp.blob_id:
            continue
        blob = FileBlob.query.get(lp.blob_id)
        if not blob:
            continue
        try:
            data = read_blob_bytes(blob)
        except Exception as exc:
            logger.warning("page layout cache skip P%s: %s", lp.page_index, exc)
            continue
        key = hashlib.sha256(data).hexdigest()
        if _delete_file(_page_layout_cache_dir() / f"{key}.json"):
            removed += 1
    return removed


def reset_pipeline_results(*, lesson_uid: str, book_type: str = "old") -> dict[str, Any]:
    """解锁并清除本课 OCR、整理标记、建块与相关磁盘缓存，便于从头重跑全流程。"""
    from .blocks import Block

    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    lesson_id = str(les.id)
    was_locked = blocks_locked(les)

    blocks = Block.query.filter_by(lesson_id=lesson_id).all()
    block_count = len(blocks)
    for block in blocks:
        db.session.delete(block)
    if was_locked:
        les.blocks_locked_at = None

    atom_count = TextbookAtom.query.filter_by(lesson_id=lesson_id).count()
    TextbookAtom.query.filter_by(lesson_id=lesson_id).delete(
        synchronize_session=False
    )

    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    for lp in pages:
        lp.ocr_atoms_json = []
        lp.atom_lineage_json = {}

    slide_rows = CoursewareSlide.query.filter_by(lesson_id=lesson_id).count()
    CoursewareSlide.query.filter_by(lesson_id=lesson_id).update(
        {"ocr_text": None},
        synchronize_session=False,
    )

    page_text_cache_removed = _clear_page_text_cache(lesson_uid)
    page_layout_cache_removed = _clear_page_layout_cache_for_pages(pages)
    slide_text_removed = _delete_file(slide_text_cache_path(lesson_uid))
    slide_layout_removed = _delete_file(slide_layout_cache_path(lesson_uid))

    db.session.commit()
    return {
        "ok": True,
        "deleted_block_count": block_count,
        "deleted_atom_count": atom_count,
        "was_locked": was_locked,
        "blocks_locked": False,
        "cleared_slide_ocr_rows": slide_rows,
        "cleared_caches": {
            "page_text": page_text_cache_removed,
            "page_layout": page_layout_cache_removed,
            "slide_text": slide_text_removed,
            "slide_layout": slide_layout_removed,
        },
    }
