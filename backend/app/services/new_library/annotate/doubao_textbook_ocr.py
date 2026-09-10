"""双轨前置1：豆包视觉教材文字原子写库。"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from ....extensions import db
from ....models import LessonPage, TextbookAtom
from ...lesson_lookup import get_lesson_by_uid
from ...llm.page_text_extract import extract_page_text_regions
from ...old_library.annotate.atom_lineage import (
    build_ocr_snapshot_from_raw,
    init_lineage_from_extract,
)
from ...old_library.annotate.atoms import _atom_row_to_raw

logger = logging.getLogger(__name__)


def _assign_atom_codes(
    raw_atoms: list[dict[str, Any]],
    *,
    lesson_id: str,
    page_index: int,
) -> list[dict[str, Any]]:
    """为本页文字原子分配不与其他类型冲突的 atom_code。"""
    pi = int(page_index)
    prefix = f"A{pi:03d}-"
    used = {
        row.atom_code
        for row in TextbookAtom.query.filter_by(lesson_id=lesson_id, page_index=pi).all()
    }
    max_idx = 0
    for code in used:
        if not str(code).startswith(prefix):
            continue
        suffix = str(code)[len(prefix) :]
        try:
            max_idx = max(max_idx, int(suffix))
        except ValueError:
            continue
    out: list[dict[str, Any]] = []
    idx = 0
    for item in raw_atoms:
        idx += 1
        while True:
            max_idx += 1
            candidate = f"{prefix}{max_idx:03d}"
            if candidate not in used:
                used.add(candidate)
                break
        row = dict(item)
        row["atom_id"] = candidate
        out.append(row)
    return out


def _write_text_atoms_for_page(
    *,
    lesson_id: str,
    page_index: int,
    raw_atoms: list[dict[str, Any]],
) -> int:
    """替换本页 text/title 原子并更新 lesson_page 快照。"""
    pi = int(page_index)
    TextbookAtom.query.filter(
        TextbookAtom.lesson_id == lesson_id,
        TextbookAtom.page_index == pi,
        TextbookAtom.atom_type.in_(("text", "title")),
    ).delete(synchronize_session=False)
    db.session.flush()

    coded = _assign_atom_codes(raw_atoms, lesson_id=lesson_id, page_index=pi)
    written = 0
    for item in coded:
        code = str(item.get("atom_id") or "")[:16]
        if not code:
            continue
        db.session.add(
            TextbookAtom(
                lesson_id=lesson_id,
                atom_code=code,
                page_index=pi,
                atom_type=str(item.get("atom_type") or "text"),
                bbox_json={
                    "x_start": float(item.get("x_start", 0)),
                    "y_start": float(item.get("y_start", 0)),
                    "x_end": float(item.get("x_end", 1)),
                    "y_end": float(item.get("y_end", 1)),
                },
                content=(item.get("content") or "")[:2000] or None,
                ocr_text=(item.get("ocr_text") or "")[:8000] or None,
                metadata_json={"ocr_engine": "doubao_page_text"},
            )
        )
        written += 1

    lp = LessonPage.query.filter_by(lesson_id=lesson_id, page_index=pi).first()
    if lp:
        all_rows = TextbookAtom.query.filter_by(lesson_id=lesson_id, page_index=pi).all()
        snapshot_atoms = [_atom_row_to_raw(a) for a in all_rows]
        lp.ocr_atoms_json = build_ocr_snapshot_from_raw(snapshot_atoms)
        lp.atom_lineage_json = init_lineage_from_extract(snapshot_atoms)

    return written


def ocr_lesson_pages_doubao_text(
    *,
    lesson_uid: str,
    book_type: str = "new",
    force_refresh: bool = False,
) -> tuple[int, int, list[str]]:
    """对本课全部教材页跑豆包文字原子 OCR。返回 (done_pages, total_pages, warnings)。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    pages = (
        LessonPage.query.filter_by(lesson_id=les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    if not pages:
        raise ValueError("本课尚无教材页，请先解析 PDF")

    warnings: list[str] = []
    done = 0
    lesson_id = str(les.id)

    page_jobs = [
        {"page_index": int(lp.page_index), "blob_id": lp.blob_id}
        for lp in pages
    ]

    for job in page_jobs:
        pi = int(job["page_index"])
        blob_id = job.get("blob_id")
        if not blob_id:
            warnings.append(f"P{pi}：无页图 blob，跳过")
            continue

        db.session.commit()
        db.session.remove()

        raw_atoms, meta = extract_page_text_regions(
            lesson_uid=lesson_uid,
            page_index=pi,
            blob_id=blob_id,
            force_refresh=force_refresh,
        )
        if meta.get("warning"):
            warnings.append(f"P{pi}：{meta['warning']}")
        if not raw_atoms:
            warnings.append(f"P{pi}：豆包未返回文字区域")
            les = get_lesson_by_uid(lesson_uid, book_type=book_type)
            continue

        try:
            written = _write_text_atoms_for_page(
                lesson_id=lesson_id,
                page_index=pi,
                raw_atoms=raw_atoms,
            )
            db.session.commit()
            if written > 0:
                done += 1
            logger.info(
                "doubao text OCR %s P%s: %d atoms (%s)",
                lesson_uid,
                pi,
                written,
                meta.get("source"),
            )
        except IntegrityError as exc:
            db.session.rollback()
            warnings.append(f"P{pi}：写入冲突 {exc}")
        les = get_lesson_by_uid(lesson_uid, book_type=book_type)

    return done, len(pages), warnings
