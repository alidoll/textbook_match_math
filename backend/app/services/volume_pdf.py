"""册次 PDF 完整版 / 修订版双轨上传与路径解析。"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any, Literal

from ..extensions import db
from ..models import FileBlob, Volume
from ..repo_paths import repo_root, textbook_mirror_path
from .blobs import _rel_storage_path, read_blob_bytes, store_blob
from .pdf_mirror_finalize import abort_pdf_mirror, finalize_pdf_mirror
from .volume_draft_pdfs import (
    draft_pdf_fields,
    draft_mirror_key,
    replace_volume_draft_pdf,
    resolve_draft_blob_id,
)

_PDF_MIME = "application/pdf"
PdfRole = Literal["full", "draft"]


def normalize_pdf_role(raw: str | None) -> PdfRole:
    r = (raw or "full").strip().lower()
    if r in ("draft", "preview", "revision", "incomplete", "修订", "不完整"):
        return "draft"
    return "full"


def _normalize_mime(mime_type: str | None, filename: str | None) -> str:
    if mime_type and "pdf" in mime_type.lower():
        return _PDF_MIME
    if filename and filename.lower().endswith(".pdf"):
        return _PDF_MIME
    return mime_type or _PDF_MIME


def stage_volume_pdf_mirror(
    *,
    volume: Volume,
    content: bytes,
    pdf_role: PdfRole,
    draft_key: str | None = None,
) -> tuple[Path, Path]:
    final = textbook_mirror_path(
        book_type=volume.book_type or "new",
        edition_label=volume.edition,
        volume_code=volume.volume_code,
        draft=(pdf_role == "draft"),
        draft_key=draft_key if pdf_role == "draft" else None,
    )
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f"{final.name}.uploading")
    tmp.write_bytes(content)
    return tmp, final


def _blob_row(blob_id: str | None) -> FileBlob | None:
    if not blob_id:
        return None
    return db.session.get(FileBlob, blob_id)


def volume_pdf_blob_fields(volume: Volume) -> dict[str, Any]:
    """详情接口：完整版 + 修订版元数据。"""
    full_row = _blob_row(volume.blob_id)
    draft_row = _blob_row(volume.preview_blob_id)
    full_mirror = (
        textbook_mirror_path(
            book_type=volume.book_type or "new",
            edition_label=volume.edition,
            volume_code=volume.volume_code,
            draft=False,
        )
        if volume.blob_id
        else None
    )
    draft_mirror = None
    if draft_row and draft_row.storage_path:
        path = repo_root() / draft_row.storage_path
        if path.is_file():
            draft_mirror = path
    elif volume.preview_blob_id:
        legacy = textbook_mirror_path(
            book_type=volume.book_type or "new",
            edition_label=volume.edition,
            volume_code=volume.volume_code,
            draft=True,
        )
        if legacy.is_file():
            draft_mirror = legacy
    return {
        "has_pdf": bool(volume.blob_id),
        "has_preview_pdf": bool(volume.preview_blob_id) or bool(draft_pdf_fields(volume)["draft_pdf_count"]),
        **draft_pdf_fields(volume),
        "blob_id": volume.blob_id,
        "preview_blob_id": volume.preview_blob_id,
        "blob_size_bytes": full_row.size_bytes if full_row else None,
        "preview_blob_size_bytes": draft_row.size_bytes if draft_row else None,
        "blob_storage": (
            "disk" if full_row and full_row.storage_path else ("mysql" if full_row else None)
        ),
        "preview_blob_storage": (
            "disk"
            if draft_row and draft_row.storage_path
            else ("mysql" if draft_row else None)
        ),
        "mirror_path": str(full_mirror) if full_mirror and full_mirror.is_file() else None,
        "preview_mirror_path": str(draft_mirror) if draft_mirror else None,
    }


def _size_matches_file(blob: FileBlob, path: Path, *, tolerance: float = 0.02) -> bool:
    if not path.is_file():
        return False
    expected = int(blob.size_bytes or 0)
    if expected <= 0:
        return True
    actual = path.stat().st_size
    return abs(actual - expected) <= max(int(expected * tolerance), 4096)


def _hash_matches_file(blob: FileBlob, path: Path) -> bool:
    if not path.is_file() or not blob.content_hash:
        return False
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest() == blob.content_hash


def _draft_pdf_candidates(volume: Volume, blob: FileBlob) -> list[Path]:
    """修订版 PDF 可能落在 hash 命名或 legacy .draft.pdf，且 blob.storage_path 可能登记错误。"""
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            candidates.append(path)

    if blob.storage_path:
        add(repo_root() / blob.storage_path)
    if blob.content_hash:
        add(
            textbook_mirror_path(
                book_type=volume.book_type or "new",
                edition_label=volume.edition,
                volume_code=volume.volume_code,
                draft=True,
                draft_key=blob.content_hash[:8],
            )
        )
    add(
        textbook_mirror_path(
            book_type=volume.book_type or "new",
            edition_label=volume.edition,
            volume_code=volume.volume_code,
            draft=True,
            draft_key=None,
        )
    )
    draft_dir = textbook_mirror_path(
        book_type=volume.book_type or "new",
        edition_label=volume.edition,
        volume_code=volume.volume_code,
        draft=True,
        draft_key=None,
    ).parent
    add(draft_dir / f"{volume.volume_code}.draft.pdf")
    for path in sorted(draft_dir.glob(f"{volume.volume_code}.draft.*.pdf")):
        add(path)
    # 修订版字节有时仅落在完整版镜像（旧上传或镜像被覆盖后 draft 路径缺失）
    add(
        textbook_mirror_path(
            book_type=volume.book_type or "new",
            edition_label=volume.edition,
            volume_code=volume.volume_code,
            draft=False,
        )
    )
    return candidates


def _resolve_draft_pdf_path(volume: Volume, blob: FileBlob) -> Path:
    candidates = _draft_pdf_candidates(volume, blob)
    matched = [p for p in candidates if _hash_matches_file(blob, p)]
    if matched:
        return matched[0]
    matched = [p for p in candidates if _size_matches_file(blob, p)]
    if matched:
        return matched[0]
    for path in candidates:
        if path.is_file():
            return path
    if blob.storage_path:
        expected = repo_root() / blob.storage_path
        raise ValueError(
            f"修订版 PDF 文件缺失（{expected.name}），请到册次 intake 重新上传修订版"
        )
    data = read_blob_bytes(blob)
    tmp = Path(tempfile.gettempdir()) / f"volume-{volume.volume_code}-draft.pdf"
    tmp.write_bytes(data)
    return tmp


def resolve_volume_pdf_path(
    volume: Volume,
    *,
    source: str | None = None,
    preview_blob_id: str | None = None,
) -> Path:
    """source=full|draft|auto；draft 可指定 preview_blob_id 选择某份修订版。"""
    role = normalize_pdf_role(source) if source and source != "auto" else None
    if preview_blob_id or role == "draft":
        blob_id = resolve_draft_blob_id(volume, preview_blob_id)
    elif role == "full":
        blob_id = volume.blob_id
        if not blob_id:
            raise ValueError("本册尚无完整版 PDF，请先上传完整版")
    else:
        blob_id = volume.blob_id or resolve_draft_blob_id(volume, None)
        if not blob_id:
            raise ValueError("请先上传 PDF（完整版或修订版）")
    blob = db.session.get(FileBlob, blob_id)
    if not blob:
        raise ValueError("PDF blob 不存在")
    if blob_id != volume.blob_id:
        return _resolve_draft_pdf_path(volume, blob)
    if blob.storage_path:
        path = repo_root() / blob.storage_path
        if path.is_file():
            return path
    data = read_blob_bytes(blob)
    tmp = Path(tempfile.gettempdir()) / f"volume-{volume.volume_code}-full.pdf"
    tmp.write_bytes(data)
    return tmp


def _invalidate_preprocess_after_full_pdf_replace(volume: Volume) -> dict[str, int]:
    """完整版 PDF 被替换后：清空旧目录课时与页图，必须重新识别目录。"""
    from sqlalchemy import or_

    from ..models import DiffLessonPair, Lesson
    from .lesson_delete import delete_all_lessons_for_volume

    lessons = Lesson.query.filter_by(volume_id=volume.id).all()
    lesson_ids = [les.id for les in lessons]
    pairs_deleted = 0
    if lesson_ids:
        pairs = DiffLessonPair.query.filter(
            or_(
                DiffLessonPair.new_lesson_id.in_(lesson_ids),
                DiffLessonPair.old_lesson_id.in_(lesson_ids),
                DiffLessonPair.old_volume_id == volume.id,
                DiffLessonPair.new_volume_id == volume.id,
            )
        ).all()
        pairs_deleted = len(pairs)
        for pair in pairs:
            db.session.delete(pair)
        if pairs:
            db.session.flush()

    lessons_deleted = delete_all_lessons_for_volume(volume)
    return {
        "lessons_deleted": lessons_deleted,
        "diff_pairs_deleted": pairs_deleted,
        "catalog_cleared": True,
    }


def upload_volume_pdf_with_role(
    volume: Volume,
    *,
    content: bytes,
    filename: str | None = None,
    mime_type: str | None = None,
    pdf_role: str | None = "full",
) -> dict[str, Any]:
    if not content:
        raise ValueError("上传文件为空")
    mime = _normalize_mime(mime_type, filename)
    if mime != _PDF_MIME:
        raise ValueError("仅支持 PDF 文件")

    role = normalize_pdf_role(pdf_role)
    replaced = bool(volume.blob_id if role == "full" else False)
    draft_appended = False
    draft_duplicate = False
    preprocess_reset: dict[str, int] | None = None
    tmp_path: Path | None = None
    final_path: Path | None = None

    try:
        draft_key = draft_mirror_key(content) if role == "draft" else None
        tmp_path, final_path = stage_volume_pdf_mirror(
            volume=volume,
            content=content,
            pdf_role=role,
            draft_key=draft_key,
        )
        rel_final = _rel_storage_path(final_path)
        blob, blob_created = store_blob(
            content=content,
            mime_type=mime,
            disk_path=tmp_path,
            storage_path=final_path,
        )
        # 无论 blob 新建还是按 hash 复用，都必须把本次上传内容落到册次镜像路径
        finalize_pdf_mirror(tmp_path, final_path)
        tmp_path = None
        if blob.storage_path != rel_final:
            blob.storage_path = rel_final
        blob.size_bytes = len(content)

        if role == "draft":
            draft_row, created = replace_volume_draft_pdf(
                volume,
                blob=blob,
                upload_filename=filename,
            )
            draft_appended = created
            draft_duplicate = not created
            volume.preview_blob_id = blob.id
        else:
            draft_row = None
            volume.blob_id = blob.id
            volume.parse_status = "pending"
            volume.parse_error = None
            volume.parse_suggestions_json = None
            if replaced:
                preprocess_reset = _invalidate_preprocess_after_full_pdf_replace(volume)

        db.session.commit()

        fields = volume_pdf_blob_fields(volume)
        return {
            **fields,
            "pdf_role": role,
            "blob_created": blob_created,
            "blob_reused": not blob_created,
            "pdf_replaced": replaced,
            "draft_appended": draft_appended,
            "draft_duplicate": draft_duplicate,
            "draft_id": draft_row.id if draft_row else None,
            "mirror_path": str(final_path),
            "upload_filename": filename,
            "preprocess_reset": preprocess_reset,
        }
    except Exception:
        db.session.rollback()
        if tmp_path is not None:
            abort_pdf_mirror(tmp_path)
        raise
