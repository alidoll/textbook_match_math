"""语文对开扫描：用 OCR 定位首课标题，校准 view = 印刷页 + x。"""
from __future__ import annotations

import logging
import re
from io import BytesIO
from pathlib import Path

_log = logging.getLogger(__name__)


def _ocr_view_text(png: bytes, ocr) -> str:
    import numpy as np
    from PIL import Image

    img = Image.open(BytesIO(png)).convert("RGB")
    w, h = img.size
    if w > 720:
        img = img.resize((720, max(1, int(h * 720 / w))))
    result, _ = ocr(np.asarray(img))
    if not result:
        return ""
    parts: list[str] = []
    for it in result:
        try:
            tp = it[1]
            txt = str(tp[0] if isinstance(tp, (list, tuple)) else tp).strip()
            if txt:
                parts.append(txt)
        except Exception:
            continue
    return "".join(parts)


def _view_looks_like_toc(text: str, needles: list[str]) -> bool:
    """目录页会同时列出多课标题；不能当正文锚点。"""
    raw = text or ""
    if "目录" in raw:
        return True
    from ...parsers.text_norm import norm_text

    blob = norm_text(raw)
    n = sum(1 for nd in needles if nd and nd in blob)
    return n >= 2


def _title_needle(title_raw: str) -> str:
    from ...parsers.text_norm import norm_text, strip_lesson_seq

    t = strip_lesson_seq(title_raw or "")
    t = re.sub(r"^[*＊]\s*", "", t)
    return norm_text(t)[:12]


def calibrate_spread_offset_via_ocr(
    pdf_path: Path,
    entries: list,
    *,
    max_views: int = 28,
    dpi: int = 72,
    layout: str = "spread",
) -> int | None:
    """
    扫描册：在前若干系统页 OCR 定位目录前几课标题，推算 x。
    layout=spread 时系统页为 view（半页）；single 时为 PDF 物理页。
    返回 offset（系统页 = 印刷页 + x），失败返回 None。
    """
    from collections import Counter

    from ...parsers.pdf_spread import PageLayout, render_view_page_png
    from ...parsers.pdf_toc import TocEntry
    from ...parsers.text_norm import norm_text

    page_layout: PageLayout = "spread" if layout == "spread" else "single"

    usable: list[TocEntry] = []
    for e in entries:
        if not isinstance(e, TocEntry):
            continue
        if e.page_1 is None or int(e.page_1) < 1:
            continue
        needle = _title_needle(e.title_raw)
        if len(needle) < 2:
            continue
        if any(k in needle for k in ("语文园地", "口语交际", "习作", "快乐读书")):
            continue
        usable.append(e)
        if len(usable) >= 3:
            break
    if len(usable) < 2:
        return None

    try:
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()
    except ImportError:
        _log.warning("RapidOCR 不可用，无法 OCR 校准目录偏移")
        return None

    # 跳过封面/目录：正文课名页不在最前几 view；目录页会同时命中多课标题
    start_view = 3 if page_layout == "single" else 5
    needles = [_title_needle(e.title_raw) for e in usable]
    hits: list[tuple[int, int, str]] = []
    try:
        for view in range(start_view, max_views + 1):
            png = render_view_page_png(
                pdf_path, view, dpi=dpi, layout=page_layout
            )
            raw = _ocr_view_text(png, ocr)
            if _view_looks_like_toc(raw, needles):
                continue
            text = norm_text(raw)
            if not text:
                continue
            for e in usable:
                needle = _title_needle(e.title_raw)
                if needle and needle in text:
                    hits.append((int(e.page_1), view, needle))
            found = {n for _, _, n in hits}
            if all(_title_needle(e.title_raw) in found for e in usable[:2]):
                if view > int(usable[1].page_1) + 20:
                    break
    except Exception as exc:
        _log.warning("OCR 校准目录偏移失败：%s", exc)
        return None

    first_hit: dict[str, tuple[int, int]] = {}
    for printed, view, needle in hits:
        prev = first_hit.get(needle)
        if prev is None or view < prev[1]:
            first_hit[needle] = (printed, view)

    xs: list[int] = []
    for needle, (printed, view) in first_hit.items():
        x = view - printed
        # x=0 常为目录 OCR 误命中；正文相对印刷页至少隔着封面/目录
        if 1 <= x <= 60:
            xs.append(x)
            _log.info(
                "OCR 锚点 %s：印刷 p%d → 系统页 %d ⇒ x=%d（%s）",
                needle,
                printed,
                view,
                x,
                page_layout,
            )
    if len(xs) < 2:
        return None
    best_x, cnt = Counter(xs).most_common(1)[0]
    if cnt < 2 and len(set(xs)) > 1:
        best_x = min(xs)
    _log.info(
        "OCR 校准偏移 x=%d（样本 %s，layout=%s）%s",
        best_x,
        xs,
        page_layout,
        pdf_path.name,
    )
    return best_x
