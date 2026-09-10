"""对照分析流水线：步骤定义与进度段状态（intake / annotate 共用）。"""
from __future__ import annotations

ANALYSIS_PIPELINE_STEPS: list[dict[str, str]] = [
    {"key": "text_ocr", "label": "文字 OCR", "short_label": "文字OCR"},
    {"key": "image_ocr", "label": "图片 OCR", "short_label": "图片OCR"},
    {"key": "curate", "label": "AI 整理原子", "short_label": "整理原子"},
    {"key": "block_match", "label": "栏目分组·块名", "short_label": "栏目·块名"},
    {"key": "prescan", "label": "新旧对照预判", "short_label": "对照预判"},
    {"key": "pair_review", "label": "确认对照关系", "short_label": "确认对照"},
]

ANALYSIS_SEGMENT_KEYS = tuple(s["key"] for s in ANALYSIS_PIPELINE_STEPS)


def _empty_segments() -> dict[str, str]:
    return {key: "pending" for key in ANALYSIS_SEGMENT_KEYS}


def compute_analysis_segments(
    *,
    phase_stat: dict,
    atoms_stat: dict,
    scan_payload: dict | None,
    prescan_ready: bool,
    pair_status: str,
    stage: str,
    batch_progress: dict | None = None,
    pair_review_pending: str = "pending",
    pair_review_no_old: str = "no_old",
) -> dict[str, str]:
    """六段对照分析进度：文字 OCR → 图片 OCR → 整理 → 块名 → 预判断 → 确认。"""
    segs = _empty_segments()
    if stage == "no_pages":
        return segs

    text_done = bool(phase_stat.get("text_ocr_complete"))
    image_done = bool(phase_stat.get("image_ocr_complete"))
    ocr_complete = bool(phase_stat.get("ocr_complete"))
    atoms_ready = bool(atoms_stat.get("atoms_ready"))
    curate_ready = bool(atoms_stat.get("curate_ready"))
    agreement = (scan_payload or {}).get("coarse_agreement")
    prescan_done = bool(agreement)
    block_done = bool(
        prescan_ready
        or prescan_done
        or (scan_payload and (scan_payload.get("block_hits") or scan_payload.get("prescan_step") == "match_done"))
    )
    pair_done = pair_status != pair_review_pending
    pair_skip = pair_status == pair_review_no_old and not (scan_payload or {}).get("recommended_old_lesson_id")

    if batch_progress:
        bstate = batch_progress.get("state")
        if bstate == "error":
            for key in segs:
                segs[key] = "error"
            return segs
        if bstate in ("running", "queued"):
            if text_done:
                segs["text_ocr"] = "done"
            else:
                segs["text_ocr"] = "running"
            if image_done:
                segs["image_ocr"] = "done"
            elif text_done:
                segs["image_ocr"] = "running"
            elif ocr_complete:
                segs["image_ocr"] = "done"
            if ocr_complete and curate_ready:
                segs["text_ocr"] = "done"
                segs["image_ocr"] = "done"
                segs["curate"] = "done"
                if not block_done:
                    segs["block_match"] = "running"
                elif not prescan_done:
                    segs["prescan"] = "running"
                elif not pair_done:
                    segs["pair_review"] = "running"
            elif ocr_complete and atoms_ready and not curate_ready:
                segs["text_ocr"] = "done"
                segs["image_ocr"] = "done"
                segs["curate"] = "running"
            elif ocr_complete and not atoms_ready:
                segs["text_ocr"] = "done"
                segs["image_ocr"] = "done"
                segs["curate"] = "running"
            return segs

    if text_done or ocr_complete:
        segs["text_ocr"] = "done"
    elif (phase_stat.get("text_pages_done") or 0) > 0:
        segs["text_ocr"] = "running"

    if image_done or ocr_complete:
        segs["image_ocr"] = "done"
    elif text_done and (phase_stat.get("ocr_pages_done") or 0) > 0:
        segs["image_ocr"] = "running"
    elif ocr_complete:
        segs["image_ocr"] = "done"

    if curate_ready:
        segs["curate"] = "done"
    elif ocr_complete and atoms_ready:
        segs["curate"] = "running"
    elif ocr_complete:
        segs["curate"] = "running"

    if block_done:
        segs["block_match"] = "done"
    elif curate_ready:
        segs["block_match"] = "running"

    if prescan_done:
        segs["prescan"] = "done"
    elif block_done:
        segs["prescan"] = "running"

    if pair_done:
        segs["pair_review"] = "done" if not pair_skip else "skip"
    elif prescan_done or (block_done and stage in ("await_confirm", "needs_review")):
        segs["pair_review"] = "running"

    if stage in ("ready", "needs_review"):
        segs["text_ocr"] = "done"
        segs["image_ocr"] = "done"
        if curate_ready:
            segs["curate"] = "done"
        elif ocr_complete:
            segs["curate"] = "error" if stage == "ready" else "running"
        if curate_ready:
            segs["block_match"] = "done"
            segs["prescan"] = "done"
        else:
            segs["block_match"] = "pending"
            segs["prescan"] = "pending"
        if stage == "ready" and curate_ready:
            segs["pair_review"] = "done" if not pair_skip else "skip"
        elif stage == "needs_review" and curate_ready and pair_done:
            segs["pair_review"] = "done" if not pair_skip else "skip"

    if stage == "failed":
        if not ocr_complete:
            if not text_done:
                segs["text_ocr"] = "error"
            if not image_done:
                segs["image_ocr"] = "error"
        elif not curate_ready:
            segs["curate"] = "error"
        elif not block_done:
            segs["block_match"] = "error"
        elif not prescan_done:
            segs["prescan"] = "error"
        else:
            segs["pair_review"] = "error"

    return segs
