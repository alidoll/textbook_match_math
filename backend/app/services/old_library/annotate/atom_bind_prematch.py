"""建块后原子补绑：预匹配 primary_slide 优先，避免 OCR 关键词 rebind 与空块灌填。"""
from __future__ import annotations

from typing import Any

from .ai_seed_blocks import (
    _atom_match_text,
    _atom_page_index,
    _atom_prematch_primary_slide,
    _atoms_index_by_page,
    _scrub_cross_page_bindings,
    _segment_prematch_boost,
    _segment_slide_text,
    _text_content_overlap_score,
)
from .block_seed_image_bind import _is_quiz_practice_segment

_TEXT_ASSIGN_MIN = 0.28
_PREMATCH_SCORE_MIN = 0.12


def _segment_slide_set(seg: dict[str, Any]) -> set[int]:
    return {int(x) for x in (seg.get("course_slide_indices") or [])}


def _segment_index_for_slide(segments: list[dict[str, Any]], slide_index: int) -> int | None:
    si = int(slide_index)
    for i, seg in enumerate(segments):
        if si in _segment_slide_set(seg):
            return i
    return None


def _is_title_only_segment(seg: dict[str, Any]) -> bool:
    slides = sorted(_segment_slide_set(seg))
    return slides == [1]


def _atom_ok_for_title_block(atom: dict[str, Any]) -> bool:
    if (atom.get("atom_type") or "text").strip().lower() == "title":
        return True
    if _atom_prematch_primary_slide(atom) != 1:
        return False
    if _atom_page_index(atom) != 1:
        return False
    text = _atom_match_text(atom)
    if text.startswith("["):
        return "标题" in text or "课节" in text
    return len(text.replace(" ", "")) <= 18


def _prematch_slide_scores(atom: dict[str, Any]) -> dict[int, float]:
    prematch = (atom.get("metadata_json") or {}).get("prematch") or {}
    raw = prematch.get("slide_scores") or {}
    out: dict[int, float] = {}
    for key, val in raw.items():
        try:
            out[int(key)] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def _segment_index_by_prematch(
    atom: dict[str, Any],
    segments: list[dict[str, Any]],
    slides_by_index: dict[int, dict[str, Any]],
    *,
    page_ok,
) -> int | None:
    """按预匹配 slide_scores 选块；跳过习题块，避免 primary 误指选择题页。"""
    scores = _prematch_slide_scores(atom)
    if not scores:
        primary = _atom_prematch_primary_slide(atom)
        if primary is None:
            return None
        scores = {int(primary): 0.5}

    ranked: list[tuple[float, int, int]] = []
    for si, score in scores.items():
        if score < _PREMATCH_SCORE_MIN:
            continue
        idx = _segment_index_for_slide(segments, si)
        if idx is None:
            continue
        seg = segments[idx]
        if _is_quiz_practice_segment(seg, slides_by_index):
            continue
        if _is_title_only_segment(seg) and not _atom_ok_for_title_block(atom):
            continue
        if not page_ok(atom, seg):
            continue
        ranked.append((score, si, idx))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][2]


def _segment_index_by_text_match(
    atom: dict[str, Any],
    segments: list[dict[str, Any]],
    slides_by_index: dict[int, dict[str, Any]],
    *,
    page_ok,
) -> tuple[int | None, float]:
    atom_text = _atom_match_text(atom)
    if not atom_text or atom_text.startswith("["):
        return None, 0.0
    best_i: int | None = None
    best_score = 0.0
    for i, seg in enumerate(segments):
        if _is_title_only_segment(seg) and not _atom_ok_for_title_block(atom):
            continue
        if _is_quiz_practice_segment(seg, slides_by_index):
            continue
        if not page_ok(atom, seg):
            continue
        slide_text = _segment_slide_text(seg, slides_by_index)
        if not slide_text.strip():
            continue
        score = _text_content_overlap_score(slide_text, atom_text)
        score += _segment_prematch_boost(seg, atom)
        if score > best_score:
            best_score = score
            best_i = i
    return best_i, best_score


def _dominant_textbook_page_for_segment(
    seg: dict[str, Any],
    *,
    by_page: dict[int, list[str]],
    by_code: dict[str, dict[str, Any]],
    slides_by_index: dict[int, dict[str, Any]],
) -> int | None:
    """块课件 OCR 最匹配的教材页（单页锚点）。"""
    slide_text = _segment_slide_text(seg, slides_by_index)
    if not slide_text.strip():
        return None
    best_page: int | None = None
    best_score = 0.0
    for page_index, codes in sorted(by_page.items()):
        scores: list[float] = []
        for code in codes:
            atom = by_code.get(code)
            if not atom or (atom.get("atom_type") or "text").lower() == "image":
                continue
            scores.append(_text_content_overlap_score(slide_text, _atom_match_text(atom)))
        if not scores:
            continue
        page_score = max(scores)
        if page_score > best_score:
            best_score = page_score
            best_page = page_index
    return best_page if best_score >= 0.18 else None


def _atom_page_allowed_for_segment(
    atom: dict[str, Any],
    seg: dict[str, Any],
    *,
    by_page: dict[int, list[str]],
    by_code: dict[str, dict[str, Any]],
    slides_by_index: dict[int, dict[str, Any]],
) -> bool:
    if _is_title_only_segment(seg):
        return _atom_ok_for_title_block(atom)
    dominant = _dominant_textbook_page_for_segment(
        seg, by_page=by_page, by_code=by_code, slides_by_index=slides_by_index
    )
    if dominant is None:
        return True
    return _atom_page_index(atom) == dominant


def fill_atom_codes_prematch_first(
    segments: list[dict[str, Any]],
    atoms_by_page: list[dict[str, Any]],
    slides: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """预匹配优先补绑：不做 rebind、不对空块按比例灌填。"""
    if not segments:
        return segments, []

    by_code, by_page = _atoms_index_by_page(atoms_by_page)
    if not by_code:
        return segments, []

    slides_by_index = (
        {int(s["slide_index"]): s for s in slides if s.get("slide_index") is not None}
        if slides
        else {}
    )

    def _page_ok(atom: dict[str, Any], seg: dict[str, Any]) -> bool:
        return _atom_page_allowed_for_segment(
            atom,
            seg,
            by_page=by_page,
            by_code=by_code,
            slides_by_index=slides_by_index,
        )

    working = [{**seg, "atom_codes": list(seg.get("atom_codes") or [])} for seg in segments]
    assigned: set[str] = set()
    for seg in working:
        for code in seg.get("atom_codes") or []:
            assigned.add(str(code))

    warnings: list[str] = []
    prematch_placed = 0
    text_placed = 0

    ordered_codes = sorted(by_code.keys())
    for code in ordered_codes:
        if code in assigned:
            continue
        atom = by_code[code]
        idx = _segment_index_by_prematch(
            atom, working, slides_by_index, page_ok=_page_ok
        )
        if idx is not None:
            working[idx]["atom_codes"] = list(working[idx]["atom_codes"]) + [code]
            assigned.add(code)
            prematch_placed += 1

    for code in ordered_codes:
        if code in assigned:
            continue
        atom = by_code[code]
        if (atom.get("atom_type") or "text").lower() == "image":
            continue
        idx, score = _segment_index_by_text_match(
            atom, working, slides_by_index, page_ok=_page_ok
        )
        min_score = _TEXT_ASSIGN_MIN
        if idx is not None:
            dom = _dominant_textbook_page_for_segment(
                working[idx],
                by_page=by_page,
                by_code=by_code,
                slides_by_index=slides_by_index,
            )
            if dom is not None and _atom_page_index(atom) == dom:
                min_score = 0.18
        if idx is not None and score >= min_score:
            working[idx]["atom_codes"] = list(working[idx]["atom_codes"]) + [code]
            assigned.add(code)
            text_placed += 1

    scrubbed = _scrub_cross_page_bindings(working, by_code, slides_by_index)
    if scrubbed:
        warnings.append(f"规则：已移除 {scrubbed} 个跨页误绑原子")
        assigned = set()
        for seg in working:
            for c in seg.get("atom_codes") or []:
                assigned.add(str(c))

    if prematch_placed:
        warnings.append(f"规则：预匹配 primary_slide 已绑定 {prematch_placed} 个原子")
    if text_placed:
        warnings.append(f"规则：课件 OCR 对齐已绑定 {text_placed} 个文字原子")

    remaining = [c for c in ordered_codes if c not in assigned]
    if remaining and slides_by_index:
        placed = 0
        for i, seg in enumerate(working):
            dominant = _dominant_textbook_page_for_segment(
                seg, by_page=by_page, by_code=by_code, slides_by_index=slides_by_index
            )
            if dominant is None:
                continue
            slide_text = _segment_slide_text(seg, slides_by_index)
            if not slide_text.strip():
                continue
            for code in list(remaining):
                if code in assigned:
                    continue
                atom = by_code[code]
                if _atom_page_index(atom) != dominant:
                    continue
                if _is_title_only_segment(seg) and not _atom_ok_for_title_block(atom):
                    continue
                if _is_quiz_practice_segment(seg, slides_by_index):
                    continue
                is_image = (atom.get("atom_type") or "text").lower() == "image"
                atom_text = _atom_match_text(atom)
                if is_image:
                    continue
                else:
                    if not atom_text or atom_text.startswith("["):
                        continue
                    score = _text_content_overlap_score(slide_text, atom_text)
                    if score < 0.15:
                        continue
                    placed += 1
                working[i]["atom_codes"] = list(working[i]["atom_codes"]) + [code]
                assigned.add(code)
                remaining.remove(code)
        if placed:
            warnings.append(f"规则：同页残余已补绑 {placed} 个原子")

    remaining = [c for c in ordered_codes if c not in assigned]
    if remaining:
        warnings.append(f"仍有 {len(remaining)} 个原子待 P0 插图邻近或人工确认")

    return working, warnings
