"""教材对比 PDF 上传。"""
from __future__ import annotations

from ...parsers.pdf_spread import (
    ensure_volume_page_layout,
    page_layout_label,
    stored_page_layout,
)
from ..volume_pdf import resolve_volume_pdf_path, upload_volume_pdf_with_role
from .volumes import diff_volume_detail, get_diff_volume_by_code


def upload_diff_volume_pdf(
    *,
    volume_code: str,
    content: bytes,
    filename: str | None = None,
    mime_type: str | None = None,
    pdf_role: str | None = "full",
) -> dict:
    volume = get_diff_volume_by_code(volume_code)
    uploaded = upload_volume_pdf_with_role(
        volume,
        content=content,
        filename=filename,
        mime_type=mime_type,
        pdf_role=pdf_role,
    )
    role = (pdf_role or "full").strip().lower()
    if role in ("full", "complete", ""):
        try:
            pdf_path = resolve_volume_pdf_path(volume, source="auto")
            layout = ensure_volume_page_layout(
                volume, pdf_path=pdf_path, force=True, commit=True
            )
            uploaded["page_layout"] = layout
            uploaded["page_layout_label"] = page_layout_label(layout)
        except Exception:
            pass
    detail = diff_volume_detail(volume)
    detail.update(uploaded)
    if "page_layout" not in detail:
        layout = stored_page_layout(volume)
        if layout:
            detail["page_layout"] = layout
            detail["page_layout_label"] = page_layout_label(layout)
    return detail
