"""将 PDF 单页渲染为 PNG。"""
from __future__ import annotations

from pathlib import Path


def render_pdf_page_png(
    pdf_path: Path,
    page_0: int,
    *,
    dpi: int = 120,
) -> bytes:
    import fitz

    doc = fitz.open(str(pdf_path))
    try:
        if page_0 < 0 or page_0 >= len(doc):
            raise ValueError(f"PDF 页码越界：{page_0 + 1}")
        page = doc.load_page(page_0)
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()
