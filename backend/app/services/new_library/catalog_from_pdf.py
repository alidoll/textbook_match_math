"""从已上传 PDF 识别目录：写入基准库 xlsx + lessons 表。"""
from __future__ import annotations

from pathlib import Path

from ...extensions import db
from ...models import FileBlob, Volume
from ...parsers.benchmark_xlsx import (
    append_catalog_to_benchmark_xlsx,
)
from ...parsers.llm_toc_cache import clear_llm_toc_cache
from ...repo_paths import benchmark_new_xlsx_path
from ..lesson_filters import filter_catalog_rows
from ..old_library.edition_registry import EDITIONS, get_edition
from ..old_library.pdf.parse import _pdf_path_for_volume
from .catalog_bootstrap import bootstrap_lessons_from_catalog
from .catalog_extract_pipeline import extract_new_library_catalog
from .volumes import get_new_volume_by_code, volume_detail_dict

_MIN_CATALOG_LINES = 3


def _edition_for_volume(volume):
    """与 catalog_bootstrap 一致：同标签优先匹配学科，避免跨学科误配。"""
    label = (volume.edition or "").strip()
    subject = (volume.subject or "").strip()
    matches = [ed for ed in EDITIONS.values() if ed.label == label]
    if matches:
        if subject:
            by_subject = [ed for ed in matches if ed.subject == subject]
            if by_subject:
                return by_subject[0]
        return matches[0]
    return get_edition("xiangke")


def _pending_benchmark_path(xlsx_path: Path) -> Path:
    return xlsx_path.with_name(f"{xlsx_path.stem}.pdf-pending.xlsx")


def _write_catalog_to_benchmark(
    *,
    xlsx_path: Path,
    sheet_name: str,
    edition_label: str,
    volume,
    catalog: list,
) -> tuple[int, Path | None, tuple[int, int] | None, str | None]:
    """写入基准 xlsx；被占用时落到 .pdf-pending.xlsx。"""
    try:
        rows, saved, row_span = append_catalog_to_benchmark_xlsx(
            xlsx_path,
            sheet_name,
            version_label=edition_label,
            grade=volume.grade,
            semester=volume.semester or "上",
            catalog=catalog,
            replace_existing=True,
        )
        return rows, saved, row_span, None
    except PermissionError:
        pending = _pending_benchmark_path(xlsx_path)
        try:
            rows, saved, row_span = append_catalog_to_benchmark_xlsx(
                pending,
                sheet_name,
                version_label=edition_label,
                grade=volume.grade,
                semester=volume.semester or "上",
                catalog=catalog,
                replace_existing=True,
            )
            warn = (
                f"基准库 {xlsx_path.name} 正被占用（请关闭 Excel/WPS），"
                f"目录已暂存至 {pending.name}。关闭占用后重新「解析目录」可写回主文件。"
            )
            return rows, saved, row_span, warn
        except PermissionError:
            return 0, None, None, (
                f"基准库无法写入（请关闭 {xlsx_path.name}）。"
                "课时表已更新，请关闭占用程序后重新解析目录。"
            )


def bootstrap_catalog_from_volume_pdf(
    *,
    volume_code: str,
    replace: bool = False,
) -> dict:
    volume = get_new_volume_by_code(volume_code)
    if not volume.blob_id:
        raise ValueError("请先上传 PDF")

    volume_id = volume.id
    edition_def = _edition_for_volume(volume)
    blob = db.session.get(FileBlob, volume.blob_id)
    if blob is None:
        raise ValueError("PDF blob 不存在")
    pdf_path: Path = _pdf_path_for_volume(volume)
    content_hash = blob.content_hash
    catalog_meta = {
        "edition": volume.edition,
        "grade": volume.grade,
        "semester": volume.semester,
    }
    if replace:
        clear_llm_toc_cache(pdf_path, content_hash=content_hash)

    # LLM 目录识别可能耗时 1–2 分钟，先结束只读事务，避免长时间占连接。
    db.session.commit()
    volume = db.session.get(Volume, volume_id)
    if volume is None:
        raise ValueError(f"册次不存在：{volume_code}")

    catalog, source = extract_new_library_catalog(
        pdf_path,
        edition=catalog_meta["edition"],
        grade=catalog_meta["grade"],
        semester=catalog_meta["semester"],
        subject=volume.subject,
        force_refresh=replace,
        content_hash=content_hash,
    )
    raw_n = len(catalog)
    catalog = filter_catalog_rows(catalog, subject=volume.subject, pipeline="new_library")
    if len(catalog) < _MIN_CATALOG_LINES:
        raise ValueError(
            f"PDF 目录识别不足（过滤后 {len(catalog)} 行 / 识别到 {raw_n} 行，"
            f"至少需 {_MIN_CATALOG_LINES} 行）。"
            "请检查 PDF 是否为扫描版目录页，或人工维护基准库。"
        )

    created = bootstrap_lessons_from_catalog(
        volume=volume,
        catalog=catalog,
        replace=replace,
    )
    volume.parse_status = volume.parse_status or "pending"

    xlsx_path = benchmark_new_xlsx_path()
    sheet_name = edition_def.sheet_for_book_type(volume.book_type or "new")
    xlsx_rows, xlsx_saved, row_span, xlsx_warning = _write_catalog_to_benchmark(
        xlsx_path=xlsx_path,
        sheet_name=sheet_name,
        edition_label=edition_def.label,
        volume=volume,
        catalog=catalog,
    )

    db.session.commit()

    result = volume_detail_dict(volume)
    if xlsx_saved is not None:
        loc = f"工作表「{sheet_name}」"
        if row_span:
            loc += f" 第 {row_span[0]}–{row_span[1]} 行"
        msg = (
            f"已从 PDF 目录导入 {created} 节课；基准库 {xlsx_rows} 行写入 {xlsx_saved.name}（{loc}）。"
        )
    else:
        msg = f"已从 PDF 目录导入 {created} 节课（基准库未写入）"
    if xlsx_warning:
        msg = f"{msg}。{xlsx_warning}"

    result.update(
        {
            "catalog_source": source,
            "catalog_lines": len(catalog),
            "lessons_created": created,
            "benchmark_xlsx_rows": xlsx_rows,
            "benchmark_xlsx_path": str(xlsx_saved) if xlsx_saved else None,
            "benchmark_xlsx_sheet": sheet_name if xlsx_saved else None,
            "benchmark_xlsx_row_span": list(row_span) if row_span else None,
            "benchmark_xlsx_warning": xlsx_warning,
            "message": msg,
        }
    )
    return result
