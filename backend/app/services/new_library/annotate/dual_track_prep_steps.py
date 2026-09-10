"""双轨测试页：前置 5 道工序（工序 1/2 可执行 OCR，其余只读检视）。"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ....models import LessonPage, TextbookAtom
from ..analysis_pipeline import ANALYSIS_PIPELINE_STEPS
from ..block_pipeline_prepare import get_page_prepare
from ..lesson_analysis import lesson_analysis_dict
from .content_prescan import (
    latest_content_scan_payload,
    lesson_ocr_phase_status,
)
from .dual_track_units import list_lesson_new_units
from .pair_review import build_pair_review_context, get_primary_lesson_match, pair_review_status
from .seed_from_old_page import _atom_plain_text, _new_content_block_name
from ...old_library.annotate.workspace import _is_placeholder_atom
from .doubao_old_textbook_cache import build_old_textbook_doubao_payload
from ...llm.page_text_extract import dual_track_textbook_uses_doubao
from ...llm.slide_text_extract import load_lesson_slide_text_cache, slide_text_for_index

_CW_TB_SPLIT = re.compile(r"^课件：(.+?)(?:；教材：(.+))?$|^教材：(.+)$")
_LOW_COVERAGE_CHAR_FLOOR = 80
_LOW_COVERAGE_RATIO = 0.45


def _excerpt(text: str, n: int = 100) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t[:n] + ("…" if len(t) > n else "")


def _atom_text(atom: TextbookAtom) -> str:
    return (atom.content or atom.ocr_text or "").strip()


def _text_atoms_for_lesson(lesson_id: str) -> dict[int, list[TextbookAtom]]:
    by_page: dict[int, list[TextbookAtom]] = defaultdict(list)
    rows = TextbookAtom.query.filter_by(lesson_id=lesson_id).all()
    for atom in rows:
        if _is_placeholder_atom(atom):
            continue
        kind = (atom.atom_type or "text").lower()
        if kind in ("image", "figure"):
            continue
        by_page[int(atom.page_index)].append(atom)
    return by_page


def _coverage_flag(char_count: int, text_atom_count: int, median_chars: float) -> bool:
    if text_atom_count <= 0:
        return True
    if char_count < _LOW_COVERAGE_CHAR_FLOOR:
        return True
    if median_chars > 0 and char_count < median_chars * _LOW_COVERAGE_RATIO:
        return True
    return False


def _page_atom_coverage_rows(
    lesson_id: str,
    *,
    book_label: str,
    data_source: str,
) -> list[dict[str, Any]]:
    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    by_page = _text_atoms_for_lesson(lesson_id)
    char_counts = [
        sum(len(_atom_text(a)) for a in by_page.get(int(lp.page_index), []))
        for lp in pages
    ]
    positive = [c for c in char_counts if c > 0]
    median_chars = float(sorted(positive)[len(positive) // 2]) if positive else 0.0

    rows: list[dict[str, Any]] = []
    for lp, char_count in zip(pages, char_counts):
        pi = int(lp.page_index)
        page_atoms = by_page.get(pi, [])
        text_parts = [_atom_text(a) for a in page_atoms if _atom_text(a)]
        rows.append(
            {
                "source": book_label,
                "page_index": pi,
                "data_source": data_source,
                "text_atom_count": len(page_atoms),
                "char_count": char_count,
                "ocr_items": len(lp.ocr_atoms_json or []),
                "has_ocr": bool(page_atoms),
                "low_coverage": _coverage_flag(char_count, len(page_atoms), median_chars),
                "excerpt": _excerpt(" ".join(text_parts)),
                "atom_codes_preview": [a.atom_code for a in page_atoms[:6]],
            }
        )
    return rows


def _block_cw_excerpt_for_slide(old_blocks: list, slide_index: int) -> str:
    for block in old_blocks or []:
        indices = block.course_slide_indices or []
        if hasattr(block, "course_slide_indices"):
            indices = block.course_slide_indices or []
        else:
            indices = (block.get("course_slide_indices") if isinstance(block, dict) else []) or []
        if int(slide_index) not in [int(x) for x in indices]:
            continue
        name = (
            (block.block_name if hasattr(block, "block_name") else block.get("block_name"))
            or ""
        ).strip()
        m = _CW_TB_SPLIT.match(name)
        if m:
            cw = (m.group(1) or "").strip()
            if cw:
                return cw
        if name:
            return name[:120]
    return ""


def build_old_courseware_text_rows(
    *,
    old_lesson_uid: str,
    slides: list[Any],
    old_blocks: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """旧课件逐页文字：豆包缓存 → DB ocr_text → 旧块课件名兜底。"""
    cache = load_lesson_slide_text_cache(old_lesson_uid)
    rows: list[dict[str, Any]] = []
    for slide in slides or []:
        if hasattr(slide, "slide_index"):
            idx = int(slide.slide_index)
            blob_id = getattr(slide, "blob_id", None)
            db_ocr = (getattr(slide, "ocr_text", None) or "").strip()
        else:
            idx = int(slide.get("slide_index") or 0)
            blob_id = slide.get("blob_id")
            db_ocr = (slide.get("ocr_text") or "").strip()
        cached = slide_text_for_index(old_lesson_uid, idx, cache=cache)
        text = (cached or {}).get("text") or ""
        source = (cached or {}).get("source") or ""
        if not text and db_ocr:
            text = db_ocr
            source = "db_ocr_text"
        if not text:
            text = _block_cw_excerpt_for_slide(old_blocks or [], idx)
            if text:
                source = "old_block_name"
        rows.append(
            {
                "source": "旧课件",
                "slide_index": idx,
                "blob_id": blob_id,
                "data_source": source or "—",
                "char_count": len(text),
                "line_count": (cached or {}).get("line_count") or len(
                    [ln for ln in text.splitlines() if ln.strip()]
                ),
                "has_text": bool(text),
                "low_coverage": not text,
                "text": text,
                "excerpt": _excerpt(text),
            }
        )
    return rows


def _slide_text_rows(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    return build_old_courseware_text_rows(
        old_lesson_uid=ctx["old_les"].lesson_uid,
        slides=ctx.get("slides") or [],
        old_blocks=ctx.get("old_blocks") or [],
    )


def _segment_status(ctx: dict[str, Any]) -> dict[str, str]:
    analysis = lesson_analysis_dict(
        lesson_id=ctx["new_les"].id,
        lesson_uid=ctx["new_les"].lesson_uid,
    )
    return dict(analysis.get("segments") or {})


def build_prep_text_ocr_detail(ctx: dict[str, Any]) -> dict[str, Any]:
    """工序1：文字 OCR（新教材执行；旧教材/旧课件只读旧库）。"""
    new_les = ctx["new_les"]
    old_les = ctx["old_les"]
    phase = lesson_ocr_phase_status(lesson_id=new_les.id)
    new_rows = _page_atom_coverage_rows(
        new_les.id, book_label="新教材", data_source="textbook_atoms"
    )
    old_rows = _page_atom_coverage_rows(
        old_les.id, book_label="旧教材", data_source="old_library_atoms"
    )
    slide_rows = _slide_text_rows(ctx)
    old_doubao = ctx.get("old_textbook_doubao") or build_old_textbook_doubao_payload(
        lesson_uid=old_les.lesson_uid,
        pages=ctx.get("old_pages") or [],
    )
    old_doubao_page_rows = [
        {
            "source": "旧教材·豆包",
            "page_index": r.get("page_index"),
            "data_source": r.get("data_source"),
            "text_atom_count": r.get("text_atom_count"),
            "char_count": r.get("char_count"),
            "low_coverage": r.get("low_coverage"),
            "excerpt": r.get("excerpt"),
        }
        for r in (old_doubao.get("pages") or [])
    ]
    low_pages = [r for r in new_rows + old_rows if r.get("low_coverage")]
    low_slides = [r for r in slide_rows if r.get("low_coverage")]
    low_old_doubao = [r for r in old_doubao_page_rows if r.get("low_coverage")]
    return {
        "step_key": "text_ocr",
        "step_label": "工序1 · 文字OCR",
        "segment_status": _segment_status(ctx).get("text_ocr", "pending"),
        "phase": phase,
        "summary": {
            "new_pages": len(new_rows),
            "old_pages": len(old_rows),
            "old_slides": len(slide_rows),
            "old_doubao_pages": old_doubao.get("pages_total") or 0,
            "old_doubao_atoms": old_doubao.get("atom_count") or 0,
            "old_doubao_pages_with_text": old_doubao.get("pages_with_text") or 0,
            "new_text_complete": phase.get("text_ocr_complete"),
            "new_char_total": sum(int(r.get("char_count") or 0) for r in new_rows),
            "old_char_total": sum(int(r.get("char_count") or 0) for r in old_rows),
            "slides_with_text": sum(1 for r in slide_rows if r.get("has_text")),
            "low_coverage_pages": len(low_pages),
            "low_coverage_slides": len(low_slides),
            "new_engine": (
                "doubao 视觉文字原子（bbox+content，双轨前置1）"
                if dual_track_textbook_uses_doubao()
                else "rapidocr+pdf（与 annotate 文字 OCR 同款）"
            ),
            "slide_engine": "旧库 DB/缓存（双轨只读）",
            "old_tb_engine": "旧库 atoms（双轨只读）",
        },
        "rows": new_rows,
        "old_textbook_rows": old_rows,
        "old_textbook_doubao_rows": old_doubao_page_rows,
        "old_textbook_doubao": old_doubao,
        "old_courseware_rows": slide_rows,
        "low_coverage_warnings": [
            f"{r['source']} p{r['page_index']} 仅 {r.get('text_atom_count', 0)} 框 / {r.get('char_count', 0)} 字"
            for r in low_pages[:12]
        ]
        + [
            f"旧教材·豆包 P{r['page_index']} 无文字"
            for r in low_old_doubao[:6]
        ]
        + [
            f"课件 P{r['slide_index']} 无文字（来源 {r.get('data_source', '—')}）"
            for r in low_slides[:8]
        ],
    }


def build_prep_image_ocr_detail(ctx: dict[str, Any]) -> dict[str, Any]:
    """工序2：图片 OCR（插图解析）。"""
    new_les = ctx["new_les"]
    phase = lesson_ocr_phase_status(lesson_id=new_les.id)
    atoms = ctx["new_atoms"]
    image_atoms = [a for a in atoms if (a.atom_type or "").lower() == "image"]
    rows: list[dict[str, Any]] = []
    for a in image_atoms[:80]:
        meta = a.metadata_json or {}
        label = (a.ocr_text or a.content or "").strip()
        rows.append(
            {
                "atom_code": a.atom_code,
                "page_index": int(a.page_index),
                "ocr_text": _excerpt(label, 80),
                "has_ocr": bool(label and not label.startswith("[插图]")),
                "has_bbox": bool(a.bbox_json),
                "image_role": meta.get("image_role") or meta.get("illustration_tag") or "—",
            }
        )
    by_page: dict[int, int] = {}
    for a in image_atoms:
        by_page[int(a.page_index)] = by_page.get(int(a.page_index), 0) + 1
    pages = (
        LessonPage.query.filter_by(lesson_id=new_les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    page_rows = []
    for lp in pages:
        pi = int(lp.page_index)
        n = by_page.get(pi, 0)
        page_rows.append(
            {
                "page_index": pi,
                "image_count": n,
                "low_coverage": n <= 0 and bool(lp.ocr_atoms_json),
            }
        )

    old_les = ctx["old_les"]
    old_image_atoms = [
        a
        for a in (ctx.get("old_atoms") or [])
        if not _is_placeholder_atom(a)
        and (getattr(a, "atom_type", None) or "").lower() == "image"
    ]
    old_by_page: dict[int, int] = {}
    for a in old_image_atoms:
        old_by_page[int(a.page_index)] = old_by_page.get(int(a.page_index), 0) + 1
    old_page_rows = [
        {
            "page_index": pi,
            "image_count": old_by_page.get(pi, 0),
            "low_coverage": old_by_page.get(pi, 0) <= 0,
        }
        for pi in sorted(
            {
                int(p.page_index)
                for p in (ctx.get("old_pages") or [])
                if hasattr(p, "page_index")
            }
            or old_by_page.keys()
        )
    ]

    from .dual_track_prep_run import build_old_slide_layout_payload

    old_layout = build_old_slide_layout_payload(
        str(old_les.lesson_uid),
        ctx.get("slides") or [],
    )
    old_courseware_layout_rows = [
        {
            "slide_index": int(s.get("slide_index") or 0),
            "region_count": int(s.get("region_count") or 0),
            "layout_scanned": bool(s.get("layout_scanned")),
            "low_coverage": not s.get("layout_scanned") or int(s.get("region_count") or 0) <= 0,
        }
        for s in (old_layout.get("slides") or [])
    ]

    return {
        "step_key": "image_ocr",
        "step_label": "工序2 · 图片OCR",
        "segment_status": _segment_status(ctx).get("image_ocr", "pending"),
        "phase": phase,
        "summary": {
            "image_atom_count": len(image_atoms),
            "with_ocr_text": sum(1 for a in image_atoms if (a.ocr_text or a.content or "").strip()),
            "pages_with_images": len(by_page),
            "image_ocr_complete": phase.get("image_ocr_complete"),
            "engine": "opencv+doubao_layout（与 annotate 图片 OCR 同款）",
            "old_image_atom_count": len(old_image_atoms),
            "old_slides_layout_done": int(old_layout.get("slides_layout_done") or 0),
            "old_slide_image_regions": int(old_layout.get("image_regions") or 0),
        },
        "rows": rows,
        "page_counts": page_rows,
        "old_page_counts": old_page_rows,
        "old_courseware_layout_rows": old_courseware_layout_rows,
    }


def build_prep_block_match_detail(ctx: dict[str, Any]) -> dict[str, Any]:
    """工序3：栏目聚类 → unit_id（新教材侧，不含 old_mirror 现网块名）。"""
    new_les = ctx["new_les"]
    lesson_uid = new_les.lesson_uid
    atoms_by_code = ctx.get("new_atoms_by_code") or {
        a.atom_code: a for a in (ctx.get("new_atoms") or [])
    }
    pages = (
        LessonPage.query.filter_by(lesson_id=new_les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    unit_rows: list[dict[str, Any]] = []
    section_rows: list[dict[str, Any]] = []
    for lp in pages:
        pi = int(lp.page_index)
        prep = get_page_prepare(lesson_uid, pi)
        if prep:
            for sec in prep.get("sections") or []:
                section_rows.append(
                    {
                        "page_index": pi,
                        "section_name": sec.get("section_name") or "—",
                        "source": sec.get("source") or prep.get("column_source") or "rules",
                    }
                )
            for cl in prep.get("clusters") or []:
                codes = [
                    str(c).strip()
                    for c in (cl.get("atom_codes") or [])
                    if str(c).strip()
                ]
                section_name = str(cl.get("section_name") or "—").strip()
                suggested = _new_content_block_name(section_name, codes, atoms_by_code)
                excerpt_parts = [
                    _atom_plain_text(atoms_by_code[c])
                    for c in codes[:3]
                    if c in atoms_by_code
                ]
                unit_rows.append(
                    {
                        "unit_id": cl.get("cluster_id") or "—",
                        "page_index": pi,
                        "section_name": section_name or "—",
                        "suggested_name": suggested,
                        "name_excerpt": _excerpt(" ".join(excerpt_parts)),
                        "atom_count": len(codes),
                        "atom_codes_preview": codes[:4],
                    }
                )
        else:
            section_rows.append(
                {
                    "page_index": pi,
                    "section_name": "—",
                    "source": "未缓存（请先在 annotate 建块页跑本页 prepare）",
                }
            )
    units = list_lesson_new_units(ctx, ensure_prepare=False)
    return {
        "step_key": "block_match",
        "step_label": "工序3 · 栏目·聚类",
        "segment_status": _segment_status(ctx).get("block_match", "pending"),
        "note": (
            "仅展示新教材 prepare 聚类与建议块名；"
            "不含 annotate「运行建块与锚定」写入的 N## 块（old_mirror 会复制旧库「课件/教材」混合格式块名）。"
        ),
        "summary": {
            "unit_count": len(units),
            "section_band_count": len(section_rows),
        },
        "unit_rows": unit_rows,
        "section_rows": section_rows[:24],
    }


def build_prep_prescan_detail(ctx: dict[str, Any]) -> dict[str, Any]:
    """工序5：预判判断（AI 初步预匹配）。"""
    payload = latest_content_scan_payload(new_lesson_id=ctx["new_les"].id)
    hits = (payload or {}).get("block_hits") or []
    hit_rows = [
        {
            "old_block_code": h.get("old_block_code") or "—",
            "new_page_index": h.get("new_page_index"),
            "match_score": h.get("match_score"),
            "hit_level": h.get("hit_level"),
            "excerpt": _excerpt(h.get("excerpt") or "", 80),
        }
        for h in hits[:20]
    ]
    return {
        "step_key": "prescan",
        "step_label": "工序4 · 对照预判",
        "segment_status": _segment_status(ctx).get("prescan", "pending"),
        "prescan": payload,
        "summary": {
            "has_scan": bool(payload),
            "coarse_agreement": (payload or {}).get("coarse_agreement"),
            "coarse_agreement_label": (payload or {}).get("coarse_agreement_label"),
            "prescan_step": (payload or {}).get("prescan_step"),
            "block_hit_count": len(hits),
            "recommended_old": (payload or {}).get("recommended_old_lesson_id"),
        },
        "rows": hit_rows,
    }


def build_prep_pair_review_detail(ctx: dict[str, Any]) -> dict[str, Any]:
    """工序6：人工确认。"""
    new_les = ctx["new_les"]
    match = get_primary_lesson_match(new_les.id)
    status = pair_review_status(match)
    review = build_pair_review_context(new_lesson_id=new_les.id)
    old_les = ctx["old_les"]
    return {
        "step_key": "pair_review",
        "step_label": "工序5 · 确认对照",
        "segment_status": _segment_status(ctx).get("pair_review", "pending"),
        "summary": {
            "pair_review_status": status,
            "old_lesson_uid": old_les.lesson_uid,
            "old_lesson_name": old_les.lesson_name,
            "match_tier": match.match_tier if match else None,
            "annotate_primary": bool(match.annotate_primary) if match else False,
        },
        "pair_review": review,
    }


PREP_STEP_BUILDERS = {
    "text_ocr": build_prep_text_ocr_detail,
    "image_ocr": build_prep_image_ocr_detail,
    "block_match": build_prep_block_match_detail,
    "prescan": build_prep_prescan_detail,
    "pair_review": build_prep_pair_review_detail,
}


def build_prep_step_detail(ctx: dict[str, Any], step_key: str) -> dict[str, Any]:
    key = (step_key or "").strip()
    fn = PREP_STEP_BUILDERS.get(key)
    if not fn:
        raise ValueError(f"未知前置工序：{step_key}")
    detail = fn(ctx)
    detail["pipeline_steps"] = ANALYSIS_PIPELINE_STEPS
    return detail
