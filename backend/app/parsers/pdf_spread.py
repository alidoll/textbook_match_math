"""PDF 页布局：单页 / 对开扫描（读时虚拟拆左/右半）。"""
from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

PageLayout = Literal["single", "spread"]
PageSide = Literal["left", "right"]

# 宽/高 ≥ 此值视为对开扫描（旧语文样张约 1.41；新册单页约 0.67）
SPREAD_ASPECT_MIN = 1.15

_log = logging.getLogger(__name__)


def split_catalog_spread_bgr(bgr):
    """对开扫描图拆成左右页；单页原样返回。供目录视觉识别读全左右栏。"""
    import numpy as np

    arr = np.asarray(bgr)
    if arr.ndim < 2:
        return [arr]
    h, w = int(arr.shape[0]), int(arr.shape[1])
    if w / max(h, 1) < SPREAD_ASPECT_MIN:
        return [arr]
    mid = w // 2
    return [arr[:, :mid], arr[:, mid:]]


def layout_for_subject(subject: str | None) -> PageLayout:
    """无 PDF 时的弱默认：一律单页。对开只能由 PDF 宽高比判定。"""
    return "single"


def stored_page_layout(volume) -> PageLayout | None:
    sug = getattr(volume, "parse_suggestions_json", None) or {}
    if not isinstance(sug, dict):
        return None
    layout = sug.get("page_layout")
    if layout in ("single", "spread"):
        return layout  # type: ignore[return-value]
    return None


def store_page_layout(
    volume,
    layout: PageLayout,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    sug = dict(getattr(volume, "parse_suggestions_json", None) or {})
    sug["page_layout"] = layout
    if meta:
        sug["page_layout_meta"] = meta
    volume.parse_suggestions_json = sug


def detect_page_layout_from_pdf(
    pdf_path: Path,
    *,
    sample_count: int = 5,
) -> tuple[PageLayout, dict[str, Any]]:
    """
    按页面 MediaBox 宽高比判定：横长 → spread，竖长 → single。
    取样多页取中位数，避免个别异形页干扰。
    """
    import fitz

    path = Path(pdf_path)
    doc = fitz.open(str(path))
    try:
        n = len(doc)
        if n <= 0:
            return "single", {"reason": "empty_pdf", "ratio": None}
        idxs = sorted({0, 1, max(0, n // 4), max(0, n // 2), max(0, n - 1)})
        idxs = [i for i in idxs if i < n][: max(1, sample_count)]
        ratios: list[float] = []
        for i in idxs:
            r = doc[i].rect
            h = float(r.height) or 1.0
            ratios.append(float(r.width) / h)
        ratios.sort()
        med = ratios[len(ratios) // 2]
        layout: PageLayout = "spread" if med >= SPREAD_ASPECT_MIN else "single"
        meta = {
            "reason": "aspect_ratio",
            "ratio": round(med, 3),
            "threshold": SPREAD_ASPECT_MIN,
            "sample_pages": [i + 1 for i in idxs],
            "sample_ratios": [round(x, 3) for x in ratios],
        }
        _log.info(
            "PDF 页布局 %s → %s (ratio=%.3f, samples=%s)",
            path.name,
            layout,
            med,
            meta["sample_ratios"],
        )
        return layout, meta
    finally:
        doc.close()


def ensure_volume_page_layout(
    volume,
    *,
    pdf_path: Path | None = None,
    force: bool = False,
    commit: bool = False,
) -> PageLayout:
    """读取或检测册次 page_layout，可选写回 parse_suggestions_json。"""
    if not force:
        cached = stored_page_layout(volume)
        if cached is not None:
            return cached

    path = pdf_path
    if path is None:
        try:
            from ..services.volume_pdf import resolve_volume_pdf_path

            path = resolve_volume_pdf_path(volume, source="auto")
        except Exception:
            path = None

    if path is not None and Path(path).is_file():
        layout, meta = detect_page_layout_from_pdf(Path(path))
        store_page_layout(volume, layout, meta=meta)
        if commit:
            from ..extensions import db

            db.session.add(volume)
            db.session.commit()
        return layout

    layout = layout_for_subject(getattr(volume, "subject", None))
    store_page_layout(
        volume,
        layout,
        meta={"reason": "no_pdf_default", "ratio": None},
    )
    if commit:
        from ..extensions import db

        db.session.add(volume)
        db.session.commit()
    return layout


def layout_for_volume(
    volume,
    *,
    pdf_path: Path | None = None,
    persist: bool = True,
) -> PageLayout:
    """册次页布局：优先已存结果，否则按 PDF 宽高比检测。"""
    if persist:
        return ensure_volume_page_layout(volume, pdf_path=pdf_path, force=False, commit=False)
    cached = stored_page_layout(volume)
    if cached is not None:
        return cached
    if pdf_path is not None and Path(pdf_path).is_file():
        layout, _ = detect_page_layout_from_pdf(Path(pdf_path))
        return layout
    return layout_for_subject(getattr(volume, "subject", None))


def page_layout_label(layout: PageLayout | str | None) -> str:
    if layout == "spread":
        return "对开拆分"
    return "单页"


def view_count_from_sheets(sheet_count: int, layout: PageLayout) -> int:
    if sheet_count < 0:
        raise ValueError(f"sheet_count 无效：{sheet_count}")
    if layout == "spread":
        return sheet_count * 2
    return sheet_count


def view_to_sheet_side(view_1: int) -> tuple[int, PageSide]:
    """view 1→sheet1 左，2→sheet1 右，3→sheet2 左…"""
    if view_1 < 1:
        raise ValueError(f"view 页码无效：{view_1}")
    sheet_1 = (view_1 + 1) // 2
    side: PageSide = "left" if view_1 % 2 == 1 else "right"
    return sheet_1, side


def sheet_side_to_view(sheet_1: int, side: PageSide) -> int:
    if sheet_1 < 1:
        raise ValueError(f"sheet 页码无效：{sheet_1}")
    return 2 * sheet_1 - 1 if side == "left" else 2 * sheet_1


def crop_half_png(
    png_bytes: bytes,
    side: PageSide,
    *,
    gutter_ratio: float = 0.0,
) -> bytes:
    from PIL import Image

    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    mid = w // 2
    gutter = max(0, int(w * gutter_ratio))
    if side == "left":
        box = (0, 0, max(1, mid - gutter), h)
    else:
        box = (min(w - 1, mid + gutter), 0, w, h)
    out = BytesIO()
    img.crop(box).save(out, format="PNG")
    return out.getvalue()


def render_view_page_png(
    pdf_path: Path,
    view_1: int,
    *,
    dpi: int = 120,
    layout: PageLayout = "single",
    gutter_ratio: float = 0.0,
) -> bytes:
    """按 view 渲染；spread 时裁对开图左/右半。"""
    from .pdf_render import render_pdf_page_png

    if view_1 < 1:
        raise ValueError(f"view 页码无效：{view_1}")
    if layout == "single":
        return render_pdf_page_png(pdf_path, view_1 - 1, dpi=dpi)

    sheet_1, side = view_to_sheet_side(view_1)
    full = render_pdf_page_png(pdf_path, sheet_1 - 1, dpi=dpi)
    return crop_half_png(full, side, gutter_ratio=gutter_ratio)
