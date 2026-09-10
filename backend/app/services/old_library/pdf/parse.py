"""旧库册次 PDF 解析：页码 + body_text。"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from ....extensions import db
from ....models import FileBlob, Lesson, Volume
from ....parsers.canonical_catalog import resolve_page_hints_for_volume
from ....parsers.llm_toc_cache import get_llm_toc_entries
from ....parsers.pdf_catalog import extract_catalog_from_pdf
from ....parsers.pdf_pages import (
    body_text_for_range,
    compute_page_ends,
    extract_all_page_texts,
    find_lesson_start_pages,
    find_lesson_starts_with_hints,
    is_pdf_text_sparse,
    LazyPageTexts,
    PdfOcrCache,
    trim_unit_boundary_pages,
    trim_backmatter_pages,
    trim_trailing_blank_pages,
    trim_trailing_blank_pages_from_pdf,
    trim_unit_summary_pages,
    trim_unit_summary_pages_from_pdf,
)
from ....parsers.pdf_toc import compute_page_ends_from_toc_entries
from ....repo_paths import old_textbook_mirror_path, repo_root
from ...blobs import read_blob_bytes
from ...lesson_filters import is_unit_summary_lesson
from ....query.lesson_order import order_lessons_query
from ..volumes import get_old_volume_by_code, volume_detail_dict
from .lesson_pages_build import _delete_lesson_pages_for_lessons, build_lesson_pages_from_parse
from .lesson_targets import build_lesson_targets

_log = logging.getLogger(__name__)

_MIN_MATCH_RATIO = 0.65


def _pdf_path_for_volume(volume: Volume) -> Path:
    if not volume.blob_id:
        raise ValueError("请先上传 PDF")
    blob = FileBlob.query.get(volume.blob_id)
    if not blob:
        raise ValueError("PDF blob 不存在")

    candidates: list[Path] = [old_textbook_mirror_path(volume.edition, volume.volume_code)]
    if blob.storage_path:
        stored = repo_root() / blob.storage_path
        if stored not in candidates:
            candidates.append(stored)

    expected = int(blob.size_bytes or 0)

    def _size_ok(path: Path) -> bool:
        if not path.is_file():
            return False
        if expected <= 0:
            return True
        return path.stat().st_size == expected

    for path in candidates:
        if _size_ok(path):
            return path
    for path in candidates:
        if path.is_file():
            return path
    data = read_blob_bytes(blob)
    tmp = Path(tempfile.gettempdir()) / f"textbook-match-{volume.volume_code}.pdf"
    tmp.write_bytes(data)
    return tmp


def _compute_lesson_pages(
    *,
    lessons: list[Lesson],
    pdf_path: Path,
    skip_body_text: bool,
    ocr_dpi: int,
    skip_pages: int,
    edition_label: str | None = None,
    grade: int | None = None,
    semester: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[str] = []
    targets = build_lesson_targets(lessons)
    work_indices = [
        i for i, les in enumerate(lessons) if not is_unit_summary_lesson(les)
    ]
    if len(work_indices) < len(lessons):
        warnings.append("单元小结课时不参与页码解析")
    lessons_work = [lessons[i] for i in work_indices]
    targets_work = [targets[i] for i in work_indices]
    ocr_cache: PdfOcrCache | None = None
    pages_text: list[str] | LazyPageTexts
    text_mode = "text"
    hints: list[int | None] | None = None
    hint_source: str | None = None

    use_scanned_hints = (
        edition_label
        and grade is not None
        and semester
        and is_pdf_text_sparse(pdf_path)
    )
    canonical_fast = False
    if use_scanned_hints:
        resolved = resolve_page_hints_for_volume(
            lessons_work,
            edition_label=edition_label,
            grade=grade,
            semester=semester,
            pdf_path=pdf_path,
        )
        if resolved:
            hints, hint_source = resolved
            hint_count = sum(1 for h in hints if h is not None)
            if hint_source in ("canonical", "llm_vision") and all(h is not None for h in hints):
                import fitz

                with fitz.open(str(pdf_path)) as doc:
                    total_pages = len(doc)
                pages_text = []
                text_mode = f"{hint_source}_pages"
                canonical_fast = True
                label = "大模型目录页码" if hint_source == "llm_vision" else "定稿页码表"
                warnings.append(
                    f"扫描版 PDF：使用{label}（{hint_count}/{len(lessons_work)} 课），无需 OCR"
                )
            elif hint_count >= max(1, len(lessons) * 0.5):
                ocr_cache = PdfOcrCache(pdf_path, dpi=ocr_dpi)
                pages_text = LazyPageTexts(ocr_cache)
                text_mode = f"ocr_hints_{hint_source}"
                warnings.append(
                    f"扫描版 PDF：使用 {hint_source} 页码提示（{hint_count}/{len(lessons_work)} 课），"
                    "按页 lazy OCR，无需整册识别"
                )

    if not canonical_fast and ocr_cache is None:
        pages_text, text_mode = extract_all_page_texts(pdf_path, ocr_dpi=ocr_dpi)

    if not canonical_fast:
        total_pages = len(pages_text)

    if canonical_fast or (ocr_cache is not None and hint_source == "canonical"):
        catalog: list[dict[str, Any]] = []
    else:
        catalog = extract_catalog_from_pdf(pdf_path)
    if catalog and abs(len(catalog) - len(lessons)) > max(3, len(lessons) * 0.3):
        warnings.append(
            f"PDF 目录约 {len(catalog)} 行，基准课时 {len(lessons)} 节，差异较大"
        )

    try:
        if canonical_fast and hints is not None:
            starts_0 = [h - 1 for h in hints]
            if hint_source == "llm_vision":
                toc_entries = get_llm_toc_entries(pdf_path) or []
                if toc_entries:
                    from ....parsers.catalog_page_offset import offset_toc_entries_to_pdf

                    toc_entries = offset_toc_entries_to_pdf(
                        toc_entries,
                        pdf_path=pdf_path,
                        lessons=lessons,
                        targets=targets,
                        edition_label=edition_label,
                        ocr_dpi=min(ocr_dpi, 96),
                    )
                    ends_1 = compute_page_ends_from_toc_entries(
                        hints, toc_entries, total_pages=total_pages
                    )
                else:
                    ends_1 = compute_page_ends(starts_0, total_pages)
                    ends_1 = trim_unit_summary_pages_from_pdf(
                        pdf_path, targets_work, starts_0, ends_1, ocr_dpi=min(ocr_dpi, 96)
                    )
            else:
                ends_1 = compute_page_ends(starts_0, total_pages)
                ends_1 = trim_unit_summary_pages_from_pdf(
                    pdf_path, targets_work, starts_0, ends_1, ocr_dpi=min(ocr_dpi, 96)
                )
            ends_1 = trim_trailing_blank_pages_from_pdf(pdf_path, starts_0, ends_1)
            skip_body_text = True
        elif ocr_cache is not None and hints is not None:
            starts_0 = find_lesson_starts_with_hints(
                pages_text, targets_work, hints, skip_pages=skip_pages
            )
            ends_1 = compute_page_ends(
                starts_0, total_pages, pages_text=pages_text, targets=targets_work
            )
            starts_0, ends_1 = trim_unit_boundary_pages(
                pages_text, targets_work, starts_0, ends_1
            )
            ends_1 = trim_backmatter_pages(pages_text, starts_0, ends_1)
            ends_1 = trim_unit_summary_pages(
                pages_text, targets_work, starts_0, ends_1
            )
            ends_1 = trim_trailing_blank_pages(
                pages_text, starts_0, ends_1, pdf_path=pdf_path
            )
            if not skip_body_text:
                warnings.append(
                    "扫描版按页 OCR 模式已跳过 body_text 提取（避免整册二次识别）"
                )
                skip_body_text = True
        else:
            starts_0 = find_lesson_start_pages(
                pages_text, targets_work, skip_pages=skip_pages
            )
            ends_1 = compute_page_ends(
                starts_0, total_pages, pages_text=pages_text, targets=targets_work
            )
            starts_0, ends_1 = trim_unit_boundary_pages(
                pages_text, targets_work, starts_0, ends_1
            )
            ends_1 = trim_backmatter_pages(pages_text, starts_0, ends_1)
            ends_1 = trim_unit_summary_pages(
                pages_text, targets_work, starts_0, ends_1
            )
            ends_1 = trim_trailing_blank_pages(
                pages_text, starts_0, ends_1, pdf_path=pdf_path
            )
    finally:
        if ocr_cache is not None:
            ocr_cache.close()

    starts_full: list[int | None] = [None] * len(lessons)
    ends_full: list[int | None] = [None] * len(lessons)
    for j, orig_i in enumerate(work_indices):
        starts_full[orig_i] = starts_0[j]
        ends_full[orig_i] = ends_1[j]
    starts_0 = starts_full
    ends_1 = ends_full

    match_details: list[dict[str, Any]] = []
    row_updates: list[dict[str, Any]] = []
    matched = 0

    for les, _tgt, start_0, end_1 in zip(lessons, targets, starts_0, ends_1, strict=True):
        detail: dict[str, Any] = {
            "lesson_uid": les.lesson_uid,
            "lesson_name": les.lesson_name,
            "page_start": None,
            "page_end": None,
            "matched": False,
        }
        if start_0 is not None and end_1 is not None:
            if is_unit_summary_lesson(les):
                warnings.append(f"单元小结已跳过：{les.lesson_name}")
                match_details.append(detail)
                continue
            page_start = start_0 + 1
            page_end = end_1
            if page_end < page_start:
                warnings.append(
                    f"页码范围无效（修剪后为空）：{les.unit_title} · {les.lesson_name}"
                )
                match_details.append(detail)
                continue
            row_updates.append(
                {
                    "lesson_id": les.id,
                    "page_start": page_start,
                    "page_end": page_end,
                    "body_text": (
                        None
                        if skip_body_text
                        else body_text_for_range(pages_text, page_start, page_end)
                    ),
                }
            )
            matched += 1
            detail.update(
                {"page_start": page_start, "page_end": page_end, "matched": True}
            )
        else:
            warnings.append(f"未定位页码：{les.unit_title} · {les.lesson_name}")
        match_details.append(detail)

    meta = {
        "matched": matched,
        "lesson_count": len(lessons),
        "text_mode": text_mode,
        "total_pages": total_pages,
        "catalog_lines": len(catalog),
        "warnings": warnings,
    }
    return meta, row_updates, match_details


def _apply_lesson_updates(
    volume_id: str,
    row_updates: list[dict[str, Any]],
    *,
    respect_verified: bool = True,
) -> tuple[list[str], list[dict[str, Any]]]:
    """
    写入本次匹配结果；未匹配课时清空页码。
    respect_verified=True 时跳过已人工保存的课时（再次解析不覆盖）。
    返回 (需清空页图的 lesson_id, 实际写入的 row_updates)。
    """
    updated_ids = {row["lesson_id"] for row in row_updates}
    cleared_ids: list[str] = []
    applied: list[dict[str, Any]] = []

    for les in Lesson.query.filter_by(volume_id=volume_id).all():
        if respect_verified and les.page_range_verified:
            continue
        if les.id in updated_ids:
            row = next(r for r in row_updates if r["lesson_id"] == les.id)
            les.page_start = row["page_start"]
            les.page_end = row["page_end"]
            les.body_text = row.get("body_text")
            les.page_range_verified = False
            applied.append(row)
        else:
            if les.page_start or les.page_end or les.body_text:
                cleared_ids.append(les.id)
            les.page_start = None
            les.page_end = None
            les.body_text = None

    return cleared_ids, applied


def parse_volume_pdf(
    *,
    volume_code: str,
    skip_body_text: bool = False,
    ocr_dpi: int = 120,
    skip_pages: int = 3,
) -> dict:
    volume = get_old_volume_by_code(volume_code)
    lessons = order_lessons_query(
        Lesson.query.filter_by(volume_id=volume.id)
    ).all()
    if not lessons:
        from ..volumes import edition_meta_for_volume

        _eid, _subj, has_bench = edition_meta_for_volume(volume)
        if has_bench:
            raise ValueError("尚无课时，请先载入基准目录")
        # 化学等无基准库：从 PDF 目录建课后再匹配页码（对齐新库识目录）
        pdf_path = _pdf_path_for_volume(volume)
        from ...new_library.pdf.parse import _ensure_lessons_for_volume

        content_hash = None
        if volume.blob_id:
            from ....models import FileBlob

            row = FileBlob.query.get(volume.blob_id)
            content_hash = getattr(row, "content_hash", None) if row else None
        _ensure_lessons_for_volume(
            volume,
            pdf_path,
            replace_from_pdf=False,
            content_hash=content_hash,
        )
        lessons = order_lessons_query(
            Lesson.query.filter_by(volume_id=volume.id)
        ).all()
        if not lessons:
            raise ValueError(
                "尚无课时：PDF 目录识别未得到课表。请确认 PDF 含可识别目录页后再解析。"
            )

    pdf_path = _pdf_path_for_volume(volume)
    volume.parse_status = "processing"
    volume.parse_error = None
    db.session.commit()

    try:
        # 必须传入版次/年级/册次，扫描版才能走定稿页码/LLM 目录快路径；
        # 否则会整册 OCR，一册可卡数十分钟，一键本版解析看起来「卡死」。
        meta, row_updates, match_details = _compute_lesson_pages(
            lessons=lessons,
            pdf_path=pdf_path,
            skip_body_text=skip_body_text,
            ocr_dpi=ocr_dpi,
            skip_pages=skip_pages,
            edition_label=volume.edition,
            grade=volume.grade,
            semester=volume.semester,
        )
        matched = meta["matched"]
        ratio = matched / max(len(lessons), 1)

        if ratio < _MIN_MATCH_RATIO:
            volume = get_old_volume_by_code(volume_code)
            volume.parse_status = "failed"
            volume.parse_error = (
                f"仅匹配 {matched}/{len(lessons)} 节课（{ratio:.0%}），"
                f"低于阈值 {_MIN_MATCH_RATIO:.0%}"
            )
            db.session.commit()
            return {
                "ok": False,
                "volume_code": volume.volume_code,
                "parse_status": volume.parse_status,
                "parse_error": volume.parse_error,
                **meta,
                "lessons": match_details,
            }

        volume = get_old_volume_by_code(volume_code)
        volume.parse_suggestions_json = None
        cleared_ids, applied_updates = _apply_lesson_updates(
            volume.id, row_updates, respect_verified=True
        )
        if cleared_ids:
            _delete_lesson_pages_for_lessons(cleared_ids)
        volume = get_old_volume_by_code(volume_code)
        page_rows = 0
        if applied_updates:
            page_rows = build_lesson_pages_from_parse(
                volume=volume,
                pdf_path=pdf_path,
                row_updates=applied_updates,
                render_dpi=max(ocr_dpi, 100),
            )
        volume = get_old_volume_by_code(volume_code)
        volume.parse_status = "done"
        volume.parse_error = None
        db.session.commit()

        result = volume_detail_dict(volume)
        result.update(
            {
                "ok": True,
                **meta,
                "parse_details": match_details,
                "lessons": match_details,
                "lesson_pages_written": page_rows,
            }
        )
        return result
    except Exception as exc:
        db.session.rollback()
        volume = get_old_volume_by_code(volume_code)
        volume.parse_status = "failed"
        volume.parse_error = str(exc)[:2000]
        db.session.commit()
        _log.exception("解析失败 %s", volume_code)
        raise
