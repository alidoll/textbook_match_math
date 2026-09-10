"""未定稿预览 PDF 与旧教材对应页对比（OCR + 目录映射）。"""
from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

from ...models import FileBlob, Volume
from ...parsers.catalog_page_offset import get_cached_catalog_pdf_offset
from ...parsers.llm_toc_cache import get_llm_toc_entries
from ...repo_paths import base_data_dir
from .catalog import _pdf_path_for_volume
from .lesson_match import lesson_at_pdf_page

PdfSource = str  # "full" | "draft"

_log = logging.getLogger(__name__)

_DRAFT_PAGE_THRESHOLD = 80


def _norm(s: str) -> str:
    return re.sub(r"[\s·●…．.．_—\-·\u00a0\u3000]+", "", s or "")


# 预览扫描稿页眉页脚常见噪声（CMYK、时间戳、文件码、裁切标记等）
_SCAN_LINE_RES = (
    re.compile(r"CMYK", re.I),
    re.compile(r"Time\s*:\s*\d{4}/\d{2}/\d{2}", re.I),
    re.compile(r"TLPC\d+", re.I),
    re.compile(r"SCGL", re.I),
    re.compile(r"New\s+D\b", re.I),
    re.compile(r"pdf_p\d+", re.I),
    re.compile(r"p\d{4}_p\d{4}", re.I),
    re.compile(r"S\d{3}\b"),
    re.compile(r"^\s*[_./:\-\d\s]+\s*$"),
    re.compile(r"^[\sL]+$"),  # 裁切 L 形标记
)


def _is_scan_noise_line(line: str) -> bool:
    s = (line or "").strip()
    if not s or len(s) <= 1:
        return True
    if "人民教育出版社" in s and len(s) < 20:
        return True
    if re.match(r"^[\d_./:\-\sCMYKTLPCSCGL]+(?:\s|$)", s, re.I) and len(s) < 80:
        if any(p.search(s) for p in _SCAN_LINE_RES[:7]):
            return True
    for pat in _SCAN_LINE_RES:
        if pat.search(s):
            return True
    return False


def _clean_ocr_for_compare(text: str) -> str:
    """去掉扫描元数据行，保留正文 OCR 便于比对。"""
    kept: list[str] = []
    for raw in (text or "").split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or _is_scan_noise_line(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def _content_lines(text: str) -> list[str]:
    return [ln for ln in _clean_ocr_for_compare(text).split("\n") if ln.strip()]


def _text_similarity(a: str, b: str) -> float:
    """基于清洗后正文的文本相似度。"""
    na, nb = _norm(_clean_ocr_for_compare(a)), _norm(_clean_ocr_for_compare(b))
    if not na or not nb:
        return 0.0
    try:
        from rapidfuzz import fuzz

        return fuzz.ratio(na, nb) / 100.0
    except ImportError:
        return SequenceMatcher(None, na, nb).ratio()


def _similarity(a: str, b: str) -> float:
    return _text_similarity(a, b)


def _render_page_gray(doc, page_index: int, *, zoom: float = 1.0, crop: float = 0.08):
    """渲染 PDF 页为灰度图，裁掉边缘扫描噪声区。"""
    import fitz
    import numpy as np

    page = doc.load_page(page_index)
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n >= 3:
        gray = (
            0.299 * arr[:, :, 0].astype(np.float32)
            + 0.587 * arr[:, :, 1].astype(np.float32)
            + 0.114 * arr[:, :, 2].astype(np.float32)
        ).astype(np.uint8)
    else:
        gray = arr[:, :, 0]
    h, w = gray.shape
    y0, y1 = int(h * crop), int(h * (1 - crop))
    x0, x1 = int(w * crop), int(w * (1 - crop))
    if y1 > y0 and x1 > x0:
        gray = gray[y0:y1, x0:x1]
    return gray


def _page_image_similarity(old_doc, old_index: int, new_doc, new_index: int) -> float:
    """页面图像相似度：裁边 + 二值化后比较内容像素。"""
    import cv2
    import numpy as np

    g_old = _render_page_gray(old_doc, old_index, zoom=1.0)
    g_new = _render_page_gray(new_doc, new_index, zoom=1.5)
    target_w = 720
    def _resize(g):
        h, w = g.shape[:2]
        nh = max(1, int(h * target_w / max(w, 1)))
        return cv2.resize(g, (target_w, nh), interpolation=cv2.INTER_AREA)

    g_old, g_new = _resize(g_old), _resize(g_new)
    h = min(g_old.shape[0], g_new.shape[0])
    w = min(g_old.shape[1], g_new.shape[1])
    g_old, g_new = g_old[:h, :w], g_new[:h, :w]

    _, b_old = cv2.threshold(g_old, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, b_new = cv2.threshold(g_new, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    diff = cv2.absdiff(b_old, b_new)
    pixel_sim = 1.0 - float(np.count_nonzero(diff)) / float(diff.size)

    # 灰度相关（对轻微亮度差更稳）
    a = g_old.astype(np.float32)
    b = g_new.astype(np.float32)
    a = (a - a.mean()) / (a.std() + 1e-6)
    b = (b - b.mean()) / (b.std() + 1e-6)
    corr = float(np.clip(np.mean(a * b), -1.0, 1.0))
    corr_sim = (corr + 1.0) / 2.0

    return round(max(0.0, min(1.0, 0.65 * pixel_sim + 0.35 * corr_sim)), 4)


def _fuzz_ratio(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    try:
        from rapidfuzz import fuzz

        return fuzz.ratio(na, nb) / 100.0
    except ImportError:
        return SequenceMatcher(None, na, nb).ratio()


def _ocr_equivalent(a: str, b: str, *, threshold: float = 0.88) -> bool:
    """两句 OCR 文本是否可视为同一内容（容忍误识别）。"""
    if _fuzz_ratio(a, b) >= threshold:
        return True
    na, nb = _norm(a), _norm(b)
    na = na.replace("Os", "O5").replace("oS", "o5")
    nb = nb.replace("Os", "O5").replace("oS", "o5")
    if na == nb:
        return True
    if _fuzz_ratio(na, nb) >= threshold:
        return True
    # 长短悬殊时不做 partial 等价（避免半句误配整段）
    if min(len(na), len(nb)) / max(len(na), len(nb), 1) >= 0.75:
        try:
            from rapidfuzz import fuzz

            return fuzz.partial_ratio(na, nb) / 100.0 >= max(threshold, 0.92)
        except ImportError:
            pass
    return False


_CO2_OTHER_RE = re.compile(r"化碳([\d.]+)%[、,]?其他气体([\d.]+)%")


def _body_for_percent_parse(text: str) -> str:
    """保留小数点，供百分比解析。"""
    return re.sub(r"[\s·●…．_—\-·\u00a0\u3000]+", "", _clean_ocr_for_compare(text))


def _parse_co2_other_percentages(text: str) -> tuple[str, str] | None:
    m = _CO2_OTHER_RE.search(_body_for_percent_parse(text))
    if not m:
        return None
    return m.group(1), m.group(2)


def _format_co2_other_display(co2: str, other: str) -> str:
    return f"化碳{co2}%、其他气体{other}%（如图2-3）"


def _extract_factual_diffs(old_text: str, new_text: str) -> list[dict]:
    """提取可核实的数据类差异（如空气成分百分比）。"""
    diffs: list[dict] = []
    old_comp = _parse_co2_other_percentages(old_text)
    new_comp = _parse_co2_other_percentages(new_text)
    if old_comp and new_comp and old_comp != new_comp:
        diffs.append(
            {
                "kind": "改",
                "category": "数据",
                "old": _format_co2_other_display(*old_comp),
                "new": _format_co2_other_display(*new_comp),
            }
        )
    return diffs


def _extract_clause_diff_strict(
    old_text: str, new_text: str, *, max_items: int = 10
) -> list[dict]:
    """仅用于全文相似度较低页面的严格逐句对比。"""
    old_clauses = _split_clauses(old_text)
    new_clauses = _split_clauses(new_text)
    if not old_clauses or not new_clauses:
        return []

    changes: list[dict] = []
    i = j = 0
    while i < len(old_clauses) and j < len(new_clauses):
        o, n = old_clauses[i], new_clauses[j]
        if _ocr_equivalent(o, n):
            i += 1
            j += 1
            continue
        if _appears_in_other(o, new_text) and not _appears_in_other(n, old_text):
            i += 1
            continue
        if _appears_in_other(n, old_text) and not _appears_in_other(o, new_text):
            j += 1
            continue
        sim = _fuzz_ratio(o, n)
        if sim >= 0.58:
            changes.append({"kind": "改", "old": o[:120], "new": n[:120]})
            i += 1
            j += 1
            continue
        if not _appears_in_other(o, new_text) and len(_norm(o)) >= 12:
            changes.append({"kind": "删", "old": o[:120]})
        i += 1
        if not _appears_in_other(n, old_text) and len(_norm(n)) >= 12:
            changes.append({"kind": "增", "new": n[:120]})
        j += 1

    while i < len(old_clauses):
        o = old_clauses[i]
        if not _appears_in_other(o, new_text) and len(_norm(o)) >= 12:
            changes.append({"kind": "删", "old": o[:120]})
        i += 1
    while j < len(new_clauses):
        n = new_clauses[j]
        if not _appears_in_other(n, old_text) and len(_norm(n)) >= 12:
            changes.append({"kind": "增", "new": n[:120]})
        j += 1
    return changes[:max_items]


def _extract_text_diff(old_text: str, new_text: str, *, max_items: int = 10) -> list[dict]:
    """
    返回实质性差异。优先可核实的数据（百分比等）；
    高相似度页不再做易错配的 OCR 逐句比对。
    """
    factual = _extract_factual_diffs(old_text, new_text)
    if factual:
        return factual[:max_items]

    if _text_similarity(old_text, new_text) >= 0.82:
        return []

    return _extract_clause_diff_strict(old_text, new_text, max_items=max_items)


def _merge_body_text(text: str) -> str:
    return _norm(_clean_ocr_for_compare(text))


def _prepare_clause_blob(text: str) -> str:
    """合并正文并在版式标签/图题处插入虚拟断句，避免 OCR 换行错位。"""
    blob = _merge_body_text(text)
    if not blob:
        return ""
    blob = re.sub(r"([①②③④⑤⑥⑦⑧⑨⑩])", r"。\1", blob)
    blob = re.sub(r"(课题\s*\d+)", r"。\1", blob)
    blob = re.sub(r"(图\s*\d+[\-\—]?\d*)", r"。\1", blob)
    for label in ("思考与讨论", "探究", "复习与提高", "整理与提升", "资料", "练习"):
        blob = blob.replace(label, f"。{label}")
    return blob


def _split_clauses(text: str, *, min_len: int = 12) -> list[str]:
    """按句号等切分语义句，避免 OCR 换行导致的假差异。"""
    blob = _prepare_clause_blob(text)
    if not blob:
        return []
    parts = re.split(r"(?<=[。！？；：])", blob)
    clauses: list[str] = []
    buf = ""
    for part in parts:
        buf += part
        if not buf:
            continue
        if buf[-1] in "。！？；：" or len(buf) >= 80:
            chunk = buf.rstrip("。！？；：")
            if len(chunk) >= min_len:
                clauses.append(chunk)
            buf = ""
    if buf:
        chunk = buf.rstrip("。！？；：")
        if len(chunk) >= min_len:
            clauses.append(chunk)
    return clauses


def _appears_in_other(clause: str, other_text: str, *, threshold: float = 0.90) -> bool:
    """该片段是否已出现在另一侧全文中（版式换行/重排）。"""
    nc = _norm(clause)
    if not nc or len(nc) < 6:
        return True
    blob = _merge_body_text(other_text)
    if nc in blob:
        return True
    try:
        from rapidfuzz import fuzz

        return fuzz.partial_ratio(nc, blob) / 100.0 >= threshold
    except ImportError:
        return False


def _effective_similarity(text_sim: float, image_sim: float) -> float:
    """综合文字 OCR 与页面图像：扫描元数据导致文字偏低时更信图像；正文一致时更信文字。"""
    if text_sim >= 0.85:
        return round(0.78 * text_sim + 0.22 * image_sim, 4)
    if image_sim >= 0.82 and text_sim < image_sim - 0.12:
        return round(0.94 * image_sim + 0.06 * text_sim, 4)
    return round(0.58 * text_sim + 0.42 * image_sim, 4)


def _analyze_page_pair(
    old_doc,
    old_index: int,
    old_text: str,
    new_doc,
    new_index: int,
    new_text: str,
) -> dict:
    text_sim = _text_similarity(old_text, new_text)
    image_sim = _page_image_similarity(old_doc, old_index, new_doc, new_index)
    effective = _effective_similarity(text_sim, image_sim)
    diffs = _extract_text_diff(old_text, new_text)
    change = _change_level(effective)
    detail_parts: list[str] = []
    detail_parts.append(
        f"文字 OCR 相似 {int(text_sim * 100)}% · 页面图像 {int(image_sim * 100)}%"
    )
    if image_sim >= 0.85 and text_sim < 0.55:
        detail_parts.append("差异主要来自扫描页眉页脚（CMYK/时间戳等），正文视觉一致。")
    elif not diffs and effective >= 0.88:
        detail_parts.append("逐句比对未发现实质性改写（差异多为 OCR 误识别或版式换行）。")
    elif not diffs:
        detail_parts.append("未发现可确认的实质性文字改写。")
    elif diffs and diffs[0].get("category") == "数据":
        detail_parts.append("检测到空气成分等数据差异，明细如下。")
    elif diffs:
        detail_parts.append(f"检测到 {len(diffs)} 处文字差异，明细如下。")
    else:
        detail_parts.append(change_summary_text(change))
    return {
        "text_similarity": round(text_sim, 3),
        "image_similarity": round(image_sim, 3),
        "similarity": round(effective, 3),
        "change": change,
        "text_diff": diffs,
        "change_detail": " ".join(detail_parts),
    }


def _resolve_new_pdf_path(
    new_vol: Volume,
    *,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> Path:
    from ..volume_pdf import resolve_volume_pdf_path

    source = "draft" if new_pdf_source == "draft" else "full"
    return resolve_volume_pdf_path(
        new_vol,
        source=source,
        preview_blob_id=preview_blob_id if new_pdf_source == "draft" else None,
    )


def pdf_page_count(
    volume: Volume,
    *,
    pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> int | None:
    try:
        import fitz

        from ...parsers.pdf_spread import layout_for_volume, view_count_from_sheets

        if pdf_source == "draft":
            path = _resolve_new_pdf_path(
                volume,
                new_pdf_source="draft",
                preview_blob_id=preview_blob_id,
            )
        else:
            path = _pdf_path_for_volume(volume)
        with fitz.open(str(path)) as doc:
            sheets = len(doc)
        return view_count_from_sheets(sheets, layout_for_volume(volume))
    except Exception:
        return None


def is_draft_preview_volume(volume: Volume, *, lesson_count: int = 0) -> bool:
    pages = pdf_page_count(volume)
    if pages is not None and pages < _DRAFT_PAGE_THRESHOLD:
        return True
    return lesson_count < 3 and bool(volume.blob_id)


def _ocr_page(doc, page_index: int, ocr, *, zoom: float = 1.5) -> str:
    return _ocr_page_region(doc, page_index, ocr, y0_ratio=0.0, y1_ratio=1.0, zoom=zoom)


def _ocr_page_region(
    doc,
    page_index: int,
    ocr,
    *,
    y0_ratio: float = 0.0,
    y1_ratio: float = 1.0,
    zoom: float = 1.5,
) -> str:
    from io import BytesIO

    import fitz
    import numpy as np
    from PIL import Image

    page = doc.load_page(page_index)
    text = (page.get_text() or "").strip()
    if y0_ratio <= 0.0 and y1_ratio >= 1.0 and len(text) > 120:
        return text
    rect = page.rect
    y0 = rect.y0 + rect.height * max(0.0, min(y0_ratio, 1.0))
    y1 = rect.y0 + rect.height * max(y0_ratio, min(y1_ratio, 1.0))
    if y1 <= y0:
        return text
    clip = fitz.Rect(rect.x0, y0, rect.x1, y1)
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
    img = Image.open(BytesIO(pix.tobytes("png")))
    arr = np.asarray(img)
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    result, _ = ocr(arr)
    if not result:
        return text
    lines: list[str] = []
    for it in result:
        tp = it[1]
        txt = str(tp[0] if isinstance(tp, (list, tuple)) else tp).strip()
        if txt:
            lines.append(txt)
    return "\n".join(lines) if lines else text


def _ocr_page_footer(doc, page_index: int, ocr, *, zoom: float = 2.0) -> str:
    """只 OCR 页脚区，读取「172 第七单元 …」等目录印刷页码。"""
    for y0 in (0.82, 0.74):
        text = _ocr_page_region(doc, page_index, ocr, y0_ratio=y0, y1_ratio=1.0, zoom=zoom)
        if parse_new_page_footer(text):
            return text
    return _ocr_page_region(doc, page_index, ocr, y0_ratio=0.74, y1_ratio=1.0, zoom=zoom)


_FOOTER_UNIT_PAGE_RE = re.compile(
    r"(?P<page>\d{1,3})\s*第([一二三四五六七八九十\d]+)单元\s*(?P<title>[^\d\nCMYKTLPC]{2,40})"
)
_FOOTER_UNIT_PAGE_COMPACT_RE = re.compile(
    r"(?P<page>\d{1,3})第([一二三四五六七八九十\d]+)单元(?P<title>[^\d\nCMYKTLPC]{2,40})"
)
_FOOTER_LESSON_PAGE_RE = re.compile(
    r"课题\s*[\d一二三四五六七八九十]+[^\d\n]{2,48}?(?P<page>\d{1,3})\s*(?:CMYK|TLPC|SCGL|New|$)",
    re.I,
)
# 「复习与提高79」「练习与应用 31」：栏目名 + 页码（勿标成「目录」）
_FOOTER_NAMED_SECTION_PAGE_RE = re.compile(
    r"^(?P<section>练习与应用|复习与提高|整理与提升|探究与实践)\s*(?P<page>\d{1,3})\s*(?:CMYK|TLPC|SCGL|New.*)?$",
    re.I,
)
# 「6 绪论 …」「6绪论…」：页码在前、章节名在后（允许无空格）
_FOOTER_LEADING_PAGE_SECTION_RE = re.compile(
    r"^(?P<page>\d{1,3})\s*(?P<label>(?:绪论|第[一二三四五六七八九十\d]+单元|课题\s*[\d一二三四五六七八九十]+)[^\nCMYKTLPC]{0,48})",
    re.I,
)
# 仅「纯页码」或「栏目名可省略 + 页码且行尾」，避免「6绪论」被当成裸页码
_FOOTER_BARE_PAGE_RE = re.compile(
    r"^(?P<page>\d{1,3})\s*(?:CMYK|TLPC|SCGL|New.*)?$",
    re.I,
)
_FOOTER_TRAILING_PAGE_RE = re.compile(r"^(?P<page>\d{1,3})\s*$")


def _footer_scan_lines(text: str, *, max_lines: int = 8) -> list[str]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    picked: list[str] = []
    for ln in reversed(lines[-max_lines:]):
        if _is_scan_noise_line(ln):
            continue
        picked.append(ln)
    return picked


def parse_new_page_footer(text: str) -> dict | None:
    """
    解析预览 PDF 页脚：目录印刷页码 + 单元/课时。
    例：「172 第七单元 能源的合理利用与开发」「课题1 我们周围的空气31」。
    """
    for ln in _footer_scan_lines(text):
        compact = _norm(ln)
        m = _FOOTER_UNIT_PAGE_RE.search(ln) or _FOOTER_UNIT_PAGE_COMPACT_RE.search(compact)
        if m:
            page = int(m.group("page"))
            unit_no = m.group(2)
            title = (m.group("title") or "").strip(" ·")
            return {
                "logical_page": page,
                "unit_no": unit_no,
                "title": title,
                "label": f"第{unit_no}单元 {title}".strip(),
                "raw": ln,
            }
        m = _FOOTER_LESSON_PAGE_RE.search(ln)
        if m:
            page = int(m.group("page"))
            label = re.sub(r"\d{1,3}\s*(?:CMYK|TLPC|SCGL|New.*)?$", "", ln, flags=re.I).strip()
            return {
                "logical_page": page,
                "unit_no": None,
                "title": label or None,
                "label": label or f"p{page}",
                "raw": ln,
            }
        m = _FOOTER_NAMED_SECTION_PAGE_RE.match(ln) or _FOOTER_NAMED_SECTION_PAGE_RE.match(
            compact
        )
        if m:
            page = int(m.group("page"))
            section = str(m.group("section") or "").strip()
            return {
                "logical_page": page,
                "unit_no": None,
                "title": section or None,
                "label": section or f"p{page}",
                "raw": ln,
            }
        m = _FOOTER_LEADING_PAGE_SECTION_RE.match(ln) or _FOOTER_LEADING_PAGE_SECTION_RE.match(
            compact
        )
        if m:
            page = int(m.group("page"))
            label = re.sub(r"\s+", " ", (m.group("label") or "")).strip(" ·")
            return {
                "logical_page": page,
                "unit_no": None,
                "title": label or None,
                "label": label or f"p{page}",
                "raw": ln,
            }
        m = _FOOTER_BARE_PAGE_RE.match(ln) or _FOOTER_BARE_PAGE_RE.match(compact)
        if m:
            page = int(m.group("page"))
            return {
                "logical_page": page,
                "unit_no": None,
                "title": None,
                "label": f"p{page}",
                "raw": ln,
            }
        m = _FOOTER_TRAILING_PAGE_RE.match(ln)
        if m:
            page = int(m.group("page"))
            if page >= 8:
                return {
                    "logical_page": page,
                    "unit_no": None,
                    "title": None,
                    "label": f"p{page}",
                    "raw": ln,
                }
    return None


def _old_pdf_page_from_logical(
    logical_page: int,
    *,
    offset: int,
    old_page_count: int,
) -> int:
    return min(max(1, int(logical_page) + int(offset)), old_page_count)


def _resolve_old_page_from_footer(
    text: str,
    old_texts: list[str],
    *,
    offset: int,
) -> tuple[int, str, float, str] | None:
    footer = parse_new_page_footer(text)
    if not footer:
        return None
    logical_page = int(footer["logical_page"])
    if logical_page < 1 or logical_page > 260:
        return None
    old_p = _old_pdf_page_from_logical(logical_page, offset=offset, old_page_count=len(old_texts))
    title = str(footer.get("label") or f"目录 p{logical_page}")
    sim = _similarity(text, old_texts[old_p - 1]) if 1 <= old_p <= len(old_texts) else 0.0
    return old_p, title, sim, "footer"


def _similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _toc_rows_from_volume(old_vol: Volume, old_pdf: Path) -> list[dict]:
    blob = old_vol.blob_id and FileBlob.query.get(old_vol.blob_id)
    content_hash = blob.content_hash if blob else None
    entries = get_llm_toc_entries(old_pdf, content_hash=content_hash) or []
    best: dict[str, dict] = {}
    for e in entries:
        mk = e.match_key or ""
        if not mk:
            continue
        row = {
            "unit_norm": e.unit_norm,
            "title_raw": e.title_raw,
            "page_1": e.page_1,
            "match_key": mk,
        }
        prev = best.get(mk)
        if prev is None or int(row["page_1"]) < int(prev["page_1"]):
            best[mk] = row
    return list(best.values())


def _old_offset(old_vol: Volume, old_pdf: Path) -> int:
    blob = old_vol.blob_id and FileBlob.query.get(old_vol.blob_id)
    content_hash = blob.content_hash if blob else None
    cached = get_cached_catalog_pdf_offset(old_pdf, content_hash=content_hash)
    if cached is not None:
        return int(cached.offset_default)
    return 7


def _is_front_matter_page(text: str) -> bool:
    """预览 PDF 前几页的说明/栏目介绍，不是正文。"""
    head = (text or "")[:400]
    strong_markers = (
        "本书主要栏目",
        "主要栏目及说明",
        "栏目及说明",
        "致同学",
        "编者的话",
        "使用说明",
        "图书在版编目",
    )
    if any(m in head for m in strong_markers):
        return True
    # 栏目说明页：多栏名并列且几乎无正文/题号（避免正文页边栏误伤）
    column_hits = sum(
        1 for m in ("资料卡片", "方法导引", "跨学科实践活动", "探究与实践")
        if m in text
    )
    if column_hits >= 3 and len(re.findall(r"^\s*\d+\.", text, re.M)) < 2:
        if len(re.findall(r"课题\s*\d", text)) == 0:
            return True
    return False


def _is_toc_page(text: str) -> bool:
    hits = re.findall(r"课题\s*\d[^\n]{0,40}\d{1,3}\s*$", text, re.M)
    if len(hits) >= 3:
        return True
    unit_lines = len(re.findall(r"第[一二三四五六七八九十\d]+单元", text))
    lesson_lines = len(re.findall(r"课题\s*\d", text))
    return unit_lines >= 2 and lesson_lines >= 3


def _page_title_line(text: str) -> str | None:
    """取页内最像正文标题的首行（非页眉噪声）。"""
    if _is_front_matter_page(text):
        for label in (
            "本书主要栏目及说明",
            "本书主要栏目",
            "主要栏目及说明",
            "致同学",
            "编者的话",
            "使用说明",
        ):
            if label in (text or ""):
                return label
        return "前言/栏目说明"
    skip_frag = ("Time:", "pdf_", "CMYK", "人民教育", "出版社", "义务教育")
    for raw in (text or "").split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 4 or len(line) > 48:
            continue
        if any(s in line for s in skip_frag):
            continue
        if re.match(r"^[\d_./:\s-]+$", line):
            continue
        if re.match(r"^课题\s*\d", line):
            return line
        if line in ("复习与提高", "整理与提升", "探究"):
            return line
        if re.match(r"^第[一二三四五六七八九十\d]+单元", line):
            return line
        if re.match(r"^实验活动\s*\d", line):
            return line
        if re.match(r"^跨学科", line):
            return line
        if re.match(r"^绪论", line):
            return line
    return None


def _match_key_from_text(text: str) -> str | None:
    """仅从页标题/显式章节行推断，避免栏目说明页里的词误匹配。"""
    if _is_front_matter_page(text):
        return None
    title = _page_title_line(text) or ""
    head = (text or "")[:600]
    unit_m = re.search(r"第([一二三四五六七八九十\d]+)单元", head)
    unit_no = unit_m.group(1) if unit_m else None

    if re.search(r"^复习与提高", title) or (
        title == "复习与提高" or re.search(r"\n复习与提高\s*\n", head)
    ):
        if unit_no:
            return f"unit{unit_no}_复习与提高"
        return None  # 无单元上下文时不猜第一单元
    if re.search(r"^整理与提升", title) or title == "整理与提升":
        if unit_no:
            return f"unit{unit_no}_整理与提升"
        return None

    patterns = [
        (r"^课题\s*1\s*我们周围的空气|课题1\s*我们周围的空气", "课题1我们周围的空气"),
        (r"^课题\s*2\s*氧气|课题2\s*氧气", "课题2氧气"),
        (r"^课题\s*3\s*制取氧气|课题3\s*制取氧气", "课题3制取氧气"),
        (r"^课题\s*1\s*分子和原子|课题1\s*分子和原子", "课题1分子和原子"),
        (r"^课题\s*2\s*原子结构|课题2\s*原子结构", "课题2原子结构"),
        (r"^课题\s*3\s*元素|课题3\s*元素", "课题3元素"),
        (r"^课题\s*1\s*水资源|课题1\s*水资源", "课题1水资源及其利用"),
        (r"^课题\s*2\s*水的组成|课题2\s*水的组成", "课题2水的组成"),
        (r"^课题\s*3\s*物质组成的表示", "课题3物质组成的表示"),
        (r"^课题\s*1\s*质量守恒|课题1\s*质量守恒", "课题1质量守恒定律"),
        (r"^课题\s*2\s*化学方程式", "课题2化学方程式"),
        (r"^课题\s*1\s*碳单质|课题1\s*碳单质", "课题1碳单质的多样性"),
        (r"^课题\s*2\s*碳的氧化物", "课题2碳的氧化物"),
        (r"^课题\s*3\s*二氧化碳", "课题3二氧化碳的实验室制取"),
        (r"^课题\s*1\s*燃料的燃烧|课题1\s*燃料", "课题1燃料的燃烧"),
        (r"^课题\s*2\s*化石能源", "课题2化石能源的合理利用"),
        (r"^实验活动\s*1", "实验活动1"),
        (r"^实验活动\s*2", "实验活动2"),
        (r"^实验活动\s*3", "实验活动3"),
        (r"^实验活动\s*4", "实验活动4"),
        (r"^跨学科实践活动\s*1|跨学科\s*1", "跨学科1"),
        (r"^跨学科实践活动\s*2|跨学科\s*2", "跨学科2"),
        (r"^跨学科实践活动\s*3|跨学科\s*3", "跨学科3"),
        (r"^跨学科实践活动\s*4|跨学科\s*4", "跨学科4"),
        (r"^跨学科实践活动\s*5|跨学科\s*5", "跨学科5"),
        (r"^跨学科实践活动\s*6|跨学科\s*6", "跨学科6"),
        (r"^绪论", "绪论"),
        (r"^探究", "探究"),
    ]
    probe = f"{title}\n{head}"
    for pat, mk in patterns:
        if re.search(pat, probe, re.M):
            if mk == "绪论":
                return "绪论"
            return mk
    return None


def _old_page_for_key(key: str, toc: list[dict], offset: int) -> tuple[int, str] | None:
    if key == "绪论":
        cands = [e for e in toc if "绪论" in e.get("unit_norm", "")]
        if cands:
            e = min(cands, key=lambda x: int(x["page_1"]))
            return int(e["page_1"]) + offset, e["title_raw"]
        return 1 + offset, "绪论"

    if key.startswith("unit") and ("复习" in key or "整理" in key):
        unit_map = {
            "一": "第一单元", "二": "第二单元", "三": "第三单元", "四": "第四单元",
            "五": "第五单元", "六": "第六单元", "七": "第七单元",
            "1": "第一单元", "2": "第二单元", "3": "第三单元", "4": "第四单元",
            "5": "第五单元", "6": "第六单元", "7": "第七单元",
        }
        u = re.search(r"unit([一二三四五六七八九十\d]+)_", key)
        if not u:
            return None
        unit_prefix = unit_map.get(u.group(1), "")
        kind = "复习与提高" if "复习" in key else "整理与提升"
        cands = [
            e for e in toc
            if kind in e.get("title_raw", "")
            and unit_prefix in e.get("unit_norm", "")
        ]
        if cands:
            e = min(cands, key=lambda x: int(x["page_1"]))
            return int(e["page_1"]) + offset, e["title_raw"]
        return None

    cands = [e for e in toc if e.get("match_key") == key]
    if not cands:
        return None
    e = min(cands, key=lambda x: int(x["page_1"]))
    return int(e["page_1"]) + offset, e["title_raw"]


def _body_start_index(old_texts: list[str], *, scan: int = 25) -> int:
    """旧书正文起始页（跳过目录区）。"""
    for i in range(min(scan, len(old_texts))):
        t = old_texts[i] or ""
        if "目录" in t[:80] and re.search(r"课题\s*\d", t):
            continue
        if re.search(r"^课题\s*\d|^第[一二三四五六七八九十\d]+单元", t, re.M):
            return i
        if "化学使世界变得更加绚丽多彩" in t:
            return i
    return min(14, len(old_texts) - 1)


def _find_best_old_page_by_content(
    text: str,
    old_texts: list[str],
    *,
    body_start: int,
    min_sim: float = 0.22,
    allow_weak: bool = False,
) -> tuple[int, float] | None:
    """在旧书正文区按全文相似度找最像的一页。"""
    best_i, best_s = -1, 0.0
    for i in range(body_start, len(old_texts)):
        s = _similarity(text, old_texts[i])
        if s > best_s:
            best_s, best_i = s, i
    if best_i < 0:
        return None
    if best_s >= min_sim or (allow_weak and best_s > 0.03):
        return best_i + 1, best_s
    return None


def _guess_old_page_by_position(
    new_page: int,
    *,
    offset: int,
    old_page_count: int,
) -> int:
    """修订版按页序粗估旧书 PDF 页（封面/前言后正文顺延）。"""
    if new_page <= 1:
        return 1
    estimated = new_page + offset - 1
    return min(max(1, estimated), old_page_count)


def _resolve_old_page(
    text: str,
    old_texts: list[str],
    toc: list[dict],
    offset: int,
    body_start: int,
    *,
    new_page: int,
) -> tuple[int, str, float, str]:
    """
    综合页脚目录页码 + 目录键 + 正文相似度 + 校验，返回 (old_page, title, similarity, method)。
    """
    footer_hit = _resolve_old_page_from_footer(text, old_texts, offset=offset)
    if footer_hit:
        return footer_hit

    key = _match_key_from_text(text)
    toc_hit: tuple[int, str] | None = None
    if key:
        toc_hit = _old_page_for_key(key, toc, offset)
    elif not _is_front_matter_page(text):
        unit_m = re.search(r"第([一二三四五六七八九十\d]+)单元[^\n]{0,24}", text[:800])
        if unit_m:
            unit_prefix = unit_m.group(0).split()[0]
            starts = [
                int(e["page_1"]) + offset
                for e in toc
                if unit_prefix.replace(" ", "") in _norm(e.get("unit_norm", ""))
            ]
            if starts:
                old_p = min(starts)
                toc_hit = (old_p, f"{unit_m.group(0)}（单元起始）")

    if toc_hit:
        old_p, title = toc_hit
        if 1 <= old_p <= len(old_texts):
            sim = _similarity(text, old_texts[old_p - 1])
            if sim >= 0.18:
                return old_p, title, sim, "toc"
            # 目录键不可信，改走全文搜索
            _log.debug("预览页目录映射相似度过低 p%d sim=%.2f key=%s", old_p, sim, key)

    content_hit = _find_best_old_page_by_content(text, old_texts, body_start=body_start)
    if content_hit:
        old_p, sim = content_hit
        title = _page_title_line(text) or "正文近似匹配"
        return old_p, title, sim, "content"

    weak_hit = _find_best_old_page_by_content(
        text, old_texts, body_start=body_start, min_sim=0.08, allow_weak=True
    )
    if weak_hit:
        old_p, sim = weak_hit
        title = _page_title_line(text) or "正文弱匹配"
        return old_p, title, sim, "content_weak"

    old_p = _guess_old_page_by_position(new_page, offset=offset, old_page_count=len(old_texts))
    title = _page_title_line(text) or f"按页序估计 · 旧书 p{old_p}"
    sim = _similarity(text, old_texts[old_p - 1]) if 1 <= old_p <= len(old_texts) else 0.0
    return old_p, title, sim, "position"


def _change_level(sim: float) -> str:
    if sim >= 0.88:
        return "基本一致"
    if sim >= 0.70:
        return "局部改写"
    if sim >= 0.45:
        return "明显修改"
    return "大幅重写"


def change_summary_text(change: str) -> str:
    """供前端展示的变化解读。"""
    return {
        "基本一致": "版式或扫描差异为主，知识点与表述大体沿用旧版。",
        "局部改写": "同一主题下题目、表述或板块有调整，框架仍相近。",
        "明显修改": "同一位置内容有较明显改写，建议人工核对增删。",
        "大幅重写": "与旧书对应页文本差异很大，可能重写、换题或仅为近似页。",
    }.get(change, "")


def _skip_summary(kind: str, note: str | None) -> str:
    if kind == "封面":
        return "封面，不参与正文对比。"
    if kind == "前言说明":
        return "新课标预览的栏目说明，旧书无同页对照。"
    if kind == "新目录页":
        return "新课标目录页，页码体系与旧书不同。"
    return note or "不参与正文对比。"


def enrich_preview_result(result: dict) -> dict:
    """为每页补充 change_summary / change_detail，并生成总览 summary。"""
    pairs = result.get("pairs") or []
    for p in pairs:
        if p.get("comparable") and p.get("change"):
            p["change_summary"] = p.get("change_detail") or change_summary_text(str(p["change"]))
        elif not p.get("comparable"):
            p["change_summary"] = _skip_summary(
                str(p.get("kind") or ""),
                p.get("note"),
            )
        else:
            p["change_summary"] = p.get("note") or ""

    comparable = [p for p in pairs if p.get("comparable")]
    by_change: dict[str, int] = {}
    for p in comparable:
        ch = str(p.get("change") or "未知")
        by_change[ch] = by_change.get(ch, 0) + 1

    focus = sorted(
        [
            {
                "new_page": p["new_page"],
                "old_page": p.get("old_page"),
                "label": p.get("label"),
                "change": p.get("change"),
                "similarity": p.get("similarity"),
                "text_similarity": p.get("text_similarity"),
                "image_similarity": p.get("image_similarity"),
                "change_summary": p.get("change_summary"),
                "text_diff": p.get("text_diff") or [],
            }
            for p in comparable
            if (p.get("similarity") or 1) < 0.75
        ],
        key=lambda x: x.get("similarity") or 0,
    )

    result["summary"] = {
        "comparable_count": len(comparable),
        "skipped_count": sum(1 for p in pairs if not p.get("comparable")),
        "by_change": by_change,
        "focus": focus[:10],
    }
    return result


_CACHE_VERSION = 16


def _old_texts_cache_path(old_vol: Volume) -> Path | None:
    ob = old_vol.blob_id and FileBlob.query.get(old_vol.blob_id)
    if not ob:
        return None
    return (
        base_data_dir()
        / "cache"
        / "diff_preview"
        / f"old_texts_{ob.content_hash[:24]}.json"
    )


def _load_or_ocr_old_texts(
    old_vol: Volume,
    old_doc,
    ocr,
    *,
    force_refresh: bool = False,
) -> list[str]:
    """旧书全文 OCR 结果落盘，避免每次重新粗分都扫整册。"""
    cache_path = _old_texts_cache_path(old_vol)
    page_count = len(old_doc)
    if cache_path and cache_path.is_file() and not force_refresh:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            texts = payload.get("texts")
            if isinstance(texts, list) and len(texts) == page_count:
                return [str(t or "") for t in texts]
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    texts = [_ocr_page(old_doc, i, ocr, zoom=1.0) for i in range(page_count)]
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"texts": texts}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            _log.warning("旧书 OCR 缓存写入失败：%s", exc)
    return texts


def _attach_preview_urls(
    pairs: list[dict],
    *,
    old_vol: Volume,
    new_vol: Volume,
    offset: int,
    old_page_count: int,
    new_pdf_source: PdfSource,
    preview_blob_id: str | None,
    include_lesson_meta: bool = True,
) -> None:
    old_code = old_vol.volume_code
    new_code = new_vol.volume_code
    new_src_q = "?source=draft"
    if preview_blob_id:
        from ..volume_draft_pdfs import resolve_draft_blob_id

        bid = resolve_draft_blob_id(new_vol, preview_blob_id)
        new_src_q = f"?source=draft&preview_blob_id={bid}"
    elif new_pdf_source == "draft":
        new_src_q = "?source=draft"
    else:
        new_src_q = ""

    for p in pairs:
        np = p.get("new_page")
        if np:
            p["new_image_url"] = (
                f"/api/textbook-diff/volumes/{new_code}/pdf-page/{np}.png{new_src_q}"
            )
        op = p.get("old_page")
        kind = str(p.get("kind") or "")
        if not op and np and kind == "封面":
            op = 1
            p["old_page"] = op
        if op:
            p["old_image_url"] = f"/api/textbook-diff/volumes/{old_code}/pdf-page/{op}.png"
        if not (op and np and p.get("comparable")):
            continue
        if include_lesson_meta:
            meta = lesson_at_pdf_page(old_vol, int(op))
            if meta:
                p["old_lesson_meta"] = meta
                p["old_lesson_label"] = (
                    f"{meta.get('unit_title') or ''} · {meta.get('lesson_name') or ''}"
                ).strip(" ·")
        draft_q = "&new_pdf_source=draft" if new_pdf_source == "draft" else ""
        blob_q = ""
        if preview_blob_id and new_pdf_source == "draft":
            from ..volume_draft_pdfs import resolve_draft_blob_id

            blob_q = f"&preview_blob_id={resolve_draft_blob_id(new_vol, preview_blob_id)}"
        p["compare_url"] = (
            f"/textbook-diff/view?old_code={old_code}&new_code={new_code}"
            f"&mode=page&old_page={op}&new_page={np}{draft_q}{blob_q}"
        )


def _save_preview_cache(cache_file: Path | None, result: dict) -> None:
    if not cache_file:
        return
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        _log.warning("预览对比缓存写入失败：%s", exc)


def _build_fast_preview_pairs(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_pdf: Path,
    new_pdf: Path,
    new_pdf_source: PdfSource,
    preview_blob_id: str | None,
    draft_blob_id: str | None = None,
) -> dict:
    """页脚目录页码粗分：OCR 页脚区映射到旧书 PDF 页，秒级返回。"""
    import fitz
    from rapidocr_onnxruntime import RapidOCR

    offset = _old_offset(old_vol, old_pdf)
    ocr = RapidOCR()
    with fitz.open(str(old_pdf)) as old_doc, fitz.open(str(new_pdf)) as new_doc:
        old_page_count = len(old_doc)
        new_pdf_pages = len(new_doc)

        pairs: list[dict] = []
        idx = 0
        for new_page in range(1, new_pdf_pages + 1):
            page_index = new_page - 1
            text = (new_doc.load_page(page_index).get_text() or "").strip()
            if new_page <= 4 and len(text) < 80:
                text = _ocr_page(new_doc, page_index, ocr, zoom=1.2)
            footer_text = _ocr_page_footer(new_doc, page_index, ocr)
            if footer_text:
                text = f"{text}\n{footer_text}" if text else footer_text

            if new_page == 1:
                pairs.append(
                    {
                        "index": 0,
                        "comparable": True,
                        "kind": "封面",
                        "new_page": new_page,
                        "old_page": 1,
                        "label": "封面",
                        "match_method": "position",
                        "note": "封面按旧书第 1 页对照",
                    }
                )
                continue
            if _is_front_matter_page(text):
                title = _page_title_line(text) or "本书主要栏目及说明"
                pairs.append(
                    {
                        "index": 0,
                        "comparable": False,
                        "kind": "前言说明",
                        "new_page": new_page,
                        "label": title,
                        "note": "出版社预览的前言/栏目说明，不与旧书正文页强行对照",
                    }
                )
                continue
            if _is_toc_page(text):
                pairs.append(
                    {
                        "index": 0,
                        "comparable": False,
                        "kind": "新目录页",
                        "new_page": new_page,
                        "label": "新课标目录",
                        "note": "目录页，可左右翻看版式差异",
                    }
                )
                continue

            idx += 1
            footer = parse_new_page_footer(text)
            if footer:
                old_p = _old_pdf_page_from_logical(
                    footer["logical_page"], offset=offset, old_page_count=old_page_count
                )
                title = str(footer.get("label") or f"目录 p{footer['logical_page']}")
                method = "footer"
                note = f"页脚目录 p{footer['logical_page']} → 旧书 PDF p{old_p}"
            else:
                old_p = _guess_old_page_by_position(
                    new_page, offset=offset, old_page_count=old_page_count
                )
                title = f"预览第 {new_page} 页"
                method = "position"
                note = "未识别页脚页码，暂按页序估计；点「重新粗分」可 OCR 全书精确对齐"

            pairs.append(
                {
                    "index": idx,
                    "comparable": True,
                    "kind": "正文抽样",
                    "new_page": new_page,
                    "old_page": old_p,
                    "label": title,
                    "match_method": method,
                    "similarity": 0.0,
                    "change": "未知",
                    "note": note,
                }
            )

    _attach_preview_urls(
        pairs,
        old_vol=old_vol,
        new_vol=new_vol,
        offset=offset,
        old_page_count=old_page_count,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        include_lesson_meta=True,
    )
    return enrich_preview_result(
        {
            "mode": "preview",
            "build_tier": "fast",
            "draft_blob_id": draft_blob_id,
            "old_code": old_vol.volume_code,
            "new_code": new_vol.volume_code,
            "new_pdf_pages": new_pdf_pages,
            "old_offset": offset,
            "pairs": pairs,
            "comparable_count": sum(1 for p in pairs if p.get("comparable")),
        }
    )


def _cache_path(
    old_vol: Volume,
    new_vol: Volume,
    *,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> Path | None:
    ob = old_vol.blob_id and FileBlob.query.get(old_vol.blob_id)
    if not ob:
        return None
    if new_pdf_source == "draft":
        from ..volume_draft_pdfs import resolve_draft_blob_id

        try:
            nb_id = resolve_draft_blob_id(new_vol, preview_blob_id)
        except ValueError:
            return None
    else:
        nb_id = new_vol.blob_id
    nb = nb_id and FileBlob.query.get(nb_id)
    if not nb:
        return None
    src_tag = "draft" if new_pdf_source == "draft" else "full"
    key = f"v{_CACHE_VERSION}_{src_tag}_{ob.content_hash[:16]}_{nb.content_hash[:16]}"
    return base_data_dir() / "cache" / "diff_preview" / f"{key}.json"


def build_preview_compare_pairs(
    *,
    old_vol: Volume,
    new_vol: Volume,
    force_refresh: bool = False,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> dict:
    """
    扫描未定稿预览 PDF 各页，映射到旧教材 PDF 页并估算改动程度。
    结果带 PDF 页图 URL，供对比页并排展示。
    """
    if not old_vol.blob_id:
        raise ValueError("请先为旧教材册次上传完整版 PDF")
    if new_pdf_source == "draft":
        from ..volume_draft_pdfs import resolve_draft_blob_id

        try:
            resolved_draft_blob_id = resolve_draft_blob_id(new_vol, preview_blob_id)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    elif not new_vol.blob_id:
        raise ValueError("请先为新教材册次上传完整版 PDF")
    else:
        resolved_draft_blob_id = None

    cache_file = _cache_path(
        old_vol,
        new_vol,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    if cache_file and cache_file.is_file() and not force_refresh:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("pairs"):
                if new_pdf_source != "draft" or cached.get("draft_blob_id") == resolved_draft_blob_id:
                    return enrich_preview_result(cached)
        except (OSError, json.JSONDecodeError):
            pass

    old_pdf = _pdf_path_for_volume(old_vol)
    new_pdf = _resolve_new_pdf_path(
        new_vol,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    toc = _toc_rows_from_volume(old_vol, old_pdf)
    if not force_refresh:
        result = _build_fast_preview_pairs(
            old_vol=old_vol,
            new_vol=new_vol,
            old_pdf=old_pdf,
            new_pdf=new_pdf,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            draft_blob_id=resolved_draft_blob_id,
        )
        _save_preview_cache(cache_file, result)
        return result

    if not toc:
        raise ValueError("旧教材尚无目录页码缓存，请先在旧教材侧完成「识别目录」")

    import fitz
    from rapidocr_onnxruntime import RapidOCR

    offset = _old_offset(old_vol, old_pdf)
    ocr = RapidOCR()
    old_doc = fitz.open(str(old_pdf))
    new_doc = fitz.open(str(new_pdf))
    new_pdf_pages = len(new_doc)
    try:
        old_texts = _load_or_ocr_old_texts(
            old_vol, old_doc, ocr, force_refresh=force_refresh
        )
        body_start = _body_start_index(old_texts)
        pairs: list[dict] = []
        idx = 0
        for pi in range(len(new_doc)):
            text = _ocr_page(new_doc, pi, ocr)
            new_page = pi + 1
            title_line = _page_title_line(text)
            if pi == 0:
                pairs.append(
                    {
                        "index": 0,
                        "comparable": False,
                        "kind": "封面",
                        "new_page": new_page,
                        "label": "封面",
                        "note": "封面不参与正文对比",
                    }
                )
                continue
            if _is_front_matter_page(text):
                pairs.append(
                    {
                        "index": 0,
                        "comparable": False,
                        "kind": "前言说明",
                        "new_page": new_page,
                        "label": title_line or "本书主要栏目及说明",
                        "note": "出版社预览的前言/栏目说明，不与旧书正文页强行对照",
                    }
                )
                continue
            if _is_toc_page(text):
                pairs.append(
                    {
                        "index": 0,
                        "comparable": False,
                        "kind": "新目录页",
                        "new_page": new_page,
                        "label": "新课标目录",
                        "note": "目录页，可左右翻看版式差异",
                    }
                )
                continue

            row: dict = {
                "comparable": False,
                "kind": "正文抽样",
                "new_page": new_page,
                "label": title_line or f"预览第 {new_page} 页",
            }

            resolved = _resolve_old_page(
                text, old_texts, toc, offset, body_start, new_page=new_page
            )
            old_p, old_title, sim, method = resolved
            idx += 1
            analysis = _analyze_page_pair(
                old_doc,
                old_p - 1,
                old_texts[old_p - 1],
                new_doc,
                pi,
                text,
            )
            row.update(
                {
                    "index": idx,
                    "comparable": True,
                    "old_page": old_p,
                    "old_lesson": old_title,
                    "match_method": method,
                    "label": title_line or old_title,
                    **analysis,
                }
            )
            footer_meta = parse_new_page_footer(text)
            if method == "footer" and footer_meta:
                row["note"] = (
                    f"页脚目录 p{footer_meta['logical_page']} → 旧书 PDF p{old_p}"
                )
            elif method in ("content_weak", "position") or analysis["similarity"] < 0.35:
                row["note"] = "自动对齐置信度较低，请人工核对"
            pairs.append(row)
    finally:
        old_doc.close()
        new_doc.close()

    _attach_preview_urls(
        pairs,
        old_vol=old_vol,
        new_vol=new_vol,
        offset=offset,
        old_page_count=len(old_texts),
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )

    result = enrich_preview_result(
        {
            "mode": "preview",
            "build_tier": "full",
            "draft_blob_id": resolved_draft_blob_id,
            "old_code": old_vol.volume_code,
            "new_code": new_vol.volume_code,
            "new_pdf_pages": new_pdf_pages,
            "old_offset": offset,
            "pairs": pairs,
            "comparable_count": sum(1 for p in pairs if p.get("comparable")),
        }
    )
    _save_preview_cache(cache_file, result)
    return result
