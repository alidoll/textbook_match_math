"""旧侧教学区块：AI 预建建议与应用。"""
from __future__ import annotations

import re
from typing import Any

from ....extensions import db
from ....models import Block, CoursewareSlide, Lesson, LessonPage, TextbookAtom
from ....services.dictionary import list_dictionary_entries
from ....services.llm.block_seed_suggest import suggest_blocks_plan_with_llm
from ....services.llm.slide_layout_extract import (
    load_lesson_slide_layout_cache,
    slide_layout_for_index,
)
from ....services.llm.slide_text_extract import (
    load_lesson_slide_text_cache,
    slide_text_for_index,
)
from ..lessons import get_lesson_by_uid
from .block_stage import apply_stage_ref
from .blocks import _ensure_blocks_editable
from .block_seed_image_bind import refine_segments_image_bindings
from .lesson_pipeline_profile import (
    profile_atom_bind_mode,
    profile_image_refine_mode,
    profile_trust_llm_atoms,
)
from .seed_blocks import _clear_lesson_blocks, _lesson_slide_indices, parse_slides_spec


def _is_placeholder_content(atom: TextbookAtom) -> bool:
    t = (atom.content or atom.ocr_text or "").strip()
    if t.startswith("[未拆分") or t.startswith("[整页未识别") or t == "[整页图像]":
        return True
    return "-GAP-" in (atom.atom_code or "") or (atom.atom_code or "").endswith("-FULL-001")


def gather_ai_seed_context(*, lesson_uid: str) -> dict[str, Any]:
    les = get_lesson_by_uid(lesson_uid, book_type="old")
    slides = (
        CoursewareSlide.query.filter_by(lesson_id=les.id)
        .order_by(CoursewareSlide.slide_index)
        .all()
    )
    atoms = (
        TextbookAtom.query.filter_by(lesson_id=les.id)
        .order_by(TextbookAtom.page_index, TextbookAtom.atom_code)
        .all()
    )
    stage_opts = list_dictionary_entries(category="block_stage")
    allowed_names = [x["label"] for x in stage_opts if x.get("label")]

    atoms_by_page: dict[int, list[dict[str, Any]]] = {}
    for atom in atoms:
        if _is_placeholder_content(atom):
            continue
        atoms_by_page.setdefault(int(atom.page_index), []).append(
            {
                "atom_code": atom.atom_code,
                "content": atom.content or "",
                "ocr_text": atom.ocr_text or "",
                "atom_type": (atom.atom_type or "text").strip().lower(),
                "page_index": int(atom.page_index),
                "metadata_json": atom.metadata_json or {},
            }
        )

    page_rows = {
        int(p.page_index): p
        for p in LessonPage.query.filter_by(lesson_id=les.id).all()
    }

    page_groups: list[dict[str, Any]] = []
    for page_index in sorted(atoms_by_page):
        pdf_page = None
        if les.page_start:
            pdf_page = int(les.page_start) + page_index - 1
        lp = page_rows.get(page_index)
        page_groups.append(
            {
                "page_index": page_index,
                "pdf_page": pdf_page,
                "blob_id": lp.blob_id if lp else None,
                "atoms": atoms_by_page[page_index],
            }
        )

    text_cache = load_lesson_slide_text_cache(les.lesson_uid)
    layout_cache = load_lesson_slide_layout_cache(les.lesson_uid)
    slide_payloads: list[dict[str, Any]] = []
    for s in slides:
        idx = int(s.slide_index)
        ocr_text = (s.ocr_text or "").strip()
        if not ocr_text:
            entry = slide_text_for_index(les.lesson_uid, idx, cache=text_cache)
            ocr_text = ((entry or {}).get("text") or "").strip()
        layout = slide_layout_for_index(les.lesson_uid, idx, cache=layout_cache)
        regions = (layout or {}).get("regions") or []
        labels = [
            str(r.get("label") or "").strip()
            for r in regions
            if isinstance(r, dict) and (r.get("label") or "").strip()
        ]
        slide_payloads.append(
            {
                "slide_index": idx,
                "ocr_text": ocr_text,
                "blob_id": s.blob_id,
                "image_labels": labels,
            }
        )

    return {
        "lesson_uid": les.lesson_uid,
        "lesson_name": les.lesson_name,
        "unit_title": les.unit_title,
        "allowed_block_names": allowed_names,
        "slide_indices": [int(s.slide_index) for s in slides],
        "slides": slide_payloads,
        "atoms_by_page": page_groups,
        "atom_count": sum(len(v) for v in atoms_by_page.values()),
    }


def _split_count_proportional(weights: list[int], total: int) -> list[int]:
    if total <= 0 or not weights:
        return [0] * len(weights)
    total_w = sum(weights)
    if total_w <= 0:
        each = total // len(weights)
        out = [each] * len(weights)
        out[-1] += total - sum(out)
        return out
    raw = [total * w / total_w for w in weights]
    floors = [int(x) for x in raw]
    rem = total - sum(floors)
    fracs = sorted(
        ((raw[i] - floors[i], i) for i in range(len(weights))),
        reverse=True,
    )
    for j in range(rem):
        floors[fracs[j % len(fracs)][1]] += 1
    return floors


def _plain_hanzi(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _segment_slide_text(
    seg: dict[str, Any],
    slides_by_index: dict[int, dict[str, Any]],
) -> str:
    parts: list[str] = []
    for idx in seg.get("course_slide_indices") or []:
        slide = slides_by_index.get(int(idx))
        if slide:
            parts.append(str(slide.get("ocr_text") or ""))
    return "\n".join(parts)


def _atom_match_text(atom: dict[str, Any]) -> str:
    return str(atom.get("content") or atom.get("ocr_text") or "")


def _text_content_overlap_score(left: str, right: str) -> float:
    """课件 OCR 与教材原子文字的相似度（最长公共汉字串 / 较长侧）。"""
    pa, pb = _plain_hanzi(left), _plain_hanzi(right)
    if not pa or not pb:
        return 0.0
    shorter, longer = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
    if shorter in longer:
        ratio = len(shorter) / max(len(longer), 1)
        if len(shorter) >= 8:
            return max(0.75, ratio)
        return ratio
    best = 0
    hanzi_a = "".join(c for c in pa if "\u4e00" <= c <= "\u9fff")
    hanzi_b = "".join(c for c in pb if "\u4e00" <= c <= "\u9fff")
    if not hanzi_a or not hanzi_b:
        return 0.0
    for n in range(min(24, len(hanzi_a), len(hanzi_b)), 3, -1):
        for i in range(len(hanzi_a) - n + 1):
            sub = hanzi_a[i : i + n]
            if sub in hanzi_b:
                best = n
                break
        if best:
            break
    if not best:
        return 0.0
    return best / max(len(hanzi_a), len(hanzi_b), 1)


def _atoms_index_by_page(
    atoms_by_page: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[int, list[str]]]:
    by_code: dict[str, dict[str, Any]] = {}
    by_page: dict[int, list[str]] = {}
    for pg in atoms_by_page:
        page_index = int(pg.get("page_index") or 0)
        for a in pg.get("atoms") or []:
            code = str(a.get("atom_code") or "").strip()
            if not code:
                continue
            by_code[code] = a
            by_page.setdefault(page_index, []).append(code)
    return by_code, by_page


def _atom_page_index(atom: dict[str, Any]) -> int:
    return int(atom.get("page_index") or 0)


def _block_anchor_pages_from_codes(
    codes: list[str],
    by_code: dict[str, dict[str, Any]],
) -> set[int]:
    pages: set[int] = set()
    for code in codes:
        atom = by_code.get(code)
        if not atom or (atom.get("atom_type") or "text").lower() == "image":
            continue
        p = _atom_page_index(atom)
        if p > 0:
            pages.add(p)
    return pages


def _block_text_anchor_pages(
    seg: dict[str, Any],
    by_code: dict[str, dict[str, Any]],
    slides_by_index: dict[int, dict[str, Any]] | None,
) -> set[int]:
    """块内「锚定页」= 与课件 OCR 强匹配的文字原子所在页（非块内全部页）。"""
    slide_text = ""
    if slides_by_index:
        slide_text = _segment_slide_text(seg, slides_by_index)
    codes = seg.get("atom_codes") or []
    if not slide_text.strip():
        return _block_anchor_pages_from_codes(codes, by_code)
    page_scores: dict[int, float] = {}
    for code in codes:
        atom = by_code.get(code)
        if not atom or (atom.get("atom_type") or "text").lower() == "image":
            continue
        p = _atom_page_index(atom)
        score = _text_content_overlap_score(slide_text, _atom_match_text(atom))
        page_scores[p] = max(page_scores.get(p, 0.0), score)
    if not page_scores:
        return _block_anchor_pages_from_codes(codes, by_code)
    best = max(page_scores.values())
    threshold = max(0.30, best * 0.78)
    return {p for p, s in page_scores.items() if s >= threshold}


def _scrub_cross_page_bindings(
    segments: list[dict[str, Any]],
    by_code: dict[str, dict[str, Any]],
    slides_by_index: dict[int, dict[str, Any]] | None = None,
) -> int:
    """块内教材原子须与块内文字锚点同页，去掉跨页误绑（含插图）。"""
    removed = 0
    for seg in segments:
        codes = list(seg.get("atom_codes") or [])
        anchor_pages = _block_text_anchor_pages(seg, by_code, slides_by_index)
        if not anchor_pages:
            continue
        kept: list[str] = []
        for code in codes:
            atom = by_code.get(code)
            if not atom:
                kept.append(code)
                continue
            if _atom_page_index(atom) not in anchor_pages:
                removed += 1
                continue
            kept.append(code)
        seg["atom_codes"] = kept
    return removed


def _atom_prematch_primary_slide(atom: dict[str, Any]) -> int | None:
    prematch = (atom.get("metadata_json") or {}).get("prematch") or {}
    raw = prematch.get("primary_slide")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _segment_prematch_boost(
    seg: dict[str, Any],
    atom: dict[str, Any],
) -> float:
    """预匹配 primary_slide 落在块课件页内时加分。"""
    primary = _atom_prematch_primary_slide(atom)
    if primary is None:
        return 0.0
    slides = seg.get("course_slide_indices") or []
    if primary in {int(x) for x in slides}:
        return 0.35
    return 0.0


def _rebind_atoms_by_content(
    segments: list[dict[str, Any]],
    atoms_by_page: list[dict[str, Any]],
    slides: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把已绑原子挪到课件 OCR 更匹配的区块（避免教材进上一块、课件单独成块）。"""
    if not slides or not segments:
        return segments, []

    slides_by_index = {
        int(s["slide_index"]): s for s in slides if s.get("slide_index") is not None
    }
    by_code, _ = _atoms_index_by_page(atoms_by_page)
    if not by_code:
        return segments, []

    out = [{**seg, "atom_codes": list(seg.get("atom_codes") or [])} for seg in segments]
    warnings: list[str] = []
    moved = 0

    for code, atom in by_code.items():
        if (atom.get("atom_type") or "text").lower() == "image":
            continue
        atom_text = _atom_match_text(atom)
        if not atom_text or atom_text.startswith("["):
            continue
        atom_page = _atom_page_index(atom)
        scores: list[tuple[float, int]] = []
        for i, seg in enumerate(out):
            slide_text = _segment_slide_text(seg, slides_by_index)
            if not slide_text.strip():
                continue
            anchor_pages = _block_text_anchor_pages(seg, by_code, slides_by_index)
            if anchor_pages and atom_page not in anchor_pages:
                continue
            score = _text_content_overlap_score(slide_text, atom_text)
            score += _segment_prematch_boost(seg, atom)
            scores.append((score, i))
        if not scores:
            continue
        scores.sort(reverse=True)
        best_score, best_i = scores[0]
        if best_score < 0.18:
            continue
        cur_i = next(
            (i for i, seg in enumerate(out) if code in (seg.get("atom_codes") or [])),
            None,
        )
        if cur_i is None or cur_i == best_i:
            continue
        cur_score = _text_content_overlap_score(
            _segment_slide_text(out[cur_i], slides_by_index),
            atom_text,
        )
        if best_score >= cur_score + 0.08:
            out[cur_i]["atom_codes"] = [
                c for c in out[cur_i]["atom_codes"] if c != code
            ]
            out[best_i]["atom_codes"] = list(out[best_i]["atom_codes"]) + [code]
            moved += 1

    if moved:
        warnings.append(f"规则：已按课件-教材文字对齐挪动 {moved} 个原子")
    return out, warnings


def _assign_orphan_atoms_by_content(
    segments: list[dict[str, Any]],
    atoms_by_page: list[dict[str, Any]],
    slides_by_index: dict[int, dict[str, Any]],
    assigned: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """跨页 scrub 或纠偏后，把未分配原子按课件 OCR 补绑到最匹配区块。"""
    by_code, _ = _atoms_index_by_page(atoms_by_page)
    ordered: list[str] = []
    for pg in sorted(atoms_by_page, key=lambda x: int(x.get("page_index") or 0)):
        for a in pg.get("atoms") or []:
            code = str(a.get("atom_code") or "").strip()
            if code and code not in assigned:
                ordered.append(code)
    if not ordered or not slides_by_index:
        return segments, []

    out = [{**seg, "atom_codes": list(seg.get("atom_codes") or [])} for seg in segments]
    warnings: list[str] = []
    placed = 0
    for code in ordered:
        atom = by_code.get(code)
        if not atom:
            continue
        if (atom.get("atom_type") or "text").lower() == "image":
            continue
        atom_text = _atom_match_text(atom)
        if not atom_text or atom_text.startswith("["):
            continue
        atom_page = _atom_page_index(atom)
        best_score = 0.0
        best_i: int | None = None
        for i, seg in enumerate(out):
            slide_text = _segment_slide_text(seg, slides_by_index)
            if not slide_text.strip():
                continue
            anchor_pages = _block_text_anchor_pages(seg, by_code, slides_by_index)
            if anchor_pages and atom_page not in anchor_pages:
                continue
            score = _text_content_overlap_score(slide_text, atom_text)
            score += _segment_prematch_boost(seg, atom)
            if score > best_score:
                best_score = score
                best_i = i
        if best_i is not None and best_score >= 0.18:
            out[best_i]["atom_codes"] = list(out[best_i]["atom_codes"]) + [code]
            assigned.add(code)
            placed += 1
    if placed:
        warnings.append(f"规则：已为 {placed} 个游离原子按课件 OCR 补绑")
    return out, warnings


def _pick_page_codes_for_segment(
    seg: dict[str, Any],
    *,
    slides_by_index: dict[int, dict[str, Any]],
    by_page: dict[int, list[str]],
    by_code: dict[str, dict[str, Any]],
    assigned: set[str],
) -> list[str]:
    slide_text = _segment_slide_text(seg, slides_by_index)
    if not slide_text.strip():
        return []

    best_page: int | None = None
    best_score = 0.0
    for page_index, codes in sorted(by_page.items()):
        if not codes:
            continue
        page_score = max(
            _text_content_overlap_score(slide_text, _atom_match_text(by_code[c]))
            for c in codes
        )
        if page_score > best_score:
            best_score = page_score
            best_page = page_index

    if best_page is not None and best_score >= 0.18:
        picked: list[tuple[float, str]] = []
        min_score = max(0.28, best_score * 0.55)
        for code in by_page.get(best_page, []):
            if code in assigned:
                continue
            score = _text_content_overlap_score(
                slide_text, _atom_match_text(by_code[code])
            )
            if score >= min_score:
                picked.append((score, code))
        if picked or best_score >= 0.18:
            picked_codes = {c for _, c in picked}
            for code in by_page.get(best_page, []):
                if code in assigned or code in picked_codes:
                    continue
                if (by_code[code].get("atom_type") or "text").lower() == "image":
                    continue
                score = _text_content_overlap_score(
                    slide_text, _atom_match_text(by_code[code])
                )
                picked.append((max(score, 0.05), code))
        picked.sort(reverse=True)
        return [code for _, code in picked]

    picked: list[tuple[float, str]] = []
    for code, atom in by_code.items():
        if code in assigned:
            continue
        if (atom.get("atom_type") or "text").lower() == "image":
            continue
        score = _text_content_overlap_score(slide_text, _atom_match_text(atom))
        if score >= 0.28:
            picked.append((score, code))
    picked.sort(reverse=True)
    return [code for _, code in picked]


def _expand_segment_sibling_atoms(
    segments: list[dict[str, Any]],
    by_page: dict[int, list[str]],
    assigned: set[str],
    by_code: dict[str, dict[str, Any]] | None = None,
) -> None:
    """不再整页并入；同页补绑由 _pick_page_codes_for_segment 按相似度处理。"""
    return


def fill_atom_codes_heuristic(
    segments: list[dict[str, Any]],
    atoms_by_page: list[dict[str, Any]],
    slides: list[dict[str, Any]] | None = None,
    *,
    lesson_uid: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """模型未绑定或绑错时：试点课走预匹配优先；其余走课件 OCR 对齐 + 补余。"""
    if lesson_uid and profile_atom_bind_mode(lesson_uid) == "prematch":
        from .atom_bind_prematch import fill_atom_codes_prematch_first

        return fill_atom_codes_prematch_first(segments, atoms_by_page, slides)

    ordered_atoms: list[str] = []
    for pg in sorted(atoms_by_page, key=lambda x: int(x.get("page_index") or 0)):
        for a in pg.get("atoms") or []:
            code = str(a.get("atom_code") or "").strip()
            if code:
                ordered_atoms.append(code)

    if not ordered_atoms or not segments:
        return segments, []

    warnings: list[str] = []
    working = [{**seg, "atom_codes": list(seg.get("atom_codes") or [])} for seg in segments]

    slides_by_index = (
        {int(s["slide_index"]): s for s in slides if s.get("slide_index") is not None}
        if slides
        else {}
    )
    by_code, by_page = _atoms_index_by_page(atoms_by_page)

    assigned: set[str] = set()
    for seg in working:
        for c in seg.get("atom_codes") or []:
            assigned.add(str(c))

    if slides_by_index:
        content_filled = 0
        for seg in working:
            if seg.get("atom_codes"):
                continue
            codes = _pick_page_codes_for_segment(
                seg,
                slides_by_index=slides_by_index,
                by_page=by_page,
                by_code=by_code,
                assigned=assigned,
            )
            if codes:
                seg["atom_codes"] = codes
                assigned.update(codes)
                content_filled += 1
        if content_filled:
            warnings.append(
                f"规则：已按课件-教材文字对齐为 {content_filled} 个区块绑定原子"
            )

    if slides:
        working, rebind_warns = _rebind_atoms_by_content(working, atoms_by_page, slides)
        warnings.extend(rebind_warns)
        assigned = set()
        for seg in working:
            for c in seg.get("atom_codes") or []:
                assigned.add(str(c))
        if slides_by_index:
            refilled = 0
            for seg in working:
                if seg.get("atom_codes"):
                    continue
                codes = _pick_page_codes_for_segment(
                    seg,
                    slides_by_index=slides_by_index,
                    by_page=by_page,
                    by_code=by_code,
                    assigned=assigned,
                )
                if codes:
                    seg["atom_codes"] = codes
                    assigned.update(codes)
                    refilled += 1
            if refilled:
                warnings.append(f"规则：纠偏后已为 {refilled} 个空区块补绑原子")

    scrubbed = _scrub_cross_page_bindings(working, by_code, slides_by_index)
    if scrubbed:
        warnings.append(f"规则：已移除 {scrubbed} 个跨页误绑原子")

    assigned = set()
    for seg in working:
        for c in seg.get("atom_codes") or []:
            assigned.add(str(c))

    if slides_by_index:
        working, orphan_warns = _assign_orphan_atoms_by_content(
            working, atoms_by_page, slides_by_index, assigned
        )
        warnings.extend(orphan_warns)

    assigned = set()
    for seg in working:
        for c in seg.get("atom_codes") or []:
            assigned.add(str(c))

    _expand_segment_sibling_atoms(working, by_page, assigned, by_code)

    remaining = [c for c in ordered_atoms if c not in assigned]
    if not remaining:
        return working, warnings

    empty_indices = [i for i, s in enumerate(working) if not (s.get("atom_codes") or [])]
    if not empty_indices:
        warnings.append(f"仍有 {len(remaining)} 个教材原子未被模型选用")
        return working, warnings

    if len(empty_indices) == len(working):
        warnings.append("模型未绑定教材原子，已按课件页占比自动分配")
    else:
        warnings.append("部分区块未绑定原子，已对空块按课件页占比自动补全")

    empty_weights = [
        max(1, len(working[i].get("course_slide_indices") or [])) for i in empty_indices
    ]
    alloc = _split_count_proportional(empty_weights, len(remaining))

    out: list[dict[str, Any]] = []
    rem_ptr = 0
    empty_slot = 0
    for seg in working:
        existing = list(seg.get("atom_codes") or [])
        if existing:
            out.append({**seg, "atom_codes": existing})
            continue
        n = alloc[empty_slot]
        empty_slot += 1
        codes = remaining[rem_ptr : rem_ptr + n]
        rem_ptr += n
        out.append({**seg, "atom_codes": codes})

    if rem_ptr < len(remaining):
        warnings.append(f"仍有 {len(remaining) - rem_ptr} 个教材原子未分配区块")

    return out, warnings


def _normalize_stage_ref(name: str, allowed: list[str]) -> str:
    raw = (name or "").strip()
    if not raw:
        raise ValueError("环节参考不能为空")
    if raw in allowed:
        return raw
    for label in allowed:
        if label in raw or raw in label:
            return label
    raise ValueError(f"环节参考「{raw}」不在允许列表中：{allowed}")


def _normalize_block_name(name: str, allowed: list[str]) -> str:
    """兼容旧名：实为环节参考 stage_ref。"""
    return _normalize_stage_ref(name, allowed)


BLOCK_NAME_MAX_LEN = 64


def _clip_topic_name(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= BLOCK_NAME_MAX_LEN:
        return t
    clipped = t[: BLOCK_NAME_MAX_LEN - 1].rstrip() + "…"
    return clipped


def _text_excerpt(raw: str, *, max_len: int = 28) -> str:
    text = (raw or "").strip()
    if not text or text.startswith("["):
        return ""
    line = text.splitlines()[-1].strip() if "\n" in text else text
    line = re.sub(r"\s+", " ", line)
    return line[:max_len]


def _compose_topic_name(
    *,
    slide_parts: list[str],
    atom_parts: list[str],
    stage_ref: str,
) -> str:
    cw = _text_excerpt(" / ".join(slide_parts), max_len=22) if slide_parts else ""
    atom = _text_excerpt("；".join(atom_parts), max_len=22) if atom_parts else ""
    if cw and atom:
        return _clip_topic_name(f"课件：{cw}；教材：{atom}")
    if cw:
        return _clip_topic_name(f"课件：{cw}")
    if atom:
        return _clip_topic_name(f"教材：{atom}")
    return _clip_topic_name(stage_ref)


def fill_topic_names_heuristic(
    segments: list[dict[str, Any]],
    atoms_by_page: list[dict[str, Any]],
    slides: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """LLM 未填 topic_name 时，用课件 OCR + 教材原子摘录兜底。"""
    by_code: dict[str, dict[str, Any]] = {}
    for pg in atoms_by_page:
        for a in pg.get("atoms") or []:
            code = str(a.get("atom_code") or "").strip()
            if code:
                by_code[code] = a

    slides_by_index = {
        int(s["slide_index"]): s for s in (slides or []) if s.get("slide_index") is not None
    }

    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    filled = 0
    for seg in segments:
        topic = _clip_topic_name(str(seg.get("topic_name") or ""))
        if not topic:
            slide_parts: list[str] = []
            for idx in seg.get("course_slide_indices") or []:
                slide = slides_by_index.get(int(idx))
                if not slide:
                    continue
                excerpt = _text_excerpt(slide.get("ocr_text") or "")
                if excerpt and excerpt not in slide_parts:
                    slide_parts.append(excerpt)

            atom_parts: list[str] = []
            for code in seg.get("atom_codes") or []:
                a = by_code.get(str(code).strip())
                if not a:
                    continue
                excerpt = _text_excerpt(a.get("content") or a.get("ocr_text") or "")
                if excerpt and excerpt not in atom_parts:
                    atom_parts.append(excerpt)

            topic = _compose_topic_name(
                slide_parts=slide_parts,
                atom_parts=atom_parts,
                stage_ref=str(seg.get("stage_ref") or ""),
            )
            filled += 1
        out.append({**seg, "topic_name": topic})
    if filled:
        warnings.append(f"规则：已为 {filled} 个区块补全 topic_name（课件+教材摘录兜底）")
    return out, warnings


def validate_ai_block_segments(
    *,
    segments: list[dict[str, Any]],
    slide_indices: list[int],
    allowed_names: list[str],
    known_atom_codes: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """校验并规范化 AI 输出；返回 (segments, warnings)。"""
    warnings: list[str] = []
    known_slides = set(slide_indices)
    used_slides: set[int] = set()
    used_atoms: set[str] = set()
    out: list[dict[str, Any]] = []

    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            raise ValueError(f"segments[{i}] 须为对象")
        stage_ref = _normalize_stage_ref(
            str(
                seg.get("stage_ref")
                or seg.get("block_name")
                or seg.get("label")
                or ""
            ),
            allowed_names,
        )
        topic_name = _clip_topic_name(str(seg.get("topic_name") or ""))
        if len(str(seg.get("topic_name") or "").strip()) > BLOCK_NAME_MAX_LEN:
            warnings.append(
                f"区块 {stage_ref} 名称超长，已截断至 {BLOCK_NAME_MAX_LEN} 字"
            )
        raw_slides = (
            seg.get("course_slide_indices")
            if seg.get("course_slide_indices") is not None
            else seg.get("slides")
        )
        slides = parse_slides_spec(raw_slides or [])

        if not slides:
            raise ValueError(f"segments[{i}]（{stage_ref}）缺少课件页")

        missing = [s for s in slides if s not in known_slides]
        if missing:
            raise ValueError(f"segments[{i}]（{stage_ref}）课件页不存在：P{missing[0]}")

        overlap = used_slides.intersection(slides)
        if overlap:
            raise ValueError(
                f"segments[{i}]（{stage_ref}）与前面区块重复课件页：P{sorted(overlap)[0]}"
            )
        used_slides.update(slides)

        atom_codes: list[str] = []
        for code in seg.get("atom_codes") or []:
            c = str(code).strip()
            if not c:
                continue
            if c not in known_atom_codes:
                warnings.append(f"忽略未知原子 {c}（区块 {stage_ref}）")
                continue
            if c in used_atoms:
                warnings.append(f"原子 {c} 重复分配，仅保留在首块")
                continue
            used_atoms.add(c)
            atom_codes.append(c)

        out.append(
            {
                "stage_ref": stage_ref,
                "topic_name": topic_name,
                "block_name": stage_ref,
                "course_slide_indices": slides,
                "atom_codes": atom_codes,
                "reason": str(seg.get("reason") or "").strip(),
                "teaching_intent": str(seg.get("teaching_intent") or "").strip()[:500],
            }
        )

    uncovered = sorted(known_slides - used_slides)
    if uncovered:
        warnings.append(f"以下课件页未分配区块：P{', P'.join(str(x) for x in uncovered)}")

    return out, warnings


def rebind_atoms_for_lesson_blocks(*, lesson_uid: str, book_type: str = "old") -> dict[str, Any]:
    """按现有区块的课件页占比，把教材原子补绑进区块（不改动课件页分配）。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    _ensure_blocks_editable(les)

    blocks = (
        Block.query.filter_by(lesson_id=les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    if not blocks:
        raise ValueError("尚无区块，请先创建或 AI 预建区块")

    ctx = gather_ai_seed_context(lesson_uid=lesson_uid)
    if ctx["atom_count"] <= 0:
        raise ValueError("本课尚无可用教材原子，请先在左侧教材页点「提取本页」")

    from .block_stage import read_block_fields

    segments = [
        {
            "stage_ref": read_block_fields(b, book_type=book_type)["stage_ref"],
            "topic_name": read_block_fields(b, book_type=book_type)["block_name"],
            "block_name": read_block_fields(b, book_type=book_type)["stage_ref"],
            "course_slide_indices": list(b.course_slide_indices or []),
            "atom_codes": list(b.atom_codes or []),
        }
        for b in blocks
    ]

    prematch_bind = profile_atom_bind_mode(lesson_uid) == "prematch"
    if prematch_bind:
        segments = [{**seg, "atom_codes": []} for seg in segments]

    all_empty = prematch_bind or all(not seg["atom_codes"] for seg in segments)
    if all_empty and not prematch_bind:
        segments = [{**seg, "atom_codes": []} for seg in segments]

    filled, warnings = fill_atom_codes_heuristic(
        segments, ctx["atoms_by_page"], ctx.get("slides"), lesson_uid=lesson_uid
    )
    if all_empty and not any(seg.get("atom_codes") for seg in filled):
        raise ValueError("未能为区块分配教材原子")

    updated: list[dict[str, Any]] = []
    for block, seg in zip(blocks, filled):
        atom_codes = [str(c).strip() for c in (seg.get("atom_codes") or []) if str(c).strip()]
        block.atom_codes = atom_codes
        if atom_codes:
            atoms = TextbookAtom.query.filter(
                TextbookAtom.lesson_id == les.id,
                TextbookAtom.atom_code.in_(atom_codes),
            ).all()
            pages = sorted({int(a.page_index) for a in atoms})
            block.textbook_page_start = min(pages) if pages else None
            block.textbook_page_end = max(pages) if pages else None
        else:
            block.textbook_page_start = None
            block.textbook_page_end = None
        updated.append(
            {
                "block_code": block.block_code,
                "block_name": block.block_name,
                "atom_codes": atom_codes,
                "course_slide_indices": block.course_slide_indices or [],
            }
        )

    db.session.commit()
    return {
        "ok": True,
        "updated_count": len(updated),
        "blocks": updated,
        "warnings": warnings,
        "atom_count": ctx["atom_count"],
    }


def reapply_block_image_bindings(
    *,
    lesson_uid: str,
    book_type: str = "old",
    image_refine_mode: str | None = None,
) -> dict[str, Any]:
    """不重跑 LLM 建块：重算文字补绑 +（可选）P0/P1/P2 插图纠偏。"""
    refine_mode = image_refine_mode or profile_image_refine_mode(lesson_uid)
    rebind = rebind_atoms_for_lesson_blocks(lesson_uid=lesson_uid, book_type=book_type)
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    blocks = (
        Block.query.filter_by(lesson_id=les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    from .block_stage import read_block_fields

    ctx = gather_ai_seed_context(lesson_uid=lesson_uid)
    segments = [
        {
            "stage_ref": read_block_fields(b, book_type=book_type)["stage_ref"],
            "topic_name": read_block_fields(b, book_type=book_type)["block_name"],
            "course_slide_indices": list(b.course_slide_indices or []),
            "atom_codes": list(b.atom_codes or []),
        }
        for b in blocks
    ]
    warnings = list(rebind.get("warnings") or [])
    if refine_mode != "off":
        segments, img_warns = refine_segments_image_bindings(
            segments=segments,
            lesson_uid=lesson_uid,
            slides=ctx.get("slides"),
            mode=refine_mode,
        )
        warnings.extend(img_warns)
    else:
        warnings.append("插图纠偏：试点已关闭 P0/P1/P2（仅文字补绑）")
    updated: list[dict[str, Any]] = []
    for block, seg in zip(blocks, segments):
        atom_codes = [str(c).strip() for c in (seg.get("atom_codes") or []) if str(c).strip()]
        block.atom_codes = atom_codes
        if atom_codes:
            atoms = TextbookAtom.query.filter(
                TextbookAtom.lesson_id == les.id,
                TextbookAtom.atom_code.in_(atom_codes),
            ).all()
            pages = sorted({int(a.page_index) for a in atoms})
            block.textbook_page_start = min(pages) if pages else None
            block.textbook_page_end = max(pages) if pages else None
        updated.append(
            {
                "block_code": block.block_code,
                "atom_codes": atom_codes,
                "course_slide_indices": block.course_slide_indices or [],
            }
        )
    db.session.commit()
    return {
        "ok": True,
        "blocks": updated,
        "warnings": warnings,
        "image_refine_mode": refine_mode,
    }


def suggest_ai_blocks(
    *,
    lesson_uid: str,
    teaching_intent: bool = False,
    audit_feedback: str | None = None,
    trust_llm_atoms: bool | None = None,
) -> dict[str, Any]:
    ctx = gather_ai_seed_context(lesson_uid=lesson_uid)
    if not ctx["slide_indices"]:
        raise ValueError("本课尚无课件，请先在接入页上传 ZIP")

    known_atoms = {
        a["atom_code"]
        for pg in ctx["atoms_by_page"]
        for a in pg["atoms"]
    }

    trust_llm = (
        profile_trust_llm_atoms(lesson_uid)
        if trust_llm_atoms is None
        else bool(trust_llm_atoms)
    )

    llm_out = suggest_blocks_plan_with_llm(
        lesson_name=ctx["lesson_name"] or "",
        unit_title=ctx["unit_title"] or "",
        allowed_names=ctx["allowed_block_names"],
        slides=ctx["slides"],
        atoms_by_page=ctx["atoms_by_page"],
        include_teaching_intent=teaching_intent,
        audit_feedback=audit_feedback,
    )

    segments, warnings = validate_ai_block_segments(
        segments=llm_out.get("blocks") or [],
        slide_indices=ctx["slide_indices"],
        allowed_names=ctx["allowed_block_names"],
        known_atom_codes=known_atoms,
    )

    if ctx["atom_count"] > 0 and not trust_llm:
        segments, fill_warns = fill_atom_codes_heuristic(
            segments, ctx["atoms_by_page"], ctx.get("slides"), lesson_uid=lesson_uid
        )
        warnings = fill_warns + warnings
    elif trust_llm:
        warnings = ["建块：信任豆包 atom_codes，跳过规则补绑"] + warnings

    segments, topic_warns = fill_topic_names_heuristic(
        segments, ctx["atoms_by_page"], ctx.get("slides")
    )
    warnings = topic_warns + warnings

    refine_mode = profile_image_refine_mode(lesson_uid)
    if refine_mode != "off" and not trust_llm:
        segments, img_warns = refine_segments_image_bindings(
            segments=segments,
            lesson_uid=lesson_uid,
            slides=ctx.get("slides"),
            mode=refine_mode,
        )
        warnings = img_warns + warnings
    elif refine_mode == "off":
        warnings = ["插图纠偏：旧库已关闭 P0/P1/P2"] + warnings

    return {
        "ok": True,
        "lesson_uid": lesson_uid,
        "segments": segments,
        "warnings": warnings,
        "image_refine_mode": refine_mode,
        "trust_llm_atoms": trust_llm,
        "audit_feedback_used": bool((audit_feedback or "").strip()),
        "slide_count": len(ctx["slide_indices"]),
        "atom_count": ctx["atom_count"],
        "source": llm_out.get("source"),
        "model": llm_out.get("model"),
        "generated_at": llm_out.get("generated_at"),
    }


def apply_ai_block_segments(
    *,
    lesson_uid: str,
    segments: list[dict[str, Any]],
    replace_existing: bool = False,
    trust_llm_atoms: bool | None = None,
) -> dict[str, Any]:
    les = get_lesson_by_uid(lesson_uid, book_type="old")
    _ensure_blocks_editable(les)

    slide_indices = _lesson_slide_indices(les.id)
    if not slide_indices:
        raise ValueError("本课尚无课件，请先在接入页上传 ZIP")

    atoms = TextbookAtom.query.filter_by(lesson_id=les.id).all()
    known_atoms = {
        a.atom_code
        for a in atoms
        if a.atom_code and not _is_placeholder_content(a)
    }
    allowed = [
        x["label"] for x in list_dictionary_entries(category="block_stage") if x.get("label")
    ]

    parsed, warnings = validate_ai_block_segments(
        segments=segments,
        slide_indices=slide_indices,
        allowed_names=allowed,
        known_atom_codes=known_atoms,
    )

    ctx_atoms = gather_ai_seed_context(lesson_uid=lesson_uid)
    trust_llm = (
        profile_trust_llm_atoms(lesson_uid)
        if trust_llm_atoms is None
        else bool(trust_llm_atoms)
    )
    if ctx_atoms["atom_count"] > 0 and not trust_llm:
        parsed, fill_warns = fill_atom_codes_heuristic(
            parsed, ctx_atoms["atoms_by_page"], ctx_atoms.get("slides"), lesson_uid=lesson_uid
        )
        warnings = fill_warns + warnings
    elif trust_llm:
        warnings = ["建块：信任豆包 atom_codes，跳过规则补绑"] + warnings

    parsed, topic_warns = fill_topic_names_heuristic(
        parsed, ctx_atoms["atoms_by_page"], ctx_atoms.get("slides")
    )
    warnings = topic_warns + warnings

    refine_mode = profile_image_refine_mode(lesson_uid)
    if refine_mode != "off" and not trust_llm:
        parsed, img_warns = refine_segments_image_bindings(
            segments=parsed,
            lesson_uid=lesson_uid,
            slides=ctx_atoms.get("slides"),
            mode=refine_mode,
        )
        warnings = img_warns + warnings
    elif refine_mode == "off":
        warnings = ["插图纠偏：旧库已关闭 P0/P1/P2"] + warnings

    existing_count = Block.query.filter_by(lesson_id=les.id).count()
    if existing_count and not replace_existing:
        raise ValueError("本课已有区块；请传 replace_existing=true 或先手动清空")

    removed = 0
    if replace_existing and existing_count:
        removed = _clear_lesson_blocks(les.id)

    created: list[dict[str, Any]] = []
    for i, seg in enumerate(parsed, start=1):
        stage_ref = seg["stage_ref"]
        topic_name = _clip_topic_name(str(seg.get("topic_name") or ""))
        block = Block(
            lesson_id=les.id,
            block_code=f"B{i:02d}",
            block_name=topic_name,
            atom_codes=seg["atom_codes"] if seg.get("atom_codes") else [],
            course_slide_indices=seg["course_slide_indices"] or None,
            textbook_page_start=None,
            textbook_page_end=None,
            sort_order=i,
            metadata_json={
                "ai_seed": True,
                "ai_reason": seg.get("reason") or "",
                "teaching_intent": seg.get("teaching_intent") or "",
            },
        )
        apply_stage_ref(block, stage_ref)
        db.session.add(block)
        atom_codes = seg["atom_codes"] if seg.get("atom_codes") else []
        if atom_codes:
            atoms = TextbookAtom.query.filter(
                TextbookAtom.lesson_id == les.id,
                TextbookAtom.atom_code.in_(atom_codes),
            ).all()
            pages = sorted({int(a.page_index) for a in atoms})
            block.textbook_page_start = min(pages) if pages else None
            block.textbook_page_end = max(pages) if pages else None
        created.append(
            {
                "block_code": block.block_code,
                "stage_ref": stage_ref,
                "block_name": topic_name,
                "course_slide_indices": seg["course_slide_indices"],
                "atom_codes": seg["atom_codes"],
                "reason": seg.get("reason") or "",
                "teaching_intent": seg.get("teaching_intent") or "",
            }
        )

    db.session.commit()
    return {
        "ok": True,
        "created_count": len(created),
        "removed_count": removed,
        "blocks": created,
        "warnings": warnings,
    }
