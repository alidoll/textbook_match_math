"""旧库 AI 建块后：教材 image 原子分步纠偏（P0 邻近文字 → P1 课件插图 label → P2 视觉相似）。"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

from ....models import CoursewareSlide, LessonPage, TextbookAtom
from ....services.blobs import read_blob_bytes
from ...lesson_lookup import get_lesson_by_uid
from ...llm.slide_layout_extract import (
    load_lesson_slide_layout_cache,
    slide_layout_for_index,
)
from ...new_library.annotate.seed_from_old_page import (
    _assignments_atom_to_block,
    _atom_plain_text,
    _cluster_image_codes_on_page,
    _image_label_text,
    _is_image_like_atom,
    _move_image_between_blocks,
    _reassign_image_atoms_by_text_proximity,
    _section_bands_on_page,
    _target_block_for_image,
    _text_similarity,
)

logger = logging.getLogger(__name__)

P1_LABEL_MIN_SCORE = 0.12
P1_LABEL_MARGIN = 0.06
P1_LEAVE_SECTION_MARGIN = 0.22
P1_SECTION_HEADER_BOOST = 0.14
P2_VISUAL_MIN_SCORE = 0.52
P2_VISUAL_MARGIN = 0.08

_DECORATIVE_IMAGE_KEYWORDS = ("装饰", "图标", "卡通", "小人", "信箱", "点缀")
_QUIZ_BLOCK_KEYWORDS = ("选择题", "一起来做练习", "判断题", "多选题", "单选题", "练习吧")
_QUIZ_SLIDE_OPTION_RE = __import__("re").compile(r"[A-D][.．、]")
_PREMATCH_SLIDE_BOOST = 0.28
_PREMATCH_SLIDE_MISMATCH = -0.18
_PREMATCH_SCORE_MIN = 0.12


def _atom_prematch_primary_slide(atom: TextbookAtom) -> int | None:
    meta = getattr(atom, "metadata_json", None) or {}
    prematch = meta.get("prematch") or {}
    raw = prematch.get("primary_slide")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _segment_slide_indices(seg: dict[str, Any]) -> set[int]:
    return {int(x) for x in (seg.get("course_slide_indices") or [])}


def _is_quiz_slide_ocr(ocr: str) -> bool:
    text = ocr or ""
    if any(k in text for k in _QUIZ_BLOCK_KEYWORDS):
        return True
    if "正确" in text and _QUIZ_SLIDE_OPTION_RE.search(text):
        return True
    opts = len(_QUIZ_SLIDE_OPTION_RE.findall(text))
    return opts >= 3 and ("变化" in text or "判断" in text)


def _is_quiz_practice_segment(
    seg: dict[str, Any],
    slides_by_index: dict[int, dict[str, Any]],
) -> bool:
    for slide_idx in _segment_slide_indices(seg):
        ocr = (slides_by_index.get(slide_idx) or {}).get("ocr_text") or ""
        if _is_quiz_slide_ocr(ocr):
            return True
    return False


def _block_eligible_for_textbook_image(
    *,
    block_code: str,
    img_page: int,
    seg: dict[str, Any],
    assignments: dict[str, list[str]],
    atoms_by_code: dict[str, TextbookAtom],
    slides_by_index: dict[int, dict[str, Any]],
    atom: TextbookAtom | None = None,
) -> bool:
    """教材插图只能进同页有文字锚点的块，或预匹配 primary_slide 落在块课件页内（且非习题块）。"""
    if _is_quiz_practice_segment(seg, slides_by_index):
        return False
    anchor_pages = _block_anchor_pages(block_code, assignments, atoms_by_code)
    if anchor_pages:
        return img_page in anchor_pages
    primary = _atom_prematch_primary_slide(atom) if atom is not None else None
    if primary is not None and primary in _segment_slide_indices(seg):
        return True
    return False


def _prematch_slide_score_adjustment(
    atom: TextbookAtom,
    seg: dict[str, Any],
) -> float:
    primary = _atom_prematch_primary_slide(atom)
    if primary is None:
        return 0.0
    slides = _segment_slide_indices(seg)
    if primary in slides:
        return _PREMATCH_SLIDE_BOOST
    return _PREMATCH_SLIDE_MISMATCH


def _purge_quiz_block_images(
    *,
    assignments: dict[str, list[str]],
    segments: list[dict[str, Any]],
    block_codes: list[str],
    atoms_by_code: dict[str, TextbookAtom],
    slides_by_index: dict[int, dict[str, Any]],
) -> int:
    """习题/练习块不应保留教材页插图（避免「蜡」等关键词误绑）。"""
    removed = 0
    for blk, seg in zip(block_codes, segments):
        if not _is_quiz_practice_segment(seg, slides_by_index):
            continue
        kept: list[str] = []
        for code in assignments.get(blk) or []:
            atom = atoms_by_code.get(code)
            if atom and _is_image_like_atom(atom):
                removed += 1
                continue
            kept.append(code)
        assignments[blk] = kept
    return removed


def _is_decorative_image_atom(atom: TextbookAtom) -> bool:
    label = _image_label_text(atom)
    if not label:
        return False
    return any(k in label for k in _DECORATIVE_IMAGE_KEYWORDS)


def _page_text_atoms_in_blocks(
    page_index: int,
    assignments: dict[str, list[str]],
    atoms_by_code: dict[str, TextbookAtom],
    atom_to_block: dict[str, str],
) -> list[TextbookAtom]:
    return [
        a
        for a in atoms_by_code.values()
        if int(a.page_index) == int(page_index)
        and not _is_image_like_atom(a)
        and _atom_plain_text(a)
        and a.atom_code in atom_to_block
    ]


def _preferred_block_by_section(
    atom: TextbookAtom,
    assignments: dict[str, list[str]],
    atoms_by_code: dict[str, TextbookAtom],
    atom_to_block: dict[str, str],
) -> str | None:
    page_index = int(atom.page_index)
    text_atoms = _page_text_atoms_in_blocks(
        page_index, assignments, atoms_by_code, atom_to_block
    )
    if not text_atoms:
        return None
    bands = _section_bands_on_page(text_atoms)
    return _target_block_for_image(atom, text_atoms, atom_to_block, bands)


def _block_anchor_pages(
    block_code: str,
    assignments: dict[str, list[str]],
    atoms_by_code: dict[str, TextbookAtom],
) -> set[int]:
    pages: set[int] = set()
    for code in assignments.get(block_code) or []:
        atom = atoms_by_code.get(code)
        if not atom or _is_image_like_atom(atom):
            continue
        pages.add(int(atom.page_index))
    return pages


def _purge_cross_page_images(
    assignments: dict[str, list[str]],
    atoms_by_code: dict[str, TextbookAtom],
) -> int:
    """插图须与块内文字锚点同页，否则解绑（装饰图可保持未分配）。"""
    removed = 0
    for blk, codes in list(assignments.items()):
        anchor_pages = _block_anchor_pages(blk, assignments, atoms_by_code)
        if not anchor_pages:
            continue
        kept: list[str] = []
        for code in codes:
            atom = atoms_by_code.get(code)
            if atom and _is_image_like_atom(atom):
                if int(atom.page_index) not in anchor_pages:
                    removed += 1
                    continue
            kept.append(code)
        assignments[blk] = kept
    return removed


def _block_codes_for_segments(segments: list[dict[str, Any]]) -> list[str]:
    return [f"B{i:02d}" for i in range(1, len(segments) + 1)]


def _segments_to_assignments(
    segments: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], list[str]]:
    codes = _block_codes_for_segments(segments)
    assignments = {
        code: list(seg.get("atom_codes") or []) for code, seg in zip(codes, segments)
    }
    return assignments, codes


def _assignments_to_segments(
    segments: list[dict[str, Any]],
    assignments: dict[str, list[str]],
    block_codes: list[str],
) -> None:
    for code, seg in zip(block_codes, segments):
        seg["atom_codes"] = list(assignments.get(code) or [])


def _dedupe_assignments(assignments: dict[str, list[str]]) -> None:
    seen: set[str] = set()
    for blk in list(assignments.keys()):
        unique: list[str] = []
        for atom_code in assignments.get(blk) or []:
            c = str(atom_code).strip()
            if not c or c in seen:
                continue
            seen.add(c)
            unique.append(c)
        assignments[blk] = unique


def _is_title_slide_segment(seg: dict[str, Any]) -> bool:
    return _segment_slide_indices(seg) == {1}


def _image_ok_on_title_block(atom: TextbookAtom) -> bool:
    meta = getattr(atom, "metadata_json", None) or {}
    prematch = meta.get("prematch") or {}
    if prematch.get("primary_slide") == 1:
        return True
    label = _image_label_text(atom)
    return bool(label and ("标题" in label or "课节" in label))


def _purge_title_block_images(
    *,
    assignments: dict[str, list[str]],
    segments: list[dict[str, Any]],
    block_codes: list[str],
    atoms_by_code: dict[str, TextbookAtom],
) -> int:
    """标题页块（仅 slide 1）不保留非标题插图。"""
    removed = 0
    for blk, seg in zip(block_codes, segments):
        if not _is_title_slide_segment(seg):
            continue
        kept: list[str] = []
        for code in assignments.get(blk) or []:
            atom = atoms_by_code.get(code)
            if atom and _is_image_like_atom(atom) and not _image_ok_on_title_block(atom):
                removed += 1
                continue
            kept.append(code)
        assignments[blk] = kept
    return removed


def _prematch_locked_block(
    atom: TextbookAtom,
    segments: list[dict[str, Any]],
    block_codes: list[str],
    slides_by_index: dict[int, dict[str, Any]] | None = None,
) -> str | None:
    meta = getattr(atom, "metadata_json", None) or {}
    prematch = meta.get("prematch") or {}
    scores = prematch.get("slide_scores") or {}
    ranked: list[tuple[float, str]] = []
    for key, val in scores.items():
        try:
            si = int(key)
            score = float(val)
        except (TypeError, ValueError):
            continue
        if score < _PREMATCH_SCORE_MIN:
            continue
        idx = next(
            (i for i, seg in enumerate(segments) if si in _segment_slide_indices(seg)),
            None,
        )
        if idx is None:
            continue
        ranked.append((score, block_codes[idx]))
    if not ranked:
        primary = prematch.get("primary_slide")
        if primary is None:
            return None
        idx = next(
            (
                i
                for i, seg in enumerate(segments)
                if int(primary) in _segment_slide_indices(seg)
            ),
            None,
        )
        return block_codes[idx] if idx is not None else None
    ranked.sort(reverse=True)
    for _, blk in ranked:
        idx = block_codes.index(blk)
        if slides_by_index and _is_quiz_practice_segment(
            segments[idx], slides_by_index
        ):
            continue
        return blk
    return None


def _reassign_images_respecting_prematch(
    *,
    assignments: dict[str, list[str]],
    atoms_by_code: dict[str, TextbookAtom],
    page_index: int,
    segments: list[dict[str, Any]],
    block_codes: list[str],
    slides_by_index: dict[int, dict[str, Any]],
) -> int:
    """P0 邻近纠偏，但不挪动预匹配已锁定的插图。"""
    atom_to_block = _assignments_atom_to_block(assignments)
    locked: set[str] = set()
    for code, atom in atoms_by_code.items():
        if not _is_image_like_atom(atom) or int(atom.page_index) != int(page_index):
            continue
        blk = atom_to_block.get(code)
        lock = _prematch_locked_block(
            atom, segments, block_codes, slides_by_index
        )
        if lock and blk == lock and not _is_quiz_practice_segment(
            segments[block_codes.index(lock)], slides_by_index
        ):
            locked.add(code)

    moved = _reassign_image_atoms_by_text_proximity(
        assignments=assignments,
        atoms_by_code=atoms_by_code,
        page_index=page_index,
    )
    if not locked:
        return moved

    for code in locked:
        lock_blk = _prematch_locked_block(
            atoms_by_code[code], segments, block_codes, slides_by_index
        )
        if not lock_blk:
            continue
        cur = _assignments_atom_to_block(assignments).get(code)
        if cur != lock_blk:
            _move_image_between_blocks(
                assignments=assignments,
                atom_to_block=_assignments_atom_to_block(assignments),
                img_code=code,
                target=lock_blk,
            )
    return moved


def _attach_unassigned_images_p0(
    assignments: dict[str, list[str]],
    atoms_by_code: dict[str, TextbookAtom],
    *,
    segments: list[dict[str, Any]] | None = None,
    block_codes: list[str] | None = None,
    slides_by_index: dict[int, dict[str, Any]] | None = None,
) -> int:
    """未进任何块的 image 原子：按同页文字邻近归入块。"""
    atom_to_block = _assignments_atom_to_block(assignments)
    added = 0
    pages = sorted({int(a.page_index) for a in atoms_by_code.values()})
    for page_index in pages:
        text_atoms = [
            a
            for a in atoms_by_code.values()
            if int(a.page_index) == page_index
            and not _is_image_like_atom(a)
            and (a.content or a.ocr_text or "").strip()
            and a.atom_code in atom_to_block
        ]
        if not text_atoms:
            continue
        bands = _section_bands_on_page(text_atoms)
        for code, atom in atoms_by_code.items():
            if not _is_image_like_atom(atom) or int(atom.page_index) != page_index:
                continue
            if code in atom_to_block:
                continue
            target = _target_block_for_image(atom, text_atoms, atom_to_block, bands)
            if not target:
                continue
            if segments and block_codes and slides_by_index and target in block_codes:
                idx = block_codes.index(target)
                if _is_quiz_practice_segment(segments[idx], slides_by_index):
                    continue
            if segments and block_codes and target in block_codes:
                seg = segments[block_codes.index(target)]
                if _is_title_slide_segment(seg) and not _image_ok_on_title_block(atom):
                    continue
            assignments.setdefault(target, []).append(code)
            atom_to_block[code] = target
            added += 1
    return added


def _block_slide_label_corpus(
    *,
    block_code: str,
    assignments: dict[str, list[str]],
    segments: list[dict[str, Any]],
    block_codes: list[str],
    slides_by_index: dict[int, dict[str, Any]],
    layout_cache: dict[str, Any],
    lesson_uid: str,
) -> str:
    idx = block_codes.index(block_code)
    seg = segments[idx]
    parts: list[str] = []
    for slide_idx in seg.get("course_slide_indices") or []:
        si = int(slide_idx)
        slide = slides_by_index.get(si) or {}
        ocr = (slide.get("ocr_text") or "").strip().replace("\n", " ")
        if ocr:
            parts.append(ocr[:240])
        layout = slide_layout_for_index(lesson_uid, si, cache=layout_cache)
        for region in (layout or {}).get("regions") or []:
            if not isinstance(region, dict):
                continue
            label = str(region.get("label") or "").strip()
            if label:
                parts.append(label)
    return "\n".join(parts)


def _apply_layout_label_binding(
    *,
    assignments: dict[str, list[str]],
    segments: list[dict[str, Any]],
    block_codes: list[str],
    atoms_by_code: dict[str, TextbookAtom],
    slides_by_index: dict[int, dict[str, Any]],
    layout_cache: dict[str, Any],
    lesson_uid: str,
) -> int:
    """
    P1：教材插图 label（如「蜡熔化实验图」）↔ 课件 slide_layout 区域 label + 课件 OCR。
    用描述文字相似度决定 image 应跟哪一块（哪几页课件）。
    """
    atom_to_block = _assignments_atom_to_block(assignments)
    block_corpus = {
        code: _block_slide_label_corpus(
            block_code=code,
            assignments=assignments,
            segments=segments,
            block_codes=block_codes,
            slides_by_index=slides_by_index,
            layout_cache=layout_cache,
            lesson_uid=lesson_uid,
        )
        for code in block_codes
    }
    moved = 0
    for img_code, atom in atoms_by_code.items():
        if not _is_image_like_atom(atom):
            continue
        if _is_decorative_image_atom(atom):
            cur_blk = atom_to_block.get(img_code)
            if cur_blk:
                assignments[cur_blk] = [
                    c for c in assignments.get(cur_blk) or [] if c != img_code
                ]
                del atom_to_block[img_code]
                moved += 1
            continue
        label = _image_label_text(atom)
        if not label:
            continue
        img_page = int(atom.page_index)
        preferred_blk = _preferred_block_by_section(
            atom, assignments, atoms_by_code, atom_to_block
        )
        scores: list[tuple[float, str]] = []
        for blk, corpus in block_corpus.items():
            if not corpus.strip():
                continue
            idx = block_codes.index(blk)
            seg = segments[idx]
            if not _block_eligible_for_textbook_image(
                block_code=blk,
                img_page=img_page,
                seg=seg,
                assignments=assignments,
                atoms_by_code=atoms_by_code,
                slides_by_index=slides_by_index,
                atom=atom,
            ):
                continue
            score = _text_similarity(label, corpus)
            score += _prematch_slide_score_adjustment(atom, seg)
            if preferred_blk and blk == preferred_blk:
                score += P1_SECTION_HEADER_BOOST
            scores.append((score, blk))
        if not scores:
            continue
        scores.sort(reverse=True)
        best_score, best_blk = scores[0]
        cur_blk = atom_to_block.get(img_code)
        cur_score = 0.0
        if cur_blk and block_corpus.get(cur_blk):
            cur_score = _text_similarity(label, block_corpus[cur_blk])
            if preferred_blk and cur_blk == preferred_blk:
                cur_score += P1_SECTION_HEADER_BOOST
        if best_score < P1_LABEL_MIN_SCORE:
            continue
        if cur_blk == best_blk:
            continue
        if preferred_blk and cur_blk == preferred_blk and best_blk != preferred_blk:
            if best_score < cur_score + P1_LEAVE_SECTION_MARGIN:
                continue
        elif cur_blk and best_score < cur_score + P1_LABEL_MARGIN:
            continue
        if _move_image_between_blocks(
            assignments=assignments,
            atom_to_block=atom_to_block,
            img_code=img_code,
            target=best_blk,
        ):
            moved += 1
    return moved


def _load_rgb_array(blob_id: str | None) -> Any | None:
    if not blob_id:
        return None
    try:
        from PIL import Image
        import numpy as np

        from ....models import FileBlob

        blob = FileBlob.query.get(str(blob_id).strip())
        if not blob:
            return None
        raw = read_blob_bytes(blob)
        if not raw:
            return None
        return np.array(Image.open(BytesIO(raw)).convert("RGB"))
    except Exception as exc:
        logger.debug("blob %s unreadable for visual bind: %s", blob_id, exc)
        return None


def _crop_norm_bbox(arr: Any, bbox: dict[str, Any] | None) -> Any | None:
    import numpy as np

    if arr is None or not bbox:
        return None
    h, w = arr.shape[:2]
    if h < 2 or w < 2:
        return None
    x0 = max(0, min(w - 1, int(float(bbox.get("x_start", 0)) * w)))
    x1 = max(x0 + 1, min(w, int(float(bbox.get("x_end", 1)) * w)))
    y0 = max(0, min(h - 1, int(float(bbox.get("y_start", 0)) * h)))
    y1 = max(y0 + 1, min(h, int(float(bbox.get("y_end", 1)) * h)))
    crop = arr[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    return crop


def _visual_similarity(a: Any, b: Any) -> float:
    """裁切图相似度：缩放 + 灰度相关（与 textbook_diff 页图比对同类思路）。"""
    import cv2
    import numpy as np

    if a is None or b is None:
        return 0.0
    if a.size == 0 or b.size == 0:
        return 0.0

    def _gray(x: Any) -> Any:
        if x.ndim == 3:
            return (
                0.299 * x[:, :, 0].astype(np.float32)
                + 0.587 * x[:, :, 1].astype(np.float32)
                + 0.114 * x[:, :, 2].astype(np.float32)
            ).astype(np.uint8)
        return x.astype(np.uint8)

    target = 96
    ga, gb = _gray(a), _gray(b)

    def _resize(g: Any) -> Any:
        hh, ww = g.shape[:2]
        nh = max(1, int(hh * target / max(ww, 1)))
        return cv2.resize(g, (target, nh), interpolation=cv2.INTER_AREA)

    ga, gb = _resize(ga), _resize(gb)
    hh = min(ga.shape[0], gb.shape[0])
    ww = min(ga.shape[1], gb.shape[1])
    ga, gb = ga[:hh, :ww], gb[:hh, :ww]
    fa = ga.astype(np.float32)
    fb = gb.astype(np.float32)
    fa = (fa - fa.mean()) / (fa.std() + 1e-6)
    fb = (fb - fb.mean()) / (fb.std() + 1e-6)
    corr = float(np.clip(np.mean(fa * fb), -1.0, 1.0))
    return max(0.0, min(1.0, (corr + 1.0) / 2.0))


def _page_blob_by_index(lesson_id: str) -> dict[int, str]:
    rows = LessonPage.query.filter_by(lesson_id=lesson_id).all()
    return {int(p.page_index): str(p.blob_id) for p in rows if p.blob_id}


def _slide_blob_by_index(lesson_id: str) -> dict[int, str]:
    rows = (
        CoursewareSlide.query.filter_by(lesson_id=lesson_id)
        .order_by(CoursewareSlide.slide_index)
        .all()
    )
    return {int(s.slide_index): str(s.blob_id) for s in rows if s.blob_id}


def _block_visual_candidates(
    *,
    block_code: str,
    segments: list[dict[str, Any]],
    block_codes: list[str],
    slide_blobs: dict[int, str],
    layout_cache: dict[str, Any],
    lesson_uid: str,
) -> list[Any]:
    idx = block_codes.index(block_code)
    seg = segments[idx]
    crops: list[Any] = []
    for slide_idx in seg.get("course_slide_indices") or []:
        si = int(slide_idx)
        slide_arr = _load_rgb_array(slide_blobs.get(si))
        if slide_arr is None:
            continue
        layout = slide_layout_for_index(lesson_uid, si, cache=layout_cache)
        regions = (layout or {}).get("regions") or []
        if regions:
            for region in regions:
                if not isinstance(region, dict):
                    continue
                crop = _crop_norm_bbox(slide_arr, region)
                if crop is not None:
                    crops.append(crop)
        else:
            crops.append(slide_arr)
    return crops


def _apply_visual_similarity_binding(
    *,
    assignments: dict[str, list[str]],
    segments: list[dict[str, Any]],
    block_codes: list[str],
    atoms_by_code: dict[str, TextbookAtom],
    page_blobs: dict[int, str],
    slide_blobs: dict[int, str],
    layout_cache: dict[str, Any],
    lesson_uid: str,
    slides_by_index: dict[int, dict[str, Any]],
) -> int:
    """P2：教材 atom 裁切 ↔ 候选块课件页/插图 region 裁切，视觉相似度最高者胜出。"""
    atom_to_block = _assignments_atom_to_block(assignments)
    block_crops_cache: dict[str, list[Any]] = {}
    page_arr_cache: dict[int, Any] = {}
    moved = 0

    for img_code, atom in atoms_by_code.items():
        if not _is_image_like_atom(atom):
            continue
        page_index = int(atom.page_index)
        if page_index not in page_blobs:
            continue
        if page_index not in page_arr_cache:
            page_arr_cache[page_index] = _load_rgb_array(page_blobs[page_index])
        atom_crop = _crop_norm_bbox(page_arr_cache[page_index], atom.bbox_json or {})
        if atom_crop is None:
            continue

        scores: list[tuple[float, str]] = []
        for blk in block_codes:
            idx = block_codes.index(blk)
            seg = segments[idx]
            if not _block_eligible_for_textbook_image(
                block_code=blk,
                img_page=page_index,
                seg=seg,
                assignments=assignments,
                atoms_by_code=atoms_by_code,
                slides_by_index=slides_by_index,
                atom=atom,
            ):
                continue
            if blk not in block_crops_cache:
                block_crops_cache[blk] = _block_visual_candidates(
                    block_code=blk,
                    segments=segments,
                    block_codes=block_codes,
                    slide_blobs=slide_blobs,
                    layout_cache=layout_cache,
                    lesson_uid=lesson_uid,
                )
            best = 0.0
            for candidate in block_crops_cache[blk]:
                best = max(best, _visual_similarity(atom_crop, candidate))
            if best > 0:
                scores.append((best, blk))
        if not scores:
            continue
        scores.sort(reverse=True)
        best_score, best_blk = scores[0]
        cur_blk = atom_to_block.get(img_code)
        cur_score = 0.0
        if cur_blk:
            for candidate in block_crops_cache.get(cur_blk) or []:
                cur_score = max(cur_score, _visual_similarity(atom_crop, candidate))
        if best_score < P2_VISUAL_MIN_SCORE:
            continue
        if cur_blk == best_blk:
            continue
        if cur_blk and best_score < cur_score + P2_VISUAL_MARGIN:
            continue
        if _move_image_between_blocks(
            assignments=assignments,
            atom_to_block=atom_to_block,
            img_code=img_code,
            target=best_blk,
        ):
            moved += 1
    return moved


def refine_segments_image_bindings(
    *,
    segments: list[dict[str, Any]],
    lesson_uid: str,
    slides: list[dict[str, Any]] | None = None,
    mode: str = "full",
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    建块后三步图片纠偏（顺序固定）：
    P0 同页文字邻近 + 组图同块
    P1 教材插图 label ↔ 课件 layout label
    P2 裁切视觉相似度

    mode=off 时跳过全部插图纠偏（试点对比用）。
    mode=p0 时仅同页文字邻近 + 习题块解绑 + 跨页清理。
    """
    if not segments:
        return segments, []

    refine_mode = (mode or "full").strip().lower()
    if refine_mode == "off":
        return segments, ["插图纠偏：试点已关闭 P0/P1/P2"]

    les = get_lesson_by_uid(lesson_uid, book_type="old")
    atoms = TextbookAtom.query.filter_by(lesson_id=les.id).all()
    atoms_by_code = {a.atom_code: a for a in atoms if a.atom_code}
    if not any(_is_image_like_atom(a) for a in atoms_by_code.values()):
        return segments, []

    assignments, block_codes = _segments_to_assignments(segments)
    _dedupe_assignments(assignments)
    warnings: list[str] = []

    slides_by_index = {
        int(s["slide_index"]): s for s in (slides or []) if s.get("slide_index") is not None
    }
    if not slides_by_index:
        rows = CoursewareSlide.query.filter_by(lesson_id=les.id).all()
        slides_by_index = {
            int(s.slide_index): {"slide_index": int(s.slide_index), "ocr_text": s.ocr_text or ""}
            for s in rows
        }

    added = _attach_unassigned_images_p0(
        assignments,
        atoms_by_code,
        segments=segments,
        block_codes=block_codes,
        slides_by_index=slides_by_index,
    )
    if added:
        warnings.append(f"P0：{added} 个未分配插图已按邻近文字归入区块")

    pages = sorted({int(a.page_index) for a in atoms_by_code.values()})
    p0_moves = 0
    for page_index in pages:
        p0_moves += _reassign_images_respecting_prematch(
            assignments=assignments,
            atoms_by_code=atoms_by_code,
            page_index=page_index,
            segments=segments,
            block_codes=block_codes,
            slides_by_index=slides_by_index,
        )
    if p0_moves:
        warnings.append(f"P0：同页插图纠偏 {p0_moves} 个")

    quiz_purged = _purge_quiz_block_images(
        assignments=assignments,
        segments=segments,
        block_codes=block_codes,
        atoms_by_code=atoms_by_code,
        slides_by_index=slides_by_index,
    )
    if quiz_purged:
        warnings.append(f"规则：已从习题块解绑 {quiz_purged} 个教材插图")

    title_purged = _purge_title_block_images(
        assignments=assignments,
        segments=segments,
        block_codes=block_codes,
        atoms_by_code=atoms_by_code,
    )
    if title_purged:
        warnings.append(f"规则：标题页块已解绑 {title_purged} 个非标题插图")

    purged = _purge_cross_page_images(assignments, atoms_by_code)
    if purged:
        warnings.append(f"规则：已解绑 {purged} 个跨页/装饰插图")

    _dedupe_assignments(assignments)
    _assignments_to_segments(segments, assignments, block_codes)

    if refine_mode == "p0":
        added2 = _attach_unassigned_images_p0(
            assignments,
            atoms_by_code,
            segments=segments,
            block_codes=block_codes,
            slides_by_index=slides_by_index,
        )
        p0_after_quiz = 0
        for page_index in pages:
            p0_after_quiz += _reassign_images_respecting_prematch(
                assignments=assignments,
                atoms_by_code=atoms_by_code,
                page_index=page_index,
                segments=segments,
                block_codes=block_codes,
                slides_by_index=slides_by_index,
            )
        quiz_final = _purge_quiz_block_images(
            assignments=assignments,
            segments=segments,
            block_codes=block_codes,
            atoms_by_code=atoms_by_code,
            slides_by_index=slides_by_index,
        )
        if quiz_final:
            warnings.append(f"规则：终检已从习题块解绑 {quiz_final} 个原子")
        title_final = _purge_title_block_images(
            assignments=assignments,
            segments=segments,
            block_codes=block_codes,
            atoms_by_code=atoms_by_code,
        )
        if title_final:
            warnings.append(f"规则：终检标题页解绑 {title_final} 个非标题插图")
        if added2 or p0_after_quiz:
            warnings.append(
                f"P0：习题块清理后再邻近 {p0_after_quiz} 个"
                + (f"，补绑 {added2} 个插图" if added2 else "")
            )
        _dedupe_assignments(assignments)
        _assignments_to_segments(segments, assignments, block_codes)
        warnings.insert(0, "插图纠偏：试点仅 P0 邻近（已关闭 P1/P2）")
        return segments, warnings

    layout_cache = load_lesson_slide_layout_cache(lesson_uid)
    p1_moves = _apply_layout_label_binding(
        assignments=assignments,
        segments=segments,
        block_codes=block_codes,
        atoms_by_code=atoms_by_code,
        slides_by_index=slides_by_index,
        layout_cache=layout_cache,
        lesson_uid=lesson_uid,
    )
    if p1_moves:
        warnings.append(f"P1：按课件插图 label 语义纠偏 {p1_moves} 个")

    quiz_purged = _purge_quiz_block_images(
        assignments=assignments,
        segments=segments,
        block_codes=block_codes,
        atoms_by_code=atoms_by_code,
        slides_by_index=slides_by_index,
    )
    if quiz_purged:
        warnings.append(f"规则：已从习题块解绑 {quiz_purged} 个教材插图")

    p0_after_p1 = 0
    for page_index in pages:
        p0_after_p1 += _reassign_image_atoms_by_text_proximity(
            assignments=assignments,
            atoms_by_code=atoms_by_code,
            page_index=page_index,
        )
    added_after_p1 = _attach_unassigned_images_p0(
        assignments,
        atoms_by_code,
        segments=segments,
        block_codes=block_codes,
        slides_by_index=slides_by_index,
    )
    if p0_after_p1 or added_after_p1:
        warnings.append(
            f"P0：P1 后按栏目邻近再纠偏 {p0_after_p1} 个"
            + (f"，补绑 {added_after_p1} 个未分配插图" if added_after_p1 else "")
        )

    page_blobs = _page_blob_by_index(str(les.id))
    slide_blobs = _slide_blob_by_index(str(les.id))
    p2_moves = 0
    if page_blobs and slide_blobs:
        try:
            p2_moves = _apply_visual_similarity_binding(
                assignments=assignments,
                segments=segments,
                block_codes=block_codes,
                atoms_by_code=atoms_by_code,
                page_blobs=page_blobs,
                slide_blobs=slide_blobs,
                layout_cache=layout_cache,
                lesson_uid=lesson_uid,
                slides_by_index=slides_by_index,
            )
        except Exception as exc:
            logger.warning("P2 visual image bind skipped: %s", exc)
            warnings.append(f"P2 视觉纠偏跳过：{exc}")
    if p2_moves:
        warnings.append(f"P2：裁切视觉相似度纠偏 {p2_moves} 个")

    purged = _purge_cross_page_images(assignments, atoms_by_code)
    if purged:
        warnings.append(f"规则：已解绑 {purged} 个跨页/装饰插图")

    _dedupe_assignments(assignments)
    _assignments_to_segments(segments, assignments, block_codes)
    return segments, warnings
