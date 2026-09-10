"""册次多份不完整修订版 PDF。"""
from __future__ import annotations

from typing import Any

from ..extensions import db
from ..models import FileBlob, Volume, VolumeDraftPdf
from ..repo_paths import repo_root
from .blobs import sha256_hex


def list_volume_draft_pdfs(volume: Volume) -> list[VolumeDraftPdf]:
    return (
        VolumeDraftPdf.query.filter_by(volume_id=volume.id)
        .order_by(VolumeDraftPdf.created_at.desc())
        .all()
    )


def draft_pdf_row(row: VolumeDraftPdf) -> dict[str, Any]:
    blob = row.blob or db.session.get(FileBlob, row.blob_id)
    mirror = None
    if blob and blob.storage_path:
        path = repo_root() / blob.storage_path
        if path.is_file():
            mirror = str(path)
    label = (row.label or row.upload_filename or f"修订版 {row.blob_id[:8]}").strip()
    return {
        "draft_id": row.id,
        "blob_id": row.blob_id,
        "label": label,
        "upload_filename": row.upload_filename,
        "size_bytes": blob.size_bytes if blob else None,
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds") if row.created_at else None,
        "mirror_path": mirror,
        "is_latest": False,
    }


def _legacy_draft_item(volume: Volume) -> dict[str, Any] | None:
    """迁移前仅 preview_blob_id、尚无 volume_draft_pdfs 行时的展示回退。"""
    if not volume.preview_blob_id:
        return None
    blob = db.session.get(FileBlob, volume.preview_blob_id)
    if not blob:
        return None
    mirror = None
    if blob.storage_path:
        path = repo_root() / blob.storage_path
        if path.is_file():
            mirror = str(path)
    return {
        "draft_id": None,
        "blob_id": volume.preview_blob_id,
        "label": "默认修订版",
        "upload_filename": None,
        "size_bytes": blob.size_bytes,
        "created_at": None,
        "mirror_path": mirror,
        "is_latest": True,
        "legacy": True,
    }


def draft_pdf_fields(volume: Volume) -> dict[str, Any]:
    rows = list_volume_draft_pdfs(volume)
    items = [draft_pdf_row(r) for r in rows]
    if not items:
        legacy = _legacy_draft_item(volume)
        if legacy:
            items = [legacy]
    if items and volume.preview_blob_id:
        for item in items:
            if item["blob_id"] == volume.preview_blob_id:
                item["is_latest"] = True
                break
        else:
            items[0]["is_latest"] = True
    elif items:
        items[0]["is_latest"] = True
    return {
        "draft_pdf_count": len(items),
        "draft_pdfs": items,
    }


def ensure_draft_pdf_registered(
    volume: Volume,
    *,
    blob: FileBlob,
    upload_filename: str | None = None,
) -> tuple[VolumeDraftPdf, bool]:
    """登记一份修订版；同 blob 不重复插入。返回 (row, created)。"""
    existing = VolumeDraftPdf.query.filter_by(volume_id=volume.id, blob_id=blob.id).first()
    if existing:
        return existing, False

    label = (upload_filename or "").strip() or f"修订版 {blob.content_hash[:8]}"
    row = VolumeDraftPdf(
        volume_id=volume.id,
        blob_id=blob.id,
        upload_filename=upload_filename,
        label=label,
    )
    db.session.add(row)
    db.session.flush()
    return row, True


def replace_volume_draft_pdf(
    volume: Volume,
    *,
    blob: FileBlob,
    upload_filename: str | None = None,
) -> tuple[VolumeDraftPdf, bool]:
    """只保留一份修订版：清空旧登记后写入本次上传。"""
    VolumeDraftPdf.query.filter_by(volume_id=volume.id).delete(synchronize_session=False)
    db.session.flush()
    row, created = ensure_draft_pdf_registered(
        volume, blob=blob, upload_filename=upload_filename
    )
    volume.preview_blob_id = blob.id
    # 换修订版后旧印刷页码/页图失效
    sug = dict(volume.parse_suggestions_json or {})
    for key in (
        "draft_page_map",
        "draft_pages_ready",
        "draft_pages_dir",
        "draft_pages_count",
    ):
        sug.pop(key, None)
    volume.parse_suggestions_json = sug or None
    return row, created


def clear_volume_full_pdf(volume: Volume) -> dict[str, Any]:
    """解绑完整版 PDF（不删磁盘镜像文件）。保留目录课时，清空页图。"""
    from ..models import Lesson, LessonPage

    if not volume.blob_id:
        return {"cleared": False, "had_full_pdf": False, "lesson_pages_deleted": 0}

    volume.blob_id = None
    volume.parse_status = "pending"
    volume.parse_error = None

    lesson_ids = [r.id for r in Lesson.query.filter_by(volume_id=volume.id).all()]
    pages_deleted = 0
    if lesson_ids:
        pages_deleted = LessonPage.query.filter(LessonPage.lesson_id.in_(lesson_ids)).delete(
            synchronize_session=False
        )
    db.session.flush()
    return {
        "cleared": True,
        "had_full_pdf": True,
        "lesson_pages_deleted": int(pages_deleted or 0),
    }


def resolve_draft_blob_id(volume: Volume, preview_blob_id: str | None) -> str:
    """解析要使用的修订版 blob；默认取最新登记的 preview_blob_id。"""
    if preview_blob_id:
        bid = preview_blob_id.strip()
        row = VolumeDraftPdf.query.filter_by(volume_id=volume.id, blob_id=bid).first()
        if not row and volume.preview_blob_id != bid:
            raise ValueError("指定的修订版不属于本册")
        blob = db.session.get(FileBlob, bid)
        if not blob:
            raise ValueError("修订版 PDF blob 不存在")
        return bid

    rows = list_volume_draft_pdfs(volume)
    if rows:
        return rows[0].blob_id
    if volume.preview_blob_id:
        return volume.preview_blob_id
    raise ValueError("本册尚无修订版 PDF")


def draft_mirror_key(content: bytes) -> str:
    return sha256_hex(content)[:8]
