"""新库：一键导入本版教材 PDF（模糊文件名匹配）。"""
from __future__ import annotations

from typing import Any

from ....models import Lesson, Volume
from ...old_library.edition_registry import get_edition, grade_term_pairs_for_edition
from ...old_library.pdf.textbook_match import assign_textbook_pdfs
from ...old_library.volume_codes import make_display_title, make_volume_code, normalize_term
from ..volume_create import create_new_volume
from .upload import upload_volume_pdf


def _edition_volumes(edition) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for grade, term in grade_term_pairs_for_edition(edition):
        term_key = normalize_term(term)
        code = make_volume_code(edition, grade=grade, term=term_key, book_type="new")
        rows.append(
            {
                "volume_code": code,
                "grade": grade,
                "term": term_key,
                "edition_label": edition.label,
                "edition": edition.label,
                "code_prefix": edition.code_prefix,
                "display_title": make_display_title(
                    edition, grade=grade, term=term_key
                ),
            }
        )
    return rows


def _volume_ready(volume_code: str) -> tuple[Volume | None, int, bool]:
    volume = Volume.query.filter_by(volume_code=volume_code, book_type="new").first()
    if not volume:
        return None, 0, False
    n = Lesson.query.filter_by(volume_id=volume.id).count()
    return volume, n, bool(volume.blob_id)


def preview_edition_textbook_import(
    *,
    edition_id: str,
    filenames: list[str] | None = None,
    min_score: float = 0.72,
) -> dict[str, Any]:
    edition = get_edition(edition_id)
    volumes = _edition_volumes(edition)
    names = [str(n).strip() for n in (filenames or []) if str(n).strip()]
    plan = assign_textbook_pdfs(names, volumes, min_score=min_score)
    for row in plan["matches"]:
        vol, lesson_count, has_pdf = _volume_ready(row["volume_code"])
        row["has_catalog"] = bool(vol and lesson_count > 0)
        row["has_pdf"] = has_pdf
        row["lesson_count"] = lesson_count
        row["in_db"] = vol is not None
    return {
        "edition_id": edition.edition_id,
        "edition_label": edition.label,
        "file_count": len(names),
        **plan,
    }


def import_edition_textbooks_from_uploads(
    *,
    edition_id: str,
    files: list[tuple[str, bytes]],
    replace: bool = False,
    only_missing: bool = True,
    min_score: float = 0.72,
    mapping: list[dict[str, str]] | None = None,
) -> dict:
    edition = get_edition(edition_id)
    volumes = _edition_volumes(edition)
    names = [fn for fn, _ in files]
    content_by_name = {fn: content for fn, content in files}

    filename_to_code: dict[str, str] = {}
    if mapping:
        for row in mapping:
            fn = str(row.get("filename") or "").strip()
            code = str(row.get("volume_code") or "").strip()
            if fn and code:
                filename_to_code[fn] = code
        plan_matches = [
            {"filename": fn, "volume_code": code, "score": 1.0, "reason": "手动指定"}
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
    errors: list[dict] = []

    for filename, code in filename_to_code.items():
        content = content_by_name.get(filename)
        if content is None:
            errors.append({"filename": filename, "volume_code": code, "error": "文件内容缺失"})
            continue
        # 按 volume_code 反推年级学期并 ensure
        vol_meta = next((v for v in volumes if v["volume_code"] == code), None)
        if not vol_meta:
            errors.append({"filename": filename, "volume_code": code, "error": "册次不在本版"})
            continue
        try:
            create_new_volume(
                edition_id=edition.edition_id,
                grade=int(vol_meta["grade"]),
                term=str(vol_meta["term"]),
            )
        except Exception as exc:
            errors.append(
                {"filename": filename, "volume_code": code, "error": f"建册失败：{exc}"}
            )
            continue

        volume, _n, has_pdf = _volume_ready(code)
        if has_pdf and only_missing and not replace:
            skipped.append(
                {
                    "filename": filename,
                    "volume_code": code,
                    "reason": "already_has_pdf",
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
            imported.append(
                {
                    "filename": filename,
                    "volume_code": code,
                    "blob_id": result.get("blob_id"),
                    "pdf_replaced": result.get("pdf_replaced"),
                }
            )
        except Exception as exc:
            errors.append({"filename": filename, "volume_code": code, "error": str(exc)})

    return {
        "edition_id": edition.edition_id,
        "edition_label": edition.label,
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "unmatched_count": len(unmatched),
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "unmatched": unmatched,
        "matches": plan_matches,
    }
