"""旧库：一键导入本版教材 PDF（模糊文件名匹配 + 磁盘扫描 / 上传）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ....models import Lesson, Volume
from ....repo_paths import old_textbook_dir, old_textbook_mirror_path
from ..edition_registry import get_edition, grade_term_pairs_for_edition
from ..volume_codes import make_display_title, make_volume_code, normalize_term
from .textbook_match import assign_textbook_pdfs
from .upload import upload_volume_pdf


def _edition_volumes(edition) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for grade, term in grade_term_pairs_for_edition(edition):
        term_key = normalize_term(term)
        code = make_volume_code(edition, grade=grade, term=term_key, book_type="old")
        rows.append(
            {
                "volume_code": code,
                "grade": grade,
                "term": term_key,
                "edition_label": edition.label,
                "edition": edition.label,
                "code_prefix": edition.code_prefix,
                "display_title": make_display_title(edition, grade=grade, term=term_key),
            }
        )
    return rows


def _collect_disk_pdfs(edition_label: str) -> list[Path]:
    """收集本版镜像目录下全部 PDF（不限标准文件名）。"""
    edition_dir = old_textbook_dir() / (edition_label or "").strip()
    if not edition_dir.is_dir():
        return []
    return sorted(p for p in edition_dir.glob("*.pdf") if p.is_file())


def _volume_ready(volume_code: str) -> tuple[Volume | None, int]:
    volume = Volume.query.filter_by(volume_code=volume_code, book_type="old").first()
    if not volume:
        return None, 0
    n = Lesson.query.filter_by(volume_id=volume.id).count()
    return volume, n


def preview_edition_textbook_import(
    *,
    edition_id: str,
    filenames: list[str] | None = None,
    min_score: float = 0.72,
) -> dict[str, Any]:
    """预览匹配：传 filenames 则按上传名匹配；否则扫描磁盘目录。"""
    edition = get_edition(edition_id)
    volumes = _edition_volumes(edition)
    if filenames:
        names = [str(n).strip() for n in filenames if str(n).strip()]
        sources = {n: n for n in names}
    else:
        paths = _collect_disk_pdfs(edition.label)
        names = [p.name for p in paths]
        sources = {p.name: str(p) for p in paths}

    plan = assign_textbook_pdfs(names, volumes, min_score=min_score)
    for row in plan["matches"]:
        row["source"] = sources.get(row["filename"])
        vol, lesson_count = _volume_ready(row["volume_code"])
        row["has_catalog"] = bool(vol and lesson_count > 0)
        row["has_pdf"] = bool(vol and vol.blob_id)
        row["lesson_count"] = lesson_count
    return {
        "edition_id": edition.edition_id,
        "edition_label": edition.label,
        "folder": str(old_textbook_dir() / edition.label),
        "file_count": len(names),
        **plan,
    }


def import_edition_textbooks_from_disk(
    *,
    edition_id: str,
    replace: bool = False,
    only_missing: bool = True,
    min_score: float = 0.72,
) -> dict:
    """扫描本版目录，按模糊文件名一对一绑定 PDF。"""
    edition = get_edition(edition_id)
    paths = _collect_disk_pdfs(edition.label)
    files = [(p.name, p.read_bytes(), str(p)) for p in paths]
    return _import_files(
        edition=edition,
        files=files,
        replace=replace,
        only_missing=only_missing,
        min_score=min_score,
        folder=str(old_textbook_dir() / edition.label),
    )


def import_edition_textbooks_from_uploads(
    *,
    edition_id: str,
    files: list[tuple[str, bytes]],
    replace: bool = False,
    only_missing: bool = True,
    min_score: float = 0.72,
    mapping: list[dict[str, str]] | None = None,
) -> dict:
    """浏览器多选 PDF 上传后自动匹配并导入（同课件批量）。"""
    edition = get_edition(edition_id)
    packed = [(fn, content, fn) for fn, content in files]
    return _import_files(
        edition=edition,
        files=packed,
        replace=replace,
        only_missing=only_missing,
        min_score=min_score,
        folder=None,
        mapping=mapping,
    )


def _import_files(
    *,
    edition,
    files: list[tuple[str, bytes, str]],
    replace: bool,
    only_missing: bool,
    min_score: float,
    folder: str | None,
    mapping: list[dict[str, str]] | None = None,
) -> dict:
    volumes = _edition_volumes(edition)
    names = [fn for fn, _, _ in files]
    content_by_name = {fn: (content, source) for fn, content, source in files}

    filename_to_code: dict[str, str] = {}
    if mapping:
        for row in mapping:
            fn = str(row.get("filename") or "").strip()
            code = str(row.get("volume_code") or "").strip()
            if fn and code:
                filename_to_code[fn] = code
        plan_matches = [
            {
                "filename": fn,
                "volume_code": code,
                "score": 1.0,
                "reason": "手动指定",
            }
            for fn, code in filename_to_code.items()
        ]
        unmatched: list[dict] = []
    else:
        plan = assign_textbook_pdfs(names, volumes, min_score=min_score)
        plan_matches = plan["matches"]
        unmatched = plan["unmatched"]
        for row in plan_matches:
            filename_to_code[row["filename"]] = row["volume_code"]

    imported: list[dict] = []
    skipped: list[dict] = []
    no_catalog: list[dict] = []
    errors: list[dict] = []

    used_names = set()
    for filename, code in filename_to_code.items():
        used_names.add(filename)
        if filename not in content_by_name:
            errors.append({"filename": filename, "volume_code": code, "error": "文件内容缺失"})
            continue
        content, source = content_by_name[filename]
        volume, lesson_count = _volume_ready(code)
        if not volume or lesson_count <= 0:
            no_catalog.append(
                {
                    "filename": filename,
                    "volume_code": code,
                    "reason": "尚未载入基准目录",
                    "source": source,
                }
            )
            continue
        if volume.blob_id and only_missing and not replace:
            skipped.append(
                {
                    "filename": filename,
                    "volume_code": code,
                    "reason": "already_has_pdf",
                    "source": source,
                }
            )
            continue
        try:
            result = upload_volume_pdf(
                volume_code=code,
                content=content,
                filename=filename,
                mime_type="application/pdf",
            )
            # 若来源不是标准镜像名，额外保留用户原名信息
            imported.append(
                {
                    "filename": filename,
                    "volume_code": code,
                    "source": source,
                    "expected_mirror": str(old_textbook_mirror_path(edition.label, code)),
                    "pdf_replaced": bool(result.get("pdf_replaced")),
                    "blob_size_bytes": result.get("blob_size_bytes"),
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "filename": filename,
                    "volume_code": code,
                    "source": source,
                    "error": str(exc),
                }
            )

    for filename, _, source in files:
        if filename in used_names:
            continue
        skipped.append(
            {
                "filename": filename,
                "reason": "未匹配到本版册次",
                "source": source,
            }
        )

    return {
        "edition_id": edition.edition_id,
        "edition_label": edition.label,
        "folder": folder,
        "folder_exists": bool(folder and Path(folder).is_dir()),
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "no_catalog_count": len(no_catalog),
        "error_count": len(errors),
        "unmatched_count": len(unmatched),
        "imported": imported,
        "skipped": skipped,
        "no_catalog": no_catalog,
        "errors": errors,
        "unmatched": unmatched,
        "matches": plan_matches,
    }
