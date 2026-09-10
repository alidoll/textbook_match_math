"""新教材 PDF 解析：从 PDF 识别目录划分课时 + 页码匹配 + 页图。"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from ....extensions import db
from ....models import FileBlob, Lesson, Volume
from ....parsers.catalog_logical_pages import build_uniform_catalog_page_plan
from ....parsers.catalog_page_offset import (
    get_cached_catalog_pdf_offset,
    offset_toc_entries_to_pdf,
)
from ....parsers.llm_toc_cache import get_llm_toc_entries
from ....parsers.pdf_catalog import _catalog_score
from ....parsers.pdf_pages import (
    LazyPageTexts,
    PdfOcrCache,
    compute_page_ends,
    is_pdf_text_sparse,
    refine_lesson_starts_near_hints,
    trim_backmatter_pages,
    trim_leading_unit_summary_pages,
    trim_trailing_blank_pages,
    trim_trailing_blank_pages_from_pdf,
    trim_unit_boundary_pages,
    trim_unit_summary_pages,
    trim_unit_summary_pages_from_pdf,
)
from ....parsers.pdf_spread import (
    ensure_volume_page_layout,
    page_layout_label,
    stored_page_layout,
    store_page_layout,
    view_count_from_sheets,
)
from ....parsers.pdf_toc import compute_page_ends_from_toc_entries, pick_toc_page_hint
from ....query.lesson_order import order_lessons_query
from ....repo_paths import repo_root
from ...blobs import read_blob_bytes
from ...lesson_filters import is_unit_summary_lesson
from ..catalog_bootstrap import (
    bootstrap_lessons_from_catalog,
    sync_lesson_unit_and_sort_by_pages,
)
from ..catalog_extract_pipeline import extract_new_library_catalog
from ..volumes import get_new_volume_by_code, volume_detail_dict
from ...lesson_filters import filter_master_class_lessons
from ...old_library.pdf.lesson_pages_build import _delete_lesson_pages_for_lessons, build_lesson_pages_from_parse
from ...old_library.pdf.lesson_targets import build_lesson_targets
from ...old_library.pdf.parse import _apply_lesson_updates, _compute_lesson_pages
from ...textbook_diff.page_plan import compute_diff_lesson_pages

_log = logging.getLogger(__name__)

_MIN_MATCH_RATIO = 0.65
_MIN_CATALOG_LINES = 3
# 页码划分与页图生成分离：默认仅划分页码，预览走 PDF 直链；人工确认后一键生成页图
_DEFAULT_WRITE_LESSON_PAGES = False
_DEFAULT_OCR_DPI = 96


def _friendly_parse_error(exc: Exception) -> str:
    msg = str(exc)
    if "lesson_matches" in msg and ("1451" in msg or "IntegrityError" in type(exc).__name__):
        return "无法覆盖课时：本册已有粗分/比对记录，清理关联数据失败。请刷新后重试。"
    if len(msg) > 240:
        return msg[:240] + "…"
    return msg


def _pdf_asset_for_volume(volume: Volume) -> tuple[Path, str]:
    if not volume.blob_id:
        raise ValueError("请先上传 PDF")
    blob = db.session.get(FileBlob, volume.blob_id)
    if not blob:
        raise ValueError("PDF blob 不存在")
    if blob.storage_path:
        path = repo_root() / blob.storage_path
        if path.is_file():
            return path, blob.content_hash
    data = read_blob_bytes(blob)
    tmp = Path(tempfile.gettempdir()) / f"textbook-match-{volume.volume_code}.pdf"
    tmp.write_bytes(data)
    return tmp, blob.content_hash


def _pdf_path_for_volume(volume: Volume) -> Path:
    return _pdf_asset_for_volume(volume)[0]


def _lessons_have_page_ranges(volume_id: str) -> bool:
    return (
        Lesson.query.filter(
            Lesson.volume_id == volume_id,
            Lesson.page_start.isnot(None),
            Lesson.page_end.isnot(None),
        ).count()
        > 0
    )


def _should_replace_lessons_from_catalog(
    *,
    sparse: bool,
    lesson_count: int,
    catalog: list[dict[str, Any]],
    replace_from_pdf: bool,
    volume_id: str,
) -> bool:
    if lesson_count == 0 or replace_from_pdf:
        return True
    if not sparse:
        return False
    if not _lessons_have_page_ranges(volume_id):
        return True
    if abs(len(catalog) - lesson_count) > max(2, int(lesson_count * 0.12)):
        return True
    return _catalog_score(catalog) > (lesson_count, 0, 0)


def _ensure_lessons_for_volume(
    volume: Volume,
    pdf_path: Path,
    *,
    replace_from_pdf: bool = False,
    content_hash: str | None = None,
) -> tuple[str, int]:
    """无课时时从 PDF 目录建课；显式 replace 时才覆盖已有课时。"""
    lesson_count = Lesson.query.filter_by(volume_id=volume.id).count()

    # 划分页码默认保留已有课表，避免扫描版再次走目录识别冲掉可用数据
    if lesson_count > 0 and not replace_from_pdf:
        return "existing", lesson_count

    catalog, source = extract_new_library_catalog(
        pdf_path,
        edition=volume.edition,
        grade=volume.grade,
        semester=volume.semester,
        subject=volume.subject,
        content_hash=content_hash,
        force_refresh=replace_from_pdf,
    )
    if len(catalog) < _MIN_CATALOG_LINES:
        if lesson_count > 0:
            return "existing", lesson_count
        raise ValueError(
            f"PDF 目录识别不足（{len(catalog)} 行）。请先上传整册 PDF，"
            "确认文件含可识别的目录页后再解析。"
        )

    created = bootstrap_lessons_from_catalog(
        volume=volume,
        catalog=catalog,
        replace=lesson_count > 0 or replace_from_pdf,
    )
    _log.info("新教材 %s 从 %s 创建 %d 节课", volume.volume_code, source, created)
    return source, created


def _offset_warning_label(pdf_path: Path, *, content_hash: str | None = None) -> str:
    off = get_cached_catalog_pdf_offset(pdf_path, content_hash=content_hash)
    if off is None:
        return "目录页码已按版别经验值转换"
    if off.offset_first == off.offset_default:
        return f"目录锚点校准：全书 +{off.offset_first}（{off.source}）"
    return (
        f"目录锚点校准：首课 +{off.offset_first}，其余 +{off.offset_default}（{off.source}）"
    )


def _compute_lesson_pages_from_llm_catalog(
    *,
    lessons: list[Lesson],
    pdf_path: Path,
    edition_label: str | None,
    ocr_dpi: int,
    content_hash: str | None = None,
    force_recalibrate: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    """
    豆包目录 → 印刷页码转 PDF 物理页 → 旧库 llm_vision 快路径（无整册 OCR）。
    起止页均基于偏移后的目录锚点，避免印刷页与 PDF 页混用。
    """
    entries = get_llm_toc_entries(pdf_path, content_hash=content_hash)
    if not entries:
        raise ValueError(
            "划分页码需要先有大模型目录缓存。请先完成「识别目录」，"
            "或重新上传 PDF 后先识别目录再划分页码。"
        )

    import fitz

    with fitz.open(str(pdf_path)) as doc:
        total_pages = len(doc)

    work_indices = [
        i for i, les in enumerate(lessons) if not is_unit_summary_lesson(les)
    ]
    lessons_work = [lessons[i] for i in work_indices]
    targets = build_lesson_targets(lessons)
    plan = build_uniform_catalog_page_plan(
        pdf_path=pdf_path,
        entries=entries,
        lessons=lessons,
        targets=targets,
        edition_label=edition_label,
        ocr_dpi=ocr_dpi,
        total_pages=total_pages,
        content_hash=content_hash,
        force_recalibrate=force_recalibrate,
    )
    if plan is None:
        return None

    uniform_x, _entries_physical, starts_full, ends_full = plan

    targets_work = build_lesson_targets(lessons_work)
    starts_0 = [starts_full[i] for i in work_indices]
    ends_1 = [ends_full[i] for i in work_indices]

    ends_1 = trim_unit_summary_pages_from_pdf(
        pdf_path, targets_work, starts_0, ends_1, ocr_dpi=min(ocr_dpi, 96)
    )
    ends_1 = trim_trailing_blank_pages_from_pdf(pdf_path, starts_0, ends_1)

    for j, orig_i in enumerate(work_indices):
        starts_full[orig_i] = starts_0[j]
        ends_full[orig_i] = ends_1[j]

    warnings = [
        f"统一目录偏移 x={uniform_x}（PDF 物理页 = 目录逻辑页 + x），按锚点划分起止",
    ]
    match_details: list[dict[str, Any]] = []
    row_updates: list[dict[str, Any]] = []
    matched = 0

    for les, start_0, end_1 in zip(lessons, starts_full, ends_full, strict=True):
        detail: dict[str, Any] = {
            "lesson_uid": les.lesson_uid,
            "lesson_name": les.lesson_name,
            "page_start": None,
            "page_end": None,
            "matched": False,
        }
        if is_unit_summary_lesson(les):
            match_details.append(detail)
            continue
        if start_0 is not None and end_1 is not None:
            page_start = start_0 + 1
            page_end = end_1
            if page_end >= page_start:
                row_updates.append(
                    {
                        "lesson_id": les.id,
                        "page_start": page_start,
                        "page_end": page_end,
                        "body_text": None,
                    }
                )
                matched += 1
                detail.update(
                    {"page_start": page_start, "page_end": page_end, "matched": True}
                )
        match_details.append(detail)

    meta = {
        "matched": matched,
        "lesson_count": len(lessons),
        "text_mode": "llm_catalog_uniform_x",
        "total_pages": total_pages,
        "catalog_lines": len(entries),
        "warnings": warnings,
    }
    _log.info(
        "新教材 %s 目录偏移快路径：匹配 %d/%d 课",
        pdf_path.name,
        matched,
        len(lessons),
    )
    return meta, row_updates, match_details


def _compute_lesson_pages_from_llm_toc(
    *,
    lessons: list[Lesson],
    pdf_path: Path,
    edition_label: str | None,
    ocr_dpi: int,
    content_hash: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None:
    """
    扫描版：锚点校准偏移 → 课名 OCR 定位起始页 → 边界页修剪（单元扉页/小结/后记）。
    """
    entries = get_llm_toc_entries(pdf_path, content_hash=content_hash)
    if not entries:
        return None

    import fitz

    with fitz.open(str(pdf_path)) as doc:
        total_pages = len(doc)

    targets = build_lesson_targets(lessons)
    work_indices = [
        i for i, les in enumerate(lessons) if not is_unit_summary_lesson(les)
    ]
    lessons_work = [lessons[i] for i in work_indices]
    targets_work = [targets[i] for i in work_indices]

    entries_pdf = offset_toc_entries_to_pdf(
        entries,
        pdf_path=pdf_path,
        lessons=lessons,
        targets=targets,
        edition_label=edition_label,
        ocr_dpi=ocr_dpi,
        content_hash=content_hash,
    )

    hints: list[int | None] = []
    for les in lessons_work:
        hints.append(
            pick_toc_page_hint(
                entries_pdf,
                les.unit_title or "",
                les.lesson_name or "",
                str(les.lesson_no or ""),
            )
        )
    hint_count = sum(1 for h in hints if h is not None)
    if hint_count < max(3, int(len(lessons_work) * 0.75)) or not all(h is not None for h in hints):
        _log.info(
            "LLM 页码提示不足（%d/%d），无法走快路径",
            hint_count,
            len(lessons_work),
        )
        return None

    hints_1based = [int(h) for h in hints]
    starts_0 = [h - 1 for h in hints_1based]

    ocr_cache = PdfOcrCache(pdf_path, dpi=min(ocr_dpi, 96))
    pages_text = LazyPageTexts(ocr_cache)
    try:
        starts_0 = refine_lesson_starts_near_hints(
            pages_text, targets_work, hints_1based
        )
        ends_1 = compute_page_ends(starts_0, total_pages)
        starts_0 = trim_leading_unit_summary_pages(
            pages_text, targets_work, starts_0, ends_1
        )
        starts_0, _ = trim_unit_boundary_pages(
            pages_text,
            targets_work,
            starts_0,
            ends_1,
            trim_trailing=False,
        )
        ends_1 = compute_page_ends(starts_0, total_pages)
        _, ends_1 = trim_unit_boundary_pages(
            pages_text,
            targets_work,
            starts_0,
            ends_1,
            trim_leading=False,
        )
        ends_1 = trim_unit_summary_pages(
            pages_text, targets_work, starts_0, ends_1
        )
        ends_1 = trim_backmatter_pages(pages_text, starts_0, ends_1)
        ends_1 = trim_trailing_blank_pages(
            pages_text, starts_0, ends_1, pdf_path=pdf_path
        )
    finally:
        ocr_cache.close()

    ends_1 = trim_unit_summary_pages_from_pdf(
        pdf_path, targets_work, starts_0, ends_1, ocr_dpi=min(ocr_dpi, 96)
    )
    ends_1 = trim_trailing_blank_pages_from_pdf(pdf_path, starts_0, ends_1)

    starts_full: list[int | None] = [None] * len(lessons)
    ends_full: list[int | None] = [None] * len(lessons)
    for j, orig_i in enumerate(work_indices):
        starts_full[orig_i] = starts_0[j]
        ends_full[orig_i] = ends_1[j]

    warnings = [
        f"扫描版：{_offset_warning_label(pdf_path, content_hash=content_hash)} + 课名 OCR 定位起始页，"
        "并修剪单元扉页/小结",
    ]
    match_details: list[dict[str, Any]] = []
    row_updates: list[dict[str, Any]] = []
    matched = 0

    for les, start_0, end_1 in zip(lessons, starts_full, ends_full, strict=True):
        detail: dict[str, Any] = {
            "lesson_uid": les.lesson_uid,
            "lesson_name": les.lesson_name,
            "page_start": None,
            "page_end": None,
            "matched": False,
        }
        if is_unit_summary_lesson(les):
            match_details.append(detail)
            continue
        if start_0 is not None and end_1 is not None:
            page_start = start_0 + 1
            page_end = end_1
            if page_end >= page_start:
                row_updates.append(
                    {
                        "lesson_id": les.id,
                        "page_start": page_start,
                        "page_end": page_end,
                        "body_text": None,
                    }
                )
                matched += 1
                detail.update(
                    {"page_start": page_start, "page_end": page_end, "matched": True}
                )
        match_details.append(detail)

    meta = {
        "matched": matched,
        "lesson_count": len(lessons),
        "text_mode": "llm_catalog+xk_offset+header_ocr",
        "total_pages": total_pages,
        "catalog_lines": 0,
        "warnings": warnings,
    }
    _log.info("新教材 %s LLM 页码快路径：匹配 %d/%d 课", pdf_path.name, matched, len(lessons))
    return meta, row_updates, match_details


def parse_volume_pdf(
    *,
    volume_code: str,
    skip_body_text: bool = False,
    ocr_dpi: int = _DEFAULT_OCR_DPI,
    skip_pages: int = 3,
    replace_lessons: bool = False,
    write_lesson_pages: bool = _DEFAULT_WRITE_LESSON_PAGES,
    force_recalibrate: bool = False,
) -> dict:
    volume = get_new_volume_by_code(volume_code)
    pdf_path, content_hash = _pdf_asset_for_volume(volume)

    if force_recalibrate:
        from ....parsers.catalog_page_offset import clear_cached_catalog_pdf_offset

        clear_cached_catalog_pdf_offset(pdf_path, content_hash=content_hash)

    if volume.parse_status == "processing":
        _log.warning("新教材 %s 上次解析未完成，重新执行", volume_code)

    volume.parse_status = "processing"
    volume.parse_error = None
    db.session.commit()

    try:
        lesson_source, _ = _ensure_lessons_for_volume(
            volume,
            pdf_path,
            replace_from_pdf=replace_lessons,
            content_hash=content_hash,
        )
        db.session.flush()

        lessons = filter_master_class_lessons(
            order_lessons_query(
                Lesson.query.filter_by(volume_id=volume.id)
            ).all()
        )
        if not lessons:
            raise ValueError("尚无课时，请先上传 PDF 并完成「识别目录」")

        layout = ensure_volume_page_layout(
            volume, pdf_path=pdf_path, force=False, commit=False
        )
        import fitz

        with fitz.open(str(pdf_path)) as doc:
            sheet_count = len(doc)
        system_total = view_count_from_sheets(sheet_count, layout)
        _log.info(
            "新教材 %s 页布局=%s（%s），sheet=%d → 系统页=%d",
            volume_code,
            layout,
            page_layout_label(layout),
            sheet_count,
            system_total,
        )

        meta: dict[str, Any]
        row_updates: list[dict[str, Any]]
        match_details: list[dict[str, Any]]

        if layout == "spread":
            # 与语文对开同一套：系统页=view，生成页图时裁左右半
            spread_plan = compute_diff_lesson_pages(
                lessons=lessons,
                pdf_path=pdf_path,
                content_hash=content_hash,
                total_pages=system_total,
                force_offset=True,
                layout="spread",
            )
            if spread_plan is None:
                raise ValueError(
                    "对开教材页码划分失败：需要有效的目录页码缓存。"
                    "请先完成「识别目录」后再划分页码。"
                )
            meta, row_updates, match_details = spread_plan
            matched = meta["matched"]
            ratio = matched / max(len(lessons), 1)
        else:
            fast = None
            if is_pdf_text_sparse(pdf_path):
                fast = _compute_lesson_pages_from_llm_catalog(
                    lessons=lessons,
                    pdf_path=pdf_path,
                    edition_label=volume.edition,
                    ocr_dpi=ocr_dpi,
                    content_hash=content_hash,
                    force_recalibrate=force_recalibrate,
                )
            if fast is not None:
                meta, row_updates, match_details = fast
                matched = meta["matched"]
                ratio = matched / max(len(lessons), 1)
            else:
                meta, row_updates, match_details = _compute_lesson_pages(
                    lessons=lessons,
                    pdf_path=pdf_path,
                    skip_body_text=True,
                    ocr_dpi=ocr_dpi,
                    skip_pages=skip_pages,
                    edition_label=volume.edition,
                    grade=volume.grade,
                    semester=volume.semester,
                )
                matched = meta["matched"]
                ratio = matched / max(len(lessons), 1)
                text_mode = meta.get("text_mode") or ""
                used_fast = text_mode in ("llm_vision_pages", "canonical_pages")
                if ratio < _MIN_MATCH_RATIO or not used_fast:
                    _log.info(
                        "新教材 %s 目录偏移快路径不可用，回退 OCR 精修（%d/%d, mode=%s）",
                        volume_code,
                        matched,
                        len(lessons),
                        text_mode,
                    )
                    fallback = _compute_lesson_pages_from_llm_toc(
                        lessons=lessons,
                        pdf_path=pdf_path,
                        edition_label=volume.edition,
                        ocr_dpi=ocr_dpi,
                        content_hash=content_hash,
                    )
                    if fallback is not None:
                        meta, row_updates, match_details = fallback
                        matched = meta["matched"]
                        ratio = matched / max(len(lessons), 1)

        meta = dict(meta)
        meta["page_layout"] = layout
        meta["page_layout_label"] = page_layout_label(layout)
        meta["sheet_count"] = sheet_count
        meta["system_total_pages"] = system_total

        if ratio < _MIN_MATCH_RATIO:
            volume = get_new_volume_by_code(volume_code)
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
                "lesson_source": lesson_source,
                **meta,
                "lessons": match_details,
            }

        volume = get_new_volume_by_code(volume_code)
        # 保留 page_layout，勿整表清空（否则页图会按单页误渲整张对开）
        layout_meta = None
        sug = dict(volume.parse_suggestions_json or {})
        if isinstance(sug.get("page_layout_meta"), dict):
            layout_meta = sug.get("page_layout_meta")
        volume.parse_suggestions_json = None
        store_page_layout(volume, layout, meta=layout_meta)

        cleared_ids, applied_updates = _apply_lesson_updates(
            volume.id, row_updates, respect_verified=True
        )
        if cleared_ids:
            _delete_lesson_pages_for_lessons(cleared_ids)

        sync_lesson_unit_and_sort_by_pages(volume_id=volume.id)

        page_rows = 0
        if applied_updates and write_lesson_pages:
            page_rows = build_lesson_pages_from_parse(
                volume=volume,
                pdf_path=pdf_path,
                row_updates=applied_updates,
                render_dpi=max(ocr_dpi, 100),
            )
        elif applied_updates:
            _log.info(
                "新教材 %s 已匹配页码（layout=%s），跳过批量页图（预览用 PDF 直链）",
                volume_code,
                layout,
            )

        volume = get_new_volume_by_code(volume_code)
        # 再次确保 layout 仍在（sync/apply 不应清掉）
        if stored_page_layout(volume) != layout:
            store_page_layout(volume, layout, meta=layout_meta)
        volume.parse_status = "done"
        volume.parse_error = None
        db.session.commit()

        result = volume_detail_dict(volume)
        result.update(
            {
                "ok": True,
                "lesson_source": lesson_source,
                "write_lesson_pages": write_lesson_pages,
                **meta,
                "parse_details": match_details,
                "lessons": match_details,
                "lesson_pages_written": page_rows,
            }
        )
        return result
    except Exception as exc:
        db.session.rollback()
        volume = get_new_volume_by_code(volume_code)
        volume.parse_status = "failed"
        err = _friendly_parse_error(exc)[:2000]
        volume.parse_error = err
        db.session.commit()
        _log.exception("新教材解析失败 %s", volume_code)
        result = volume_detail_dict(volume)
        result.update({"ok": False, "parse_error": err, "error": err})
        return result
