"""修订版 PDF：解析页脚印刷页码并缓存页图。"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ...extensions import db
from ...models import Volume
from ...repo_paths import repo_root
from ..volume_draft_pdfs import resolve_draft_blob_id
from ..volume_pdf import resolve_volume_pdf_path

_log = logging.getLogger(__name__)

_DRAFT_MAP_KEY = "draft_page_map"
_DRAFT_PAGES_READY_KEY = "draft_pages_ready"

_UNIT_COVER_RE = re.compile(
    r"第([一二三四五六七八九十\d]+)单元\s*[·\s]*([^\n\d]{2,40})",
)


def _suggestions(volume: Volume) -> dict[str, Any]:
    return dict(volume.parse_suggestions_json or {})


def _save_suggestions(volume: Volume, data: dict[str, Any]) -> None:
    volume.parse_suggestions_json = data or None
    db.session.flush()


def draft_page_map_cache_path(volume: Volume, *, preview_blob_id: str | None = None) -> Path:
    bid = resolve_draft_blob_id(volume, preview_blob_id)
    return (
        repo_root()
        / "data"
        / "diff-textbook"
        / "draft-page-maps"
        / f"{volume.volume_code}-{bid[:12]}.json"
    )


def draft_page_png_dir(volume: Volume, *, preview_blob_id: str | None = None) -> Path:
    bid = resolve_draft_blob_id(volume, preview_blob_id)
    return (
        repo_root()
        / "data"
        / "diff-textbook"
        / "draft-pages"
        / volume.volume_code
        / bid[:12]
    )


def get_stored_draft_page_map(volume: Volume) -> dict[str, Any] | None:
    sug = _suggestions(volume)
    stored = sug.get(_DRAFT_MAP_KEY)
    if isinstance(stored, dict) and stored.get("pages"):
        return stored
    try:
        path = draft_page_map_cache_path(volume)
    except ValueError:
        # 本册尚无修订版 PDF：详情/列表仍应可打开
        return None
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("pages"):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return None


def draft_preprocess_fields(volume: Volume) -> dict[str, Any]:
    """写入 volume detail 的修订版预处理状态。"""
    try:
        page_map = get_stored_draft_page_map(volume)
    except ValueError:
        page_map = None
    sug = _suggestions(volume)
    ready = bool(sug.get(_DRAFT_PAGES_READY_KEY))
    return {
        "draft_page_map": page_map,
        "draft_page_mapped": int((page_map or {}).get("mapped") or 0),
        "draft_page_count": int((page_map or {}).get("page_count") or 0),
        "draft_pages_ready": ready,
        "draft_pages_dir": sug.get("draft_pages_dir"),
        "draft_pages_count": sug.get("draft_pages_count") or 0,
    }


def _score_unit_title(s: str) -> tuple:
    """越高越像单元总标题（非正文段、非课题短名）。"""
    has_stop = any(c in s for c in "。；！？")
    # 正文句常见「中/着/了/伴随着」；单元题多为名词短语
    prose_like = bool(re.search(r"着|了|伴随着|中常|同时", s))
    title_len = 6 <= len(s) <= 18
    compound = bool(re.search(r"[的与和]", s))
    topical = 1 if re.search(r"利用|开发|组成|世界|资源|空气|氧气", s) else 0
    return (
        0 if has_stop else 1,
        0 if prose_like else 1,
        1 if title_len else 0,
        topical,
        1 if compound else 0,
        len(s) if 6 <= len(s) <= 18 else 0,
    )


def _pick_unit_title_candidates(lines: list[str]) -> str | None:
    """OCR 行序可能乱：在候选里选更像单元总标题的一行。"""
    cands: list[str] = []
    for nxt in lines[:8]:
        title = re.sub(r"\s+", " ", nxt).strip(" ·•●·")
        if not title or title.startswith(("•", "·", "●")):
            continue
        if len(title) < 4 or len(title) > 40:
            continue
        if re.match(r"^第[一二三四五六七八九十\d]+单元", title):
            continue
        if re.match(r"^课题\s*\d", title):
            continue
        cands.append(title)
    if not cands:
        return None
    cands.sort(key=_score_unit_title, reverse=True)
    return cands[0]


def _detect_unit_cover_label(text: str) -> str | None:
    """无印刷页时，从正文识别单元扉页标题。"""
    from .preview_compare import _is_front_matter_page, _is_toc_page

    if _is_front_matter_page(text) or _is_toc_page(text):
        return None
    head = (text or "")[:1000]
    lines = [re.sub(r"\s+", " ", raw).strip() for raw in head.splitlines()]
    lines = [ln for ln in lines if ln]
    for idx, line in enumerate(lines):
        if re.match(r"^\d{1,3}\s*第", line):
            continue
        m = re.match(r"^第([一二三四五六七八九十\d]+)单元\s*$", line)
        if m:
            unit = f"第{m.group(1)}单元"
            title = _pick_unit_title_candidates(lines[idx + 1 :])
            return f"{unit} {title}".strip() if title else unit
        m = _UNIT_COVER_RE.match(line)
        if m:
            unit = f"第{m.group(1)}单元"
            inline = re.sub(r"\s+", " ", m.group(2)).strip(" ·•●")
            title = _pick_unit_title_candidates([inline, *lines[idx + 1 :]])
            return f"{unit} {title or inline}".strip()
    return None


def _detect_lesson_cover_label(text: str) -> str | None:
    from .preview_compare import _is_front_matter_page, _page_title_line

    if _is_front_matter_page(text):
        return None
    title = _page_title_line(text)
    if title and re.match(r"^(课题\s*\d|绪论|实验活动\s*\d|跨学科)", title):
        return title
    return None


def _footer_semantic_label(footer: dict[str, Any] | None) -> str | None:
    if not footer:
        return None
    label = str(footer.get("label") or "").strip()
    if not label:
        return None
    if re.fullmatch(r"p\d{1,3}", label):
        return None
    if label.startswith("目录"):
        return None
    return label


def _infer_semantic(
    *,
    pdf_page: int,
    text: str,
    footer: dict[str, Any] | None,
) -> tuple[str, str, str]:
    """
    语义轨：kind / label / semantic_track。
    与页码轨独立；无页码时才用 structure（封面/单元页）。
    """
    from .preview_compare import (
        _is_front_matter_page,
        _is_toc_page,
        _page_title_line,
    )

    if pdf_page == 1:
        return "封面", "封面", "structure"

    if _is_toc_page(text):
        return "目录", "目录", "body"

    if _is_front_matter_page(text):
        return "前言说明", _page_title_line(text) or "前言/栏目说明", "body"

    has_page = bool(footer and footer.get("logical_page") is not None)
    unit_label = _detect_unit_cover_label(text)
    # 无印刷页的单元扉页：结构页（对标新库常删的单元页）
    if unit_label and not has_page:
        return "单元页", unit_label, "structure"

    foot_label = _footer_semantic_label(footer)
    if foot_label:
        return "正文", foot_label, "footer"

    lesson = _detect_lesson_cover_label(text)
    if lesson:
        return ("正文" if has_page else "课题页"), lesson, "body"

    title = _page_title_line(text)
    if title and title in ("复习与提高", "整理与提升", "练习与应用", "探究与实践"):
        return "正文", title, "body"

    if has_page:
        return "正文", f"p{int(footer['logical_page'])}", "page"

    return "未识别", f"修订 PDF 第 {pdf_page} 页", "none"


def classify_draft_page(*, pdf_page: int, text: str) -> dict[str, Any]:
    """
    修订版单页双轨分类：
    - 页码轨：logical_page / mapped / page_track（有页脚页码必须吃到）
    - 语义轨：kind / label / semantic_track（页眉页脚课时/单元，或结构页）
    """
    from .preview_compare import parse_new_page_footer

    footer = parse_new_page_footer(text) if pdf_page > 1 else None
    logical = int(footer["logical_page"]) if footer and footer.get("logical_page") is not None else None
    mapped = logical is not None
    page_track = "footer" if mapped else None

    kind, label, semantic_track = _infer_semantic(
        pdf_page=pdf_page, text=text, footer=footer
    )

    row: dict[str, Any] = {
        "pdf_page": pdf_page,
        "logical_page": logical,
        "mapped": mapped,
        "page_track": page_track,
        "kind": kind,
        "label": label,
        "semantic_track": semantic_track,
    }
    if footer and footer.get("raw"):
        row["raw"] = footer.get("raw")
    return row


def scan_draft_printed_pages(
    volume: Volume,
    *,
    preview_blob_id: str | None = None,
) -> dict[str, Any]:
    """
    扫描修订版每一 PDF 页的页脚印刷页码。
    返回 { preview_blob_id, page_count, mapped, pages: [{pdf_page, logical_page, label, kind}] }
    """
    import fitz

    from .preview_compare import _ocr_page, _ocr_page_footer

    pdf_path = resolve_volume_pdf_path(
        volume, source="draft", preview_blob_id=preview_blob_id
    )
    bid = resolve_draft_blob_id(volume, preview_blob_id)

    try:
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()
    except Exception as exc:
        raise ValueError(f"修订版页码解析需要 RapidOCR：{exc}") from exc

    pages: list[dict[str, Any]] = []
    with fitz.open(str(pdf_path)) as doc:
        n = len(doc)
        for pdf_page in range(1, n + 1):
            page_index = pdf_page - 1
            text = (doc.load_page(page_index).get_text() or "").strip()
            if len(text) < 120:
                text = _ocr_page(doc, page_index, ocr, zoom=1.2)
            footer_text = _ocr_page_footer(doc, page_index, ocr)
            if footer_text:
                text = f"{text}\n{footer_text}" if text else footer_text
            pages.append(classify_draft_page(pdf_page=pdf_page, text=text))

    mapped = sum(1 for p in pages if p.get("mapped"))
    result = {
        "preview_blob_id": bid,
        "page_count": len(pages),
        "mapped": mapped,
        "pages": pages,
    }

    cache = draft_page_map_cache_path(volume, preview_blob_id=bid)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    sug = _suggestions(volume)
    sug[_DRAFT_MAP_KEY] = result
    sug[_DRAFT_PAGES_READY_KEY] = False
    _save_suggestions(volume, sug)
    db.session.commit()

    _log.info(
        "修订版页码映射 %s：%d/%d 页识别到印刷页",
        volume.volume_code,
        mapped,
        len(pages),
    )
    return result


def build_draft_page_images(
    volume: Volume,
    *,
    preview_blob_id: str | None = None,
    dpi: int = 144,
) -> dict[str, Any]:
    """按修订版 PDF 物理页渲染 PNG 到磁盘（供后续粗分/对照使用）。"""
    from ...parsers.pdf_spread import render_view_page_png
    from ...parsers.pdf_render import render_pdf_page_png

    page_map = get_stored_draft_page_map(volume)
    if not page_map or not page_map.get("pages"):
        page_map = scan_draft_printed_pages(volume, preview_blob_id=preview_blob_id)

    bid = str(page_map.get("preview_blob_id") or resolve_draft_blob_id(volume, preview_blob_id))
    pdf_path = resolve_volume_pdf_path(volume, source="draft", preview_blob_id=bid)
    out_dir = draft_page_png_dir(volume, preview_blob_id=bid)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 修订版按物理单页渲染（不连续选页，不做对开裁切）
    written = 0
    for item in page_map["pages"]:
        pdf_page = int(item["pdf_page"])
        dest = out_dir / f"p{pdf_page:04d}.png"
        png = render_pdf_page_png(pdf_path, pdf_page - 1, dpi=dpi)
        dest.write_bytes(png)
        written += 1

    sug = _suggestions(volume)
    sug[_DRAFT_MAP_KEY] = page_map
    sug[_DRAFT_PAGES_READY_KEY] = True
    sug["draft_pages_dir"] = str(out_dir.relative_to(repo_root())).replace("\\", "/")
    sug["draft_pages_count"] = written
    _save_suggestions(volume, sug)
    db.session.commit()

    return {
        "ok": True,
        "written": written,
        "pages_dir": sug["draft_pages_dir"],
        "mapped": page_map.get("mapped"),
        "page_count": page_map.get("page_count"),
        "draft_pages_ready": True,
    }
