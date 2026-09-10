"""新教材册次 PDF 上传。"""
from __future__ import annotations

from pathlib import Path

from ....extensions import db
from ....parsers.pdf_spread import ensure_volume_page_layout, page_layout_label
from ...blobs import store_blob
from ..volumes import get_new_volume_by_code, volume_detail_dict
from .mirror import abort_pdf_mirror, finalize_pdf_mirror, stage_pdf_mirror

_PDF_MIME = "application/pdf"


def _normalize_mime(mime_type: str | None, filename: str | None) -> str:
    if mime_type and "pdf" in mime_type.lower():
        return _PDF_MIME
    if filename and filename.lower().endswith(".pdf"):
        return _PDF_MIME
    return mime_type or _PDF_MIME


def upload_volume_pdf(
    *,
    volume_code: str,
    content: bytes,
    filename: str | None = None,
    mime_type: str | None = None,
) -> dict:
    if not content:
        raise ValueError("上传文件为空")

    volume = get_new_volume_by_code(volume_code)
    mime = _normalize_mime(mime_type, filename)
    if mime != _PDF_MIME:
        raise ValueError("仅支持 PDF 文件")

    replaced = volume.blob_id is not None
    tmp_path: Path | None = None
    final_path: Path | None = None

    try:
        tmp_path, final_path = stage_pdf_mirror(
            edition_label=volume.edition,
            volume_code=volume.volume_code,
            content=content,
        )
        # 先落盘再 commit / 读版式：避免指向尚不存在的正式路径
        finalize_pdf_mirror(tmp_path, final_path)
        tmp_path = None

        blob, blob_created = store_blob(
            content=content,
            mime_type=mime,
            disk_path=final_path,
            storage_path=final_path,
        )
        volume.blob_id = blob.id
        volume.parse_status = "pending"
        volume.parse_error = None
        volume.parse_suggestions_json = None

        db.session.flush()
        # 上传后立刻判定单页/对开，供后续划分页码与页图裁半
        layout = ensure_volume_page_layout(
            volume, pdf_path=final_path, force=True, commit=False
        )

        db.session.commit()

        detail = volume_detail_dict(volume)
        detail.update(
            {
                "blob_id": blob.id,
                "blob_created": blob_created,
                "blob_size_bytes": blob.size_bytes,
                "blob_reused": not blob_created,
                "blob_storage": "disk" if blob.storage_path else "mysql",
                "pdf_replaced": replaced,
                "mirror_path": str(final_path),
                "upload_filename": filename,
                "page_layout": layout,
                "page_layout_label": page_layout_label(layout),
            }
        )
        return detail
    except Exception:
        db.session.rollback()
        if tmp_path is not None:
            abort_pdf_mirror(tmp_path)
        raise
