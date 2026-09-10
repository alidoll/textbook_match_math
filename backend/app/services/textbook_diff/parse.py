"""教材对比 PDF 解析：页码划分（目录逻辑页 + 自动校准偏移）。"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from ...extensions import db
from ...models import FileBlob, Lesson, Volume
from ...query.lesson_order import order_lessons_query
from ..blobs import read_blob_bytes
from ..lesson_filters import filter_master_class_lessons
from ..new_library.pdf.parse import _friendly_parse_error
from ..old_library.pdf.parse import _apply_lesson_updates
from ..old_library.pdf.lesson_pages_build import _delete_lesson_pages_for_lessons
from .intake_parse_pipeline import try_ocr_refine_diff_pages
from .page_plan import compute_diff_lesson_pages
from .toc_cache import ensure_diff_toc_page_cache
from .volumes import diff_volume_detail, get_diff_volume_by_code

_log = logging.getLogger(__name__)

_MIN_MATCH_RATIO = 0.65


def _pdf_asset_for_volume(volume: Volume) -> tuple[Path, str]:
    from ..volume_pdf import resolve_volume_pdf_path

    if not volume.blob_id:
        raise ValueError("请先上传完整版 PDF，再划分页码")
    pdf_path = resolve_volume_pdf_path(volume, source="full")
    blob = db.session.get(FileBlob, volume.blob_id)
    if not blob:
        raise ValueError("PDF blob 不存在")
    return pdf_path, blob.content_hash


def _pdf_path_for_volume(volume: Volume) -> Path:
    return _pdf_asset_for_volume(volume)[0]


def parse_diff_volume_pdf(
    *,
    volume_code: str,
    replace_lessons: bool = False,
    force_recalibrate: bool = False,
) -> dict:
    """划分页码：自动校准 x，使 PDF 物理页 = 目录逻辑页 + x。"""
    volume = get_diff_volume_by_code(volume_code)

    volume.parse_status = "processing"
    volume.parse_error = None
    db.session.commit()

    try:
        pdf_path, content_hash = _pdf_asset_for_volume(volume)

        if force_recalibrate:
            from ...parsers.catalog_page_offset import clear_cached_catalog_pdf_offset

            clear_cached_catalog_pdf_offset(pdf_path, content_hash=content_hash)

        lessons = filter_master_class_lessons(
            order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
        )
        if not lessons:
            raise ValueError("尚无课时，请先上传 PDF 并完成「识别目录」")

        import fitz
        from ...parsers.pdf_spread import (
            ensure_volume_page_layout,
            view_count_from_sheets,
        )

        layout = ensure_volume_page_layout(
            volume, pdf_path=pdf_path, force=False, commit=True
        )
        with fitz.open(str(pdf_path)) as doc:
            sheet_count = len(doc)
        total_pages = view_count_from_sheets(sheet_count, layout)

        toc_n = ensure_diff_toc_page_cache(
            pdf_path,
            lessons=lessons,
            content_hash=content_hash,
            force=True,
            layout=layout,
        )
        if toc_n < 3:
            raise ValueError(
                "划分页码需要目录印刷页码。请点「重新预处理」重新识别完整目录"
                "（须从第一单元起，且每课有右侧页码），不要只划分页码。"
            )

        result_plan = compute_diff_lesson_pages(
            lessons=lessons,
            pdf_path=pdf_path,
            content_hash=content_hash,
            total_pages=total_pages,
            force_offset=True,
            layout=layout,
        )
        if result_plan is None:
            result_plan = try_ocr_refine_diff_pages(
                lessons=lessons,
                pdf_path=pdf_path,
                content_hash=content_hash,
                layout=layout,
                edition_label=volume.edition,
            )
        if result_plan is None:
            raise ValueError(
                "无法自动校准目录偏移或划分页码。"
                "请确认已「识别目录」且 PDF 含可提取文字层；"
                "扫描版需 OCR 或手工填写页码。"
            )

        meta, row_updates, match_details = result_plan
        matched = meta["matched"]
        ratio = matched / max(len(lessons), 1)

        if ratio < _MIN_MATCH_RATIO:
            refined = try_ocr_refine_diff_pages(
                lessons=lessons,
                pdf_path=pdf_path,
                content_hash=content_hash,
                layout=layout,
                edition_label=volume.edition,
            )
            if refined is not None:
                meta2, _, _ = refined
                if meta2["matched"] > matched:
                    result_plan = refined
                    meta, row_updates, match_details = result_plan
                    matched = meta["matched"]
                    ratio = matched / max(len(lessons), 1)

        if ratio < _MIN_MATCH_RATIO:
            volume = get_diff_volume_by_code(volume_code)
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

        volume = get_diff_volume_by_code(volume_code)
        # 保留 page_layout 等检测结果，勿整表清空
        prev_sug = dict(volume.parse_suggestions_json or {})
        volume.parse_suggestions_json = {
            k: prev_sug[k]
            for k in ("page_layout", "page_layout_meta")
            if k in prev_sug
        } or None
        cleared_ids, _applied = _apply_lesson_updates(
            volume.id, row_updates, respect_verified=True
        )
        if cleared_ids:
            _delete_lesson_pages_for_lessons(cleared_ids)

        volume = get_diff_volume_by_code(volume_code)
        volume.parse_status = "done"
        volume.parse_error = None
        db.session.commit()

        result = diff_volume_detail(volume)
        result.update(
            {
                "ok": True,
                **meta,
                "lessons": match_details,
            }
        )
        return result
    except Exception as exc:
        db.session.rollback()
        volume = get_diff_volume_by_code(volume_code)
        volume.parse_status = "failed"
        err = _friendly_parse_error(exc)[:2000]
        volume.parse_error = err
        db.session.commit()
        result = diff_volume_detail(volume)
        result.update({"ok": False, "parse_error": err})
        return result
