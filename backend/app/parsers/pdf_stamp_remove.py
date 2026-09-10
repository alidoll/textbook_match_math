"""扫描版 PDF / 页图：红色印章与手写红笔批注去除（OCR 前预处理）。"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import cv2
import fitz
import numpy as np

_log = logging.getLogger(__name__)

# 极淡红也会触发；细笔迹像素占比常远低于旧印章阈值
_MIN_RED_INK_PIXEL_RATIO = 0.00005


def _raw_red_pixels(bgr: np.ndarray) -> np.ndarray:
    """HSV + 红通道主导：印章与手写红笔共用底层检测。"""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 30, 30), (15, 255, 255))
    m2 = cv2.inRange(hsv, (165, 30, 30), (180, 255, 255))
    red = cv2.bitwise_or(m1, m2)
    b, g, r = cv2.split(bgr)
    dom = (
        (r.astype(np.int16) - g > 18)
        & (r.astype(np.int16) - b > 18)
        & (r > 90)
    ).astype(np.uint8) * 255
    return cv2.bitwise_or(red, dom)


def red_pen_mask(bgr: np.ndarray) -> np.ndarray:
    """细红笔圈画/下划线：小核连通，避免大膨胀吃掉印刷标点。"""
    raw = _raw_red_pixels(bgr)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, k, iterations=1)
    mask = cv2.dilate(mask, k, iterations=1)
    return mask


def red_stamp_mask(bgr: np.ndarray) -> np.ndarray:
    """红色圆形/椭圆印章 + 手写红笔批注（合并掩膜）。"""
    raw = _raw_red_pixels(bgr)
    # 印章：大核闭合 + 膨胀
    k_stamp = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    stamp = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, k_stamp, iterations=2)
    stamp = cv2.dilate(stamp, k_stamp, iterations=2)
    # 红笔：小核，保留细笔画
    pen = red_pen_mask(bgr)
    return cv2.bitwise_or(stamp, pen)


def stamp_pixel_ratio(bgr: np.ndarray) -> float:
    mask = red_stamp_mask(bgr)
    return float(np.count_nonzero(mask)) / max(mask.size, 1)


def remove_red_stamp_from_bgr(bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    去除红色印章与手写红笔：白纸区填白，压字区去红留黑，淡红晕 inpaint。
    返回 (cleaned_bgr, red_ink_mask)。
    """
    mask = red_stamp_mask(bgr)
    if not np.any(mask):
        return bgr, mask

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    mn = np.min(bgr, axis=2)
    out = bgr.copy().astype(np.float32)
    m = mask > 0
    light = m & (gray > 145)
    out[light] = 255
    dark = m & (gray <= 145)
    v = np.minimum(mn, gray).astype(np.float32)
    out[dark, 0] = v[dark]
    out[dark, 1] = v[dark]
    out[dark, 2] = v[dark]
    out = out.astype(np.uint8)

    # 残留淡红晕：用细笔核再扫一轮，避免大核二次膨胀
    residual = red_pen_mask(out)
    halo = cv2.bitwise_and(residual, cv2.bitwise_not(mask))
    halo = cv2.dilate(
        halo,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )
    if np.any(halo):
        out = cv2.inpaint(out, halo, 3, cv2.INPAINT_TELEA)
    return out, mask


def _downscale_bgr(bgr: np.ndarray, *, max_side: int = 1400) -> np.ndarray:
    h, w = bgr.shape[:2]
    side = max(h, w)
    if side <= max_side:
        return bgr
    scale = max_side / side
    nh, nw = int(h * scale), int(w * scale)
    return cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)


def page_to_bgr(page: fitz.Page, *, zoom: float = 1.2) -> np.ndarray:
    return _page_to_bgr(page, zoom=zoom)


def prepare_catalog_page_bgr(
    page: fitz.Page,
    *,
    zoom: float = 1.2,
    max_side: int = 1400,
) -> np.ndarray:
    """渲染目录页并在内存中去红章/红笔（不生成临时 PDF）。"""
    return prepare_catalog_page_bgrs(page, zoom=zoom, max_side=max_side)[0]


def prepare_catalog_page_bgrs(
    page: fitz.Page,
    *,
    zoom: float = 1.2,
    max_side: int = 1400,
) -> list[np.ndarray]:
    """渲染目录页：对开扫描先拆左右再缩放去章，避免只看清右半页。"""
    from .pdf_spread import SPREAD_ASPECT_MIN, split_catalog_spread_bgr

    r = page.rect
    z = zoom
    if float(r.width) / max(float(r.height), 1.0) >= SPREAD_ASPECT_MIN:
        z = max(zoom, 2.2)
    regions: list[np.ndarray] = []
    for part in split_catalog_spread_bgr(_page_to_bgr(page, zoom=z)):
        bgr = _downscale_bgr(part, max_side=max_side)
        if stamp_pixel_ratio(bgr) >= _MIN_RED_INK_PIXEL_RATIO:
            bgr, _ = remove_red_stamp_from_bgr(bgr)
        regions.append(bgr)
    return regions or [_downscale_bgr(_page_to_bgr(page, zoom=zoom), max_side=max_side)]


def prepare_lesson_page_bgr_from_path(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """
    读取 lesson_pages 落盘的 PNG/JPG，必要时去红章与手写红笔。
    返回 (bgr, red_ink_mask)；无红墨时 mask 全零。
    """
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"无法读取页面图 {path}")
    ratio = stamp_pixel_ratio(bgr)
    if ratio >= _MIN_RED_INK_PIXEL_RATIO:
        cleaned, mask = remove_red_stamp_from_bgr(bgr)
        _log.info(
            "lesson page %s 检测到红章/红笔（%.3f%%），已去除",
            path.name,
            ratio * 100,
        )
        return cleaned, mask
    return bgr, np.zeros(bgr.shape[:2], dtype=np.uint8)


def _page_to_bgr(page: fitz.Page, *, zoom: float) -> np.ndarray:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        return cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def _bgr_to_png_bytes(bgr: np.ndarray) -> bytes:
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    ok, buf = cv2.imencode(".png", rgb)
    if not ok:
        raise ValueError("无法编码去印章页面")
    return buf.tobytes()


def build_catalog_pdf_without_stamps(
    pdf_path: Path,
    *,
    max_pages: int = 9,
    zoom: float = 1.2,
    force_sparse: bool = False,
) -> Path | None:
    """
    若目录区存在红章/红笔（或 force_sparse 为真），生成仅前几页去红的临时 PDF。
    未改动时返回 None，调用方继续使用原 PDF。
    """
    src = fitz.open(str(pdf_path))
    page_count = len(src)
    if page_count == 0:
        src.close()
        return None

    n_clean = min(max_pages, page_count)
    clean_flags: list[bool] = []
    cleaned_images: list[np.ndarray | None] = []

    for i in range(n_clean):
        bgr = _page_to_bgr(src.load_page(i), zoom=zoom)
        ratio = stamp_pixel_ratio(bgr)
        need = force_sparse or ratio >= _MIN_RED_INK_PIXEL_RATIO
        clean_flags.append(need)
        if need:
            cleaned, _ = remove_red_stamp_from_bgr(bgr)
            cleaned_images.append(cleaned)
            if ratio >= _MIN_RED_INK_PIXEL_RATIO:
                _log.info(
                    "PDF %s 第 %d 页检测到红章/红笔（%.3f%%），已去除",
                    pdf_path.name,
                    i + 1,
                    ratio * 100,
                )
        else:
            cleaned_images.append(None)

    if not any(clean_flags):
        src.close()
        return None

    out = fitz.open()
    try:
        for i in range(page_count):
            if i < n_clean and clean_flags[i] and cleaned_images[i] is not None:
                src_page = src.load_page(i)
                new_page = out.new_page(
                    width=src_page.rect.width,
                    height=src_page.rect.height,
                )
                new_page.insert_image(
                    new_page.rect,
                    stream=_bgr_to_png_bytes(cleaned_images[i]),
                )
            else:
                out.insert_pdf(src, from_page=i, to_page=i)

        tmp = Path(tempfile.gettempdir()) / f"textbook-match-stamp-{pdf_path.stem}.pdf"
        out.save(str(tmp))
        _log.info(
            "PDF %s 目录区去红章/红笔：前 %d 页中 %d 页已处理 → %s",
            pdf_path.name,
            n_clean,
            sum(1 for f in clean_flags if f),
            tmp.name,
        )
        return tmp
    finally:
        out.close()
        src.close()
