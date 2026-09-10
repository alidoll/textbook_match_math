# -*- coding: utf-8 -*-
"""教材库：插图原子裁图落库（侧缓存 crop_blob_id）。"""
from __future__ import annotations

import io
import logging
from typing import Any

from ...extensions import db
from ...models import FileBlob, Lesson, LessonPage, Volume
from ..blobs import blob_id_available, read_blob_bytes, store_blob

logger = logging.getLogger(__name__)

_IMAGE_TYPES = frozenset({"image", "figure"})


def _atom_code(atom: dict[str, Any]) -> str:
    return str(atom.get("atom_id") or atom.get("atom_code") or "").strip()


def _atom_bbox(atom: dict[str, Any]) -> dict[str, float]:
    raw = atom.get("bbox") or atom.get("bbox_json") or {}
    if isinstance(raw, dict) and raw.get("x_end") is not None:
        return {
            "x_start": float(raw.get("x_start", 0)),
            "y_start": float(raw.get("y_start", 0)),
            "x_end": float(raw.get("x_end", 1)),
            "y_end": float(raw.get("y_end", 1)),
        }
    return {
        "x_start": float(atom.get("x_start", 0)),
        "y_start": float(atom.get("y_start", 0)),
        "x_end": float(atom.get("x_end", 1)),
        "y_end": float(atom.get("y_end", 1)),
    }


def crop_jpeg_from_page_bytes(
    page_bytes: bytes,
    atom: dict[str, Any],
    *,
    pad: float = 0.008,
) -> bytes:
    """按归一化 bbox 从页图 JPEG/PNG 字节裁出插图。"""
    from PIL import Image

    im = Image.open(io.BytesIO(page_bytes)).convert("RGB")
    w, h = im.size
    bb = _atom_bbox(atom)
    xs = max(0.0, float(bb["x_start"]) - pad)
    ys = max(0.0, float(bb["y_start"]) - pad)
    xe = min(1.0, float(bb["x_end"]) + pad)
    ye = min(1.0, float(bb["y_end"]) + pad)
    x0 = max(0, min(w - 2, int(xs * w)))
    y0 = max(0, min(h - 2, int(ys * h)))
    x1 = max(x0 + 2, min(w, int(xe * w)))
    y1 = max(y0 + 2, min(h, int(ye * h)))
    crop = im.crop((x0, y0, x1, y1))
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def volume_page_image_bytes(volume: Any, pdf_page: int) -> bytes | None:
    """从册 PDF 渲染单页 PNG 字节（无 LessonPage 时兜底）。"""
    if not getattr(volume, "blob_id", None):
        return None
    try:
        from ..old_library.pdf.parse import _pdf_path_for_volume
        from ...parsers.pdf_spread import layout_for_volume, render_view_page_png

        pdf_path = _pdf_path_for_volume(volume)
        return render_view_page_png(
            pdf_path,
            int(pdf_page),
            dpi=150,
            layout=layout_for_volume(volume, persist=False),
        )
    except Exception as exc:
        logger.warning(
            "volume page render failed vol=%s p=%s: %s",
            getattr(volume, "volume_code", "?"),
            pdf_page,
            exc,
        )
        return None


def _page_image_bytes(
    *,
    volume: Volume,
    les: Lesson,
    pdf_page: int,
    page_index: int,
) -> bytes | None:
    lp = LessonPage.query.filter_by(
        lesson_id=les.id, page_index=int(page_index)
    ).first()
    if lp and lp.blob_id and blob_id_available(lp.blob_id):
        blob = FileBlob.query.get(lp.blob_id)
        if blob:
            return read_blob_bytes(blob)
    if not volume.blob_id:
        return None
    rendered = volume_page_image_bytes(volume, int(pdf_page))
    if rendered:
        return rendered
    return None


def ensure_library_atom_crops(
    *,
    volume: Volume,
    les: Lesson,
    pdf_page: int,
    page_index: int,
    atoms: list[dict[str, Any]],
    image_ocr_done: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """为 image/figure 原子补 crop_blob_id；返回 (atoms, changed)。"""
    if not image_ocr_done or not atoms:
        return list(atoms or []), False
    has_image = any(
        str(a.get("atom_type") or "").strip() in _IMAGE_TYPES for a in atoms
    )
    if not has_image:
        return list(atoms or []), False

    page_bytes = _page_image_bytes(
        volume=volume,
        les=les,
        pdf_page=int(pdf_page),
        page_index=int(page_index),
    )
    if not page_bytes:
        return list(atoms or []), False

    out = [dict(a) for a in atoms]
    changed = False
    for atom in out:
        if str(atom.get("atom_type") or "").strip() not in _IMAGE_TYPES:
            continue
        existing = str(atom.get("crop_blob_id") or "").strip()
        if existing and blob_id_available(existing):
            continue
        try:
            jpeg = crop_jpeg_from_page_bytes(page_bytes, atom)
            blob, _ = store_blob(
                content=jpeg,
                mime_type="image/jpeg",
                force_new=True,
            )
            atom["crop_blob_id"] = blob.id
            changed = True
            logger.info(
                "library atom crop saved vol=%s p=%s atom=%s blob=%s",
                volume.volume_code,
                pdf_page,
                _atom_code(atom),
                blob.id,
            )
        except Exception as exc:
            logger.warning(
                "library atom crop failed vol=%s p=%s atom=%s: %s",
                volume.volume_code,
                pdf_page,
                _atom_code(atom),
                exc,
            )
    if changed:
        db.session.commit()
    return out, changed
