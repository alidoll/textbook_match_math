"""教材对比：从 PDF 识别目录。"""
from __future__ import annotations

import json
import logging
import re

from ...extensions import db
from ...models import FileBlob, Volume
from ...parsers.llm_toc_cache import clear_llm_toc_cache
from ..new_library.catalog_bootstrap import _edition_for_volume
from .volumes import diff_volume_detail, get_diff_volume_by_code

_log = logging.getLogger(__name__)
_MIN_CATALOG_LINES = 3


def _bootstrap_lessons_preserve_order(volume, catalog, replace=False):
    """将目录行写入 lessons 表，保留目录行原始顺序（不打乱）。"""
    from ...models import Lesson
    from ...parsers.catalog_lesson_line import (
        is_yuwen_subtitle_line,
        parse_catalog_lesson_fields,
        strip_yuwen_subtitle_label,
    )
    from ...parsers.unit_title import (
        filter_spurious_xulun_rows,
        sort_catalog_by_unit_and_lesson,
        unit_no_from_title,
    )
    from ...services.lesson_filters import catalog_filter_mode, filter_catalog_rows
    from ...services.lesson_delete import delete_all_lessons_for_volume
    from ...services.old_library.volume_codes import make_lesson_uid_for_volume
    from .subject_postprocess import normalize_lesson_for_storage

    if not catalog:
        raise ValueError("目录为空")

    subj = (volume.subject or "").strip()
    mode = catalog_filter_mode(subj, pipeline="textbook_diff")
    # Test 仍按语文规则；化学与科学等「所见即所得」保留目录原序
    is_yuwen = mode == "yuwen"
    is_as_seen = mode == "as_seen"
    catalog = filter_catalog_rows(
        catalog,
        keep_yuwen_subtitles=is_yuwen,
        subject=volume.subject,
        pipeline="textbook_diff",
    )
    catalog = filter_spurious_xulun_rows(catalog)
    from ...parsers.catalog_lesson_line import expand_glued_catalog_rows

    catalog = expand_glued_catalog_rows(catalog)
    # 语文须保留相邻顺序；数学所见即所得也保留目录原序，勿按课号重排打散
    if not is_yuwen and not is_as_seen:
        catalog = sort_catalog_by_unit_and_lesson(catalog)
    if not catalog:
        raise ValueError("目录过滤后为空")

    # 语文：副标题不单独建课，合并进上一主课 lesson_name
    if is_yuwen:
        merged_rows: list[dict] = []
        pending_subs: list[str] = []

        def _flush_subs() -> None:
            nonlocal pending_subs
            if not merged_rows or not pending_subs:
                pending_subs = []
                return
            parent = merged_rows[-1]
            base = str(parent.get("lesson") or "").strip()
            # 去掉可能已带的括号，避免重复合并
            base = re.sub(r"（[^）]*）\s*$", "", base).strip()
            joined = "；".join(pending_subs)
            parent["lesson"] = f"{base}（{joined}）"
            pending_subs = []

        for row in catalog:
            raw = str(row.get("lesson") or "")
            is_sub = bool(row.get("_subtitle")) or is_yuwen_subtitle_line(raw)
            if is_sub:
                title = strip_yuwen_subtitle_label(raw)
                if title and title not in pending_subs:
                    pending_subs.append(title)
                continue
            _flush_subs()
            merged_rows.append(dict(row))
            merged_rows[-1].pop("_subtitle", None)
        _flush_subs()
        catalog = merged_rows
        if not catalog:
            raise ValueError("目录过滤后为空")

    edition = _edition_for_volume(volume)
    existing = Lesson.query.filter_by(volume_id=volume.id).all()
    if existing and not replace:
        return len(existing)
    if existing:
        delete_all_lessons_for_volume(volume)

    unit_no_map: dict[str, int] = {}
    unknown_counter = 0
    lesson_counters: dict[int, int] = {}
    seen_uids: set[str] = set()
    seen_unit_lesson_no: set[tuple[int, str]] = set()
    created = 0

    for row in catalog:
        unit_title = (row.get("unit") or "").strip() or "未命名单元"
        if unit_title not in unit_no_map:
            parsed = unit_no_from_title(unit_title)
            if parsed is not None:
                unit_no_map[unit_title] = parsed
            else:
                unknown_counter += 1
                unit_no_map[unit_title] = 9000 + unknown_counter
        unit_no = unit_no_map[unit_title]

        lesson_counters.setdefault(unit_no, 0)
        lesson_counters[unit_no] += 1

        lesson_raw = str(row.get("lesson") or "")
        lesson_no, lesson_name, parsed_order = parse_catalog_lesson_fields(
            lesson_raw,
            fallback_no=lesson_counters[unit_no],
        )
        # 所见即所得：按目录出现顺序写 sort_order，避免「12.5」排到「读一读」前面
        sort_order = (
            lesson_counters[unit_no] * 10 if is_as_seen else parsed_order
        )

        lesson_no, lesson_name, uid_lesson_no = normalize_lesson_for_storage(
            volume.subject or "",
            unit_no=unit_no,
            lesson_no=lesson_no,
            lesson_name=lesson_name,
        )

        uk = (unit_no, lesson_no or uid_lesson_no)
        if uk in seen_unit_lesson_no:
            _log.warning(
                "跳过重复 unit/lesson_no=%s/%s（%s）",
                unit_no,
                lesson_no or uid_lesson_no,
                lesson_raw,
            )
            continue
        seen_unit_lesson_no.add(uk)

        copy_version = 1
        try:
            from ..textbook_library.codes import copy_version_from_volume_code

            copy_version = copy_version_from_volume_code(volume.volume_code or "")
        except Exception:
            copy_version = 1

        lesson_uid = make_lesson_uid_for_volume(
            edition,
            grade=volume.grade,
            term=volume.semester,
            book_type=volume.book_type,
            unit_no=unit_no,
            lesson_no=uid_lesson_no,
            copy_version=copy_version,
        )
        if lesson_uid in seen_uids:
            _log.warning(
                "跳过重复 lesson_uid=%s（unit=%s lesson=%s）",
                lesson_uid,
                unit_title,
                lesson_raw,
            )
            continue
        seen_uids.add(lesson_uid)

        # lesson_name 列长 256；过长时截断副标题
        if len(lesson_name) > 240:
            lesson_name = lesson_name[:237] + "…"

        les = Lesson(
            volume_id=volume.id,
            lesson_uid=lesson_uid,
            unit_no=unit_no,
            unit_title=unit_title,
            lesson_no=lesson_no,
            lesson_name=lesson_name,
            sort_order=sort_order,
            slides_fetch_status="not_uploaded",
        )
        db.session.add(les)
        created += 1

    db.session.flush()
    return created


def _pdf_path_for_volume(volume: Volume):
    from ...repo_paths import repo_root
    from pathlib import Path
    import tempfile

    from ..blobs import read_blob_bytes

    blob = db.session.get(FileBlob, volume.blob_id)
    if not blob:
        raise ValueError("PDF blob 不存在")
    if blob.storage_path:
        path = repo_root() / blob.storage_path
        if path.is_file():
            return path
    data = read_blob_bytes(blob)
    tmp = Path(tempfile.gettempdir()) / f"textbook-diff-{volume.volume_code}.pdf"
    tmp.write_bytes(data)
    return tmp


def _extract_with_llm_direct(
    pdf_path, edition, grade, semester, force_refresh, content_hash, subject=None
):
    """直接用 LLM 视觉识别目录，不经过 fallback 链路。"""
    from ...parsers.pdf_catalog_step1 import locate_catalog_page_range
    from ...parsers.pdf_pages import is_pdf_text_sparse
    from ...services.llm.catalog_extract import extract_catalog_with_llm_vision

    page_start, page_end = 0, None
    max_pages = 8
    if is_pdf_text_sparse(pdf_path):
        page_start, page_end = locate_catalog_page_range(pdf_path)
        max_pages = max(6, page_end - page_start + 1)

    try:
        rows, tag = extract_catalog_with_llm_vision(
            pdf_path,
            edition=edition,
            grade=grade,
            semester=semester,
            subject=subject,
            max_pages=max_pages,
            page_start=page_start,
            page_end=page_end,
            force_refresh=force_refresh,
            content_hash=content_hash,
        )
        if len(rows) >= _MIN_CATALOG_LINES:
            return rows, tag
        _log.warning("LLM 目录行不足（%d），需要兜底", len(rows))
    except Exception as exc:
        _log.warning("LLM 目录识别失败：%s", exc)
    return None, None


def _ocr_all_toc_pages(pdf_path, *, max_pages: int = 16):
    """OCR 扫描前 max_pages 页，提取目录行。"""
    from ...parsers.pdf_catalog import CatalogLineParser
    from ...parsers.benchmark_xlsx import sort_catalog_rows
    from ...parsers.pdf_pages import extract_page_texts_pdfplumber, ocr_page_texts

    all_lines: list[str] = []
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        n = min(max_pages, len(doc))
        # Try pdfplumber first for text pages
        for i in range(n):
            page = doc.load_page(i)
            text = page.get_text()
            if text and len(text.strip()) > 80:
                for line in text.split("\n"):
                    stripped = line.strip()
                    if stripped and (
                        "第" in stripped
                        or "单元" in stripped
                        or "课" in stripped
                        or "节" in stripped
                        or "目录" in stripped
                        or any(c.isdigit() for c in stripped[:3])
                    ):
                        all_lines.append(stripped)
        doc.close()

        # If pdfplumber text is sparse, use OCR
        if len(all_lines) < 6:
            all_lines = _ocr_pages(pdf_path, max_pages=max_pages)
    except Exception:
        all_lines = _ocr_pages(pdf_path, max_pages=max_pages)

    parser = CatalogLineParser()
    return sort_catalog_rows(parser.parse_lines(all_lines))


def _extract_fitz_text_lines(pdf_path, *, max_pages: int = 16) -> list[str]:
    """PyMuPDF 内嵌文字（不依赖 OCR）。"""
    import fitz

    doc = fitz.open(str(pdf_path))
    all_lines: list[str] = []
    try:
        for i in range(min(max_pages, len(doc))):
            text = doc.load_page(i).get_text() or ""
            for line in text.split("\n"):
                s = line.strip()
                if s:
                    all_lines.append(s)
    finally:
        doc.close()
    return all_lines


def _ocr_sparse_pages(pdf_path, *, max_pages: int = 16) -> list[str]:
    """内嵌文字不足时对稀疏页做 RapidOCR（可选依赖）。"""
    import fitz
    from collections import defaultdict
    from io import BytesIO

    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    doc = fitz.open(str(pdf_path))
    extra: list[str] = []
    try:
        for i in range(min(max_pages, len(doc))):
            page = doc.load_page(i)
            text = page.get_text() or ""
            if text and len(text.strip()) > 80:
                continue
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.open(BytesIO(pix.tobytes("png")))
            arr = np.asarray(img)
            if arr.ndim == 2:
                arr = np.stack([arr, arr, arr], axis=-1)
            result, _ = ocr(arr)
            if not result:
                continue
            buckets: dict[int, list[tuple[float, str]]] = defaultdict(list)
            for it in result:
                try:
                    box, tp = it[0], it[1]
                    txt = str(tp[0] if isinstance(tp, (list, tuple)) else tp).strip()
                    if not txt:
                        continue
                    cy = (min(p[1] for p in box) + max(p[1] for p in box)) / 2
                    cx = (min(p[0] for p in box) + max(p[0] for p in box)) / 2
                    buckets[int(round(cy / 48))].append((cx, txt))
                except Exception:
                    continue
            for k in sorted(buckets):
                parts = sorted(buckets[k], key=lambda x: x[0])
                merged = " ".join(p[1] for p in parts if p[1])
                if merged.strip():
                    extra.append(merged.strip())
    finally:
        doc.close()
    return extra


def _ocr_pages(pdf_path, *, max_pages: int = 16) -> list[str]:
    """优先 PDF 内嵌文字；不足时尝试 RapidOCR。"""
    lines = _extract_fitz_text_lines(pdf_path, max_pages=max_pages)
    if len(lines) >= 6:
        return lines
    try:
        lines.extend(_ocr_sparse_pages(pdf_path, max_pages=max_pages))
    except ImportError:
        _log.warning("RapidOCR 不可用，目录补充仅使用 PDF 内嵌文字（%d 行）", len(lines))
    return lines


def _raw_text_to_catalog(all_lines: list[str]) -> list[dict]:
    """从 PDF 原文/OCR 行提取目录（化学人教版专用顺序解析）。"""
    from ...parsers.catalog_lesson_line import parse_chemistry_toc_lines

    return parse_chemistry_toc_lines(all_lines)


def _enrich_catalog_from_pdf_text(
    catalog: list[dict],
    pdf_path,
    *,
    max_pages: int = 20,
) -> tuple[list[dict], int]:
    """用 PDF 目录页原文补充 LLM 常漏掉的绪论/整理/复习/实验等行。"""
    from ...parsers.catalog_lesson_line import merge_catalog_rows

    raw_lines = _collect_raw_text(pdf_path, max_pages=max_pages)
    supplemental = _raw_text_to_catalog(raw_lines)
    if not supplemental:
        return catalog, 0
    before = len(catalog)
    merged = merge_catalog_rows(catalog, supplemental) if catalog else supplemental
    return merged, len(merged) - before


def _collect_raw_text(pdf_path, max_pages=20):
    """把所有文字（PyMuPDF get_text + RapidOCR）收集到一个列表返回。"""
    from ...parsers.pdf_catalog_step1 import (
        extract_catalog_lines_step1_ocr,
        locate_catalog_page_range,
    )
    from ...parsers.pdf_pages import is_pdf_text_sparse

    if is_pdf_text_sparse(pdf_path):
        try:
            start, end = locate_catalog_page_range(pdf_path)
            return extract_catalog_lines_step1_ocr(
                pdf_path,
                start_page=start,
                end_page=end,
            )
        except ImportError:
            _log.warning("RapidOCR 不可用，扫描版目录仅尝试 LLM")
        except Exception as exc:
            _log.warning("扫描版目录 OCR 失败：%s", exc)

    return _ocr_pages(pdf_path, max_pages=max_pages)


def bootstrap_diff_catalog(
    *,
    volume_code: str,
    replace: bool = False,
    clear_llm_cache: bool | None = None,
) -> dict:
    volume = get_diff_volume_by_code(volume_code)
    if not volume.blob_id and not volume.preview_blob_id:
        raise ValueError("请先上传 PDF（完整版或修订版）")

    from ..volume_pdf import resolve_volume_pdf_path

    volume_id = volume.id
    pdf_path = resolve_volume_pdf_path(volume, source="auto")
    blob_id = volume.blob_id or volume.preview_blob_id
    blob = db.session.get(FileBlob, blob_id)
    if blob is None:
        raise ValueError("PDF blob 不存在")
    content_hash = blob.content_hash

    # 默认：replace 时清缓存以便重识别；可显式 clear_llm_cache=False 仅重建课表
    do_clear = replace if clear_llm_cache is None else bool(clear_llm_cache)
    if do_clear:
        clear_llm_toc_cache(pdf_path, content_hash=content_hash)

    db.session.commit()
    volume = db.session.get(Volume, volume_id)
    if volume is None:
        raise ValueError(f"册次不存在：{volume_code}")

    from ...parsers.pdf_spread import ensure_volume_page_layout
    from .intake_catalog_pipeline import run_diff_catalog_extract

    layout = ensure_volume_page_layout(
        volume, pdf_path=pdf_path, force=False, commit=True
    )
    volume = db.session.get(Volume, volume_id)
    if volume is None:
        raise ValueError(f"册次不存在：{volume_code}")

    extract = run_diff_catalog_extract(
        pdf_path,
        volume=volume,
        content_hash=content_hash,
        force_refresh=do_clear,
        layout=layout,
    )
    catalog = extract.rows
    source = extract.source
    if extract.degraded:
        _log.warning("目录识别降级：%s %s", volume_code, extract.warnings)

    # 手动删旧课时 + flush，确保清理不可逆
    from ...models import Lesson as _Les
    from ...extensions import db as _db

    _Les.query.filter_by(volume_id=volume.id).delete()
    _db.session.flush()

    created = _bootstrap_lessons_preserve_order(
        volume=volume, catalog=catalog, replace=False  # 已手动删除故不替换
    )
    # 目录重建后旧页码作废，须重新「划分页码」；保留 page_layout 判定
    volume.parse_status = "pending"
    volume.parse_error = None
    prev_sug = dict(volume.parse_suggestions_json or {})
    kept_sug = {
        k: prev_sug[k]
        for k in ("page_layout", "page_layout_meta")
        if k in prev_sug
    }
    if extract.degraded:
        kept_sug["catalog_degraded"] = True
        kept_sug["catalog_warnings"] = list(extract.warnings or [])
    else:
        kept_sug.pop("catalog_degraded", None)
        kept_sug.pop("catalog_warnings", None)
    volume.parse_suggestions_json = kept_sug or None

    try:
        from .toc_cache import ensure_diff_toc_page_cache
        from ...parsers.pdf_spread import ensure_volume_page_layout

        lessons_after = _Les.query.filter_by(volume_id=volume.id).all()
        layout_now = ensure_volume_page_layout(
            volume, pdf_path=pdf_path, force=False, commit=False
        )
        ensure_diff_toc_page_cache(
            pdf_path,
            lessons=lessons_after,
            content_hash=content_hash,
            force=True,
            layout=layout_now,
        )
    except Exception as exc_toc:
        _log.warning("目录页码缓存写入失败：%s", exc_toc)

    db.session.commit()

    result = diff_volume_detail(volume)
    message = f"已从 PDF 目录导入 {created} 节课（{source}）"
    extra: dict = {
        "catalog_source": source,
        "catalog_lines": len(catalog),
        "lessons_created": created,
        "message": message,
    }
    if extract.degraded:
        extra["catalog_degraded"] = True
        extra["catalog_warnings"] = list(extract.warnings or [])
        extra["message"] = f"{message}，已降级，需复核"
    result.update(extra)
    return result
