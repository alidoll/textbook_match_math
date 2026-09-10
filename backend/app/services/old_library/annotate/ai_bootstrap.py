"""旧侧建块多 Agent 编排：提取 → 整理 → AI 预建区块（中间态落库）。"""
from __future__ import annotations

from typing import Any, Literal

from ....models import LessonPage, TextbookAtom
from ...lesson_lookup import get_lesson_by_uid
from .atom_prematch_curate import atom_prematch_lesson, lesson_prematch_done
from .ai_curate_atoms import ai_curate_lesson
from .ai_seed_blocks import apply_ai_block_segments, suggest_ai_blocks
from .atom_lineage import ocr_source_count
from .blocks import blocks_locked, _ensure_blocks_editable
from .seed_blocks import _lesson_slide_indices
from .lesson_pipeline_profile import get_lesson_pipeline_profile
from .lesson_ocr_v2 import ocr_old_lesson_split_pipeline, lesson_ocr_phase_stats

BootstrapMode = Literal["full", "curate_and_seed", "seed_only"]


def _is_visible_atom(atom: TextbookAtom) -> bool:
    t = (atom.content or atom.ocr_text or "").strip()
    if t.startswith("[未拆分") or t.startswith("[整页未识别") or t == "[整页图像]":
        return False
    code = atom.atom_code or ""
    return "-GAP-" not in code and not code.endswith("-FULL-001")


def assess_lesson_bootstrap_readiness(
    *,
    lesson_uid: str,
    book_type: str = "old",
) -> dict[str, Any]:
    """
    判断一键建块应从哪一步开始。
    - full: OCR → 整理 → 建块
    - curate_and_seed: 已有 OCR 基准与原子，但尚未整理 → 跳过 OCR
    - seed_only: 已完成 OCR + 整理 → 直接建块
    """
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    pages = (
        LessonPage.query.filter_by(lesson_id=les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    atoms = TextbookAtom.query.filter_by(lesson_id=les.id).all()
    visible = [a for a in atoms if _is_visible_atom(a)]

    page_count = len(pages)
    if page_count == 0:
        return {
            "page_count": 0,
            "pages_with_ocr": 0,
            "pages_with_atoms": 0,
            "total_ocr_atoms": 0,
            "total_atoms": 0,
            "curated_pages": 0,
            "recommended_mode": "full",
            "recommended_extract_first": True,
            "recommended_skip_curate": False,
            "summary": "尚无教材页，将执行全课 OCR → AI 整理 → AI 建块",
        }

    page_indices = [p.page_index for p in pages]
    atoms_by_page: dict[int, int] = {}
    for a in visible:
        atoms_by_page[a.page_index] = atoms_by_page.get(a.page_index, 0) + 1

    pages_with_ocr = sum(1 for p in pages if p.ocr_atoms_json)
    pages_with_atoms = sum(
        1 for pi in page_indices if atoms_by_page.get(pi, 0) > 0
    )
    total_ocr = sum(len(p.ocr_atoms_json or []) for p in pages)
    total_atoms = len(visible)

    curated_pages = 0
    merged_atom_count = 0
    for p in pages:
        baseline = list(p.ocr_atoms_json or [])
        lineage = dict(p.atom_lineage_json or {})
        page_atoms = [a for a in visible if a.page_index == p.page_index]
        if baseline and len(page_atoms) < len(baseline):
            curated_pages += 1
        for a in page_atoms:
            if ocr_source_count(lineage, a.atom_code, baseline, a.bbox_json or {}) >= 2:
                merged_atom_count += 1

    all_have_ocr = pages_with_ocr == page_count
    all_have_atoms = pages_with_atoms == page_count
    curated_hint = (
        curated_pages > 0
        or merged_atom_count > 0
        or (total_ocr > 0 and total_atoms < total_ocr * 0.98)
    )

    profile = get_lesson_pipeline_profile(lesson_uid)
    skip_curate_profile = profile.get("skip_curate", False)

    if all_have_ocr and all_have_atoms and (curated_hint or skip_curate_profile):
        mode: BootstrapMode = "seed_only"
        if skip_curate_profile and not curated_hint:
            summary = "本课已完成 OCR（试点跳过整理），将直接 AI 建块"
        else:
            summary = "本课已完成 OCR 与 AI 整理，将直接 AI 建块"
    elif all_have_ocr and all_have_atoms:
        mode = "curate_and_seed"
        summary = "本课已有 OCR 与原子，将跳过 OCR，执行 AI 整理 + 建块"
    else:
        mode = "full"
        if skip_curate_profile:
            summary = "将执行豆包文字 OCR → 图片 OCR → AI 建块（试点跳过整理）"
        else:
            summary = "将执行全课 OCR → AI 整理原子 → AI 建块"

    return {
        "page_count": page_count,
        "pages_with_ocr": pages_with_ocr,
        "pages_with_atoms": pages_with_atoms,
        "total_ocr_atoms": total_ocr,
        "total_atoms": total_atoms,
        "curated_pages": curated_pages,
        "merged_atom_count": merged_atom_count,
        "curated_hint": curated_hint,
        "recommended_mode": mode,
        "recommended_extract_first": mode == "full",
        "recommended_skip_curate": mode == "seed_only" or skip_curate_profile,
        "pipeline_profile": profile,
        "ocr_phase": lesson_ocr_phase_stats(lesson_uid=lesson_uid, book_type=book_type),
        "summary": summary,
    }


def _ocr_substep(
    *,
    complete: bool,
    done: int,
    total: int,
    empty_detail: str,
) -> tuple[str, str]:
    if total <= 0:
        return ("done", empty_detail)
    if complete:
        return ("done", f"{total} 页/张" if total else "已完成")
    if done > 0:
        return ("partial", f"{done}/{total}")
    return ("pending", f"0/{total}")


def _compute_doubao_build_pipeline(
    *,
    readiness: dict[str, Any],
    block_count: int,
    blocks_locked: bool,
    slide_count: int,
    lesson_page_count: int,
    atom_count: int,
) -> dict[str, Any]:
    """旧库豆包建块四步展示：文字 OCR → 图片 OCR → 豆包建块 → 确认。"""
    page_count = int(readiness.get("page_count") or 0) or int(lesson_page_count or 0)
    phase = readiness.get("ocr_phase") or {}
    tb_pages = int(phase.get("page_count") or page_count or 0)
    slide_total = int(phase.get("slide_count") or slide_count or 0)

    if block_count > 0:
        text_status, text_detail = "done", (
            f"{tb_pages} 页" if tb_pages else "已完成"
        )
        image_status, image_detail = "done", (
            f"教材 {int(phase.get('image_pages_done') or tb_pages)}/{tb_pages}"
            + (f" · 课件 {int(phase.get('slides_layout_done') or slide_total)}/{slide_total}"
               if slide_total else "")
        )
        seed_status, seed_detail = "done", f"{block_count} 区块"
    elif page_count <= 0:
        text_status, text_detail = (
            ("done", "已有原子") if atom_count > 0 else ("pending", "待页图")
        )
        image_status, image_detail = (
            ("pending", "待页图") if atom_count <= 0 else ("pending", "待识别")
        )
        seed_status, seed_detail = "pending", "待课件" if slide_count <= 0 else "待建块"
    else:
        text_status, text_detail = _ocr_substep(
            complete=bool(phase.get("text_ocr_complete")),
            done=int(phase.get("text_pages_done") or 0)
            + int(phase.get("slides_text_done") or 0),
            total=tb_pages + slide_total,
            empty_detail="待 OCR",
        )
        image_status, image_detail = _ocr_substep(
            complete=bool(phase.get("image_ocr_complete")),
            done=int(phase.get("image_pages_done") or 0)
            + int(phase.get("slides_layout_done") or 0),
            total=tb_pages + slide_total,
            empty_detail="待 OCR",
        )
        if text_status != "done" or image_status != "done":
            seed_status, seed_detail = "pending", "待 OCR"
        elif slide_count <= 0:
            seed_status, seed_detail = "pending", "待课件"
        else:
            seed_status, seed_detail = "pending", "待建块"

    if blocks_locked:
        confirm_status, confirm_detail = "done", "已保存"
    elif block_count > 0:
        confirm_status, confirm_detail = "pending", "待核对保存"
    else:
        confirm_status, confirm_detail = "pending", "—"

    steps = [
        {"key": "ocr_text", "label": "文字OCR", "status": text_status, "detail": text_detail},
        {"key": "ocr_image", "label": "图片OCR", "status": image_status, "detail": image_detail},
        {"key": "seed", "label": "建块", "status": seed_status, "detail": seed_detail},
        {"key": "confirm", "label": "确认", "status": confirm_status, "detail": confirm_detail},
    ]

    active_index = len(steps)
    for i, step in enumerate(steps):
        if step["status"] not in ("done", "skip"):
            active_index = i
            break

    all_done = confirm_status == "done"
    if all_done:
        summary = "已完成并保存"
    elif seed_status == "done":
        summary = "待核对保存"
    elif text_status != "done" or image_status != "done":
        if text_status == "partial" or image_status == "partial":
            summary = "OCR 进行中"
        else:
            summary = "待 OCR"
    elif seed_status == "pending":
        summary = "待建块"
    else:
        summary = readiness.get("summary") or "待开始"

    return {
        "variant": "doubao",
        "steps": steps,
        "active_index": active_index,
        "all_done": all_done,
        "summary": summary,
    }


def compute_lesson_build_pipeline(
    *,
    readiness: dict[str, Any],
    block_count: int,
    blocks_locked: bool,
    slide_count: int,
    lesson_page_count: int = 0,
    atom_count: int = 0,
    lesson_id: str | None = None,
) -> dict[str, Any]:
    """单课建块进度（旧库豆包四步 /  legacy 五步）。"""
    profile = readiness.get("pipeline_profile") or {}
    if profile.get("trust_llm_atoms"):
        return _compute_doubao_build_pipeline(
            readiness=readiness,
            block_count=block_count,
            blocks_locked=blocks_locked,
            slide_count=slide_count,
            lesson_page_count=lesson_page_count,
            atom_count=atom_count,
        )

    page_count = int(readiness.get("page_count") or 0) or int(lesson_page_count or 0)
    pages_with_ocr = int(readiness.get("pages_with_ocr") or 0)
    mode = readiness.get("recommended_mode") or "full"
    curated_hint = bool(readiness.get("curated_hint"))
    skip_curate = bool(readiness.get("recommended_skip_curate"))
    profile = readiness.get("pipeline_profile") or {}
    trust_llm_atoms = bool(profile.get("trust_llm_atoms"))
    prematch_ok = lesson_prematch_done(lesson_id=lesson_id) if lesson_id else False
    if block_count > 0:
        ocr_status = "done"
        ocr_detail = f"{page_count} 页" if page_count else "已完成"
        curate_status = "done"
        curate_detail = "已整理"
        prematch_status = "done"
        prematch_detail = "已完成"
        seed_status = "done"
        seed_detail = f"{block_count} 区块"
    elif page_count <= 0:
        ocr_status = "done" if atom_count > 0 else "pending"
        ocr_detail = "已有原子" if atom_count > 0 else "待页图"
        curate_status = "pending"
        curate_detail = "待 OCR"
        prematch_status = "pending"
        prematch_detail = "待整理"
        seed_status = "pending"
        seed_detail = "待课件" if slide_count <= 0 else "待预匹配"
    elif pages_with_ocr >= page_count or atom_count > 0:
        ocr_status = "done"
        ocr_detail = f"{page_count} 页"
        if mode == "seed_only" or curated_hint or skip_curate:
            curate_status = "skip" if skip_curate and not curated_hint else "done"
            curate_detail = "试点跳过" if skip_curate and not curated_hint else "已整理"
        else:
            curate_status = "pending"
            curate_detail = "待整理"
        curate_ready = curate_status in ("done", "skip")
        if trust_llm_atoms:
            prematch_status = "skip"
            prematch_detail = "豆包直建块"
        elif not curate_ready:
            prematch_status = "pending"
            prematch_detail = "待整理"
        elif prematch_ok:
            prematch_status = "done"
            prematch_detail = "已完成"
        else:
            prematch_status = "pending"
            prematch_detail = "待预匹配"
        prematch_ready = prematch_status in ("done", "skip")
        if slide_count <= 0:
            seed_status = "pending"
            seed_detail = "待课件"
        elif not curate_ready:
            seed_status = "pending"
            seed_detail = "待整理"
        elif not prematch_ready:
            seed_status = "pending"
            seed_detail = "待预匹配"
        else:
            seed_status = "pending"
            seed_detail = "待建块"
    elif pages_with_ocr > 0:
        ocr_status = "partial"
        ocr_detail = f"{pages_with_ocr}/{page_count} 页"
        curate_status = "pending"
        curate_detail = "待 OCR"
        prematch_status = "pending"
        prematch_detail = "待整理"
        seed_status = "pending"
        seed_detail = "待整理" if slide_count > 0 else "待课件"
    else:
        ocr_status = "pending"
        ocr_detail = f"0/{page_count} 页"
        curate_status = "pending"
        curate_detail = "待 OCR"
        prematch_status = "pending"
        prematch_detail = "待整理"
        seed_status = "pending"
        seed_detail = "待课件" if slide_count <= 0 else "待整理"

    if blocks_locked:
        confirm_status = "done"
        confirm_detail = "已保存"
    elif block_count > 0:
        confirm_status = "pending"
        confirm_detail = "待核对"
    else:
        confirm_status = "pending"
        confirm_detail = "—"

    steps = [
        {"key": "ocr", "label": "OCR", "status": ocr_status, "detail": ocr_detail},
        {
            "key": "curate",
            "label": "版面整理",
            "status": curate_status,
            "detail": curate_detail,
        },
        {
            "key": "prematch",
            "label": "课件预匹配",
            "status": prematch_status,
            "detail": prematch_detail,
        },
        {"key": "seed", "label": "AI建块", "status": seed_status, "detail": seed_detail},
        {
            "key": "confirm",
            "label": "确认",
            "status": confirm_status,
            "detail": confirm_detail,
        },
    ]

    active_index = len(steps)
    for i, step in enumerate(steps):
        if step["status"] not in ("done", "skip"):
            active_index = i
            break

    all_done = confirm_status == "done"
    if all_done:
        summary = "已完成并保存"
    elif seed_status == "done":
        summary = "待核对保存"
    elif prematch_status == "done" and seed_status == "pending":
        summary = "待 AI 建块"
    elif curate_status in ("done", "skip") and prematch_status == "pending":
        summary = "待课件预匹配"
    elif curate_status == "done" and seed_status == "pending":
        summary = "待 AI 建块"
    elif ocr_status == "done" and curate_status not in ("done", "skip"):
        summary = "待版面整理"
    elif ocr_status == "partial":
        summary = f"OCR 中 · {ocr_detail}"
    else:
        summary = readiness.get("summary") or "待开始"

    return {
        "variant": "legacy",
        "steps": steps,
        "active_index": active_index,
        "all_done": all_done,
        "summary": summary,
    }


def ai_bootstrap_lesson(
    *,
    lesson_uid: str,
    extract_first: bool | None = None,
    replace_blocks: bool = True,
    skip_block_seed: bool = False,
    skip_curate: bool | None = None,
    skip_prematch: bool | None = None,
    book_type: str = "old",
) -> dict[str, Any]:
    """
    Agent 流水线（LangChain 各步独立调用，上下文经数据库传递）：
    1. 提取 — OCR 粗原子
    2. 版面整理 — 视觉大模型 merge/delete
    3. 课件预匹配 — 按 slide OCR 拆/合/标引用
    4. 预建区块 — 文本大模型建块
    """
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    if blocks_locked(les):
        raise ValueError("本课区块已锁定，请先解锁")

    slides = _lesson_slide_indices(les.id)
    if not slides and not skip_block_seed:
        raise ValueError("本课尚无课件，请先在接入页上传 ZIP")

    readiness = assess_lesson_bootstrap_readiness(
        lesson_uid=lesson_uid, book_type=book_type
    )
    do_extract = (
        readiness["recommended_extract_first"]
        if extract_first is None
        else extract_first
    )
    profile = get_lesson_pipeline_profile(lesson_uid)
    ocr_phase = readiness.get("ocr_phase") or lesson_ocr_phase_stats(
        lesson_uid=lesson_uid, book_type=book_type
    )
    # OCR 已齐全时默认只建块；仅当客户端显式 extract_first=true 才重跑 OCR
    if (
        do_extract
        and profile.get("split_ocr")
        and ocr_phase.get("text_ocr_complete")
        and ocr_phase.get("image_ocr_complete")
        and extract_first is not True
    ):
        do_extract = False

    if skip_curate is None and profile.get("skip_curate"):
        skip_curate = True

    do_skip_curate = (
        readiness["recommended_skip_curate"]
        if skip_curate is None
        else skip_curate
    )

    if do_extract and profile.get("doubao_text_ocr") and profile.get("split_ocr"):
        ocr_out = ocr_old_lesson_split_pipeline(lesson_uid=lesson_uid)
        do_extract = False
        extract_note = ocr_out
    else:
        extract_note = None

    if do_skip_curate:
        curate_out: dict[str, Any] = {
            "skipped": True,
            "reason": "existing_curated_atoms",
            "page_count": readiness["page_count"],
            "readiness": readiness,
        }
    else:
        curate_out = ai_curate_lesson(
            lesson_uid=lesson_uid,
            extract_first=do_extract,
            book_type=book_type,
        )
        curate_out["readiness"] = readiness

    if skip_prematch is None and profile.get("trust_llm_atoms"):
        skip_prematch = True
    do_skip_prematch = bool(skip_prematch) if skip_prematch is not None else False
    prematch_out: dict[str, Any] = {"skipped": True}
    if not skip_block_seed and not do_skip_prematch and slides:
        prematch_out = atom_prematch_lesson(lesson_uid=lesson_uid, book_type=book_type)
        prematch_out["skipped"] = False

    block_out: dict[str, Any] = {"skipped": True}
    all_warnings = list(curate_out.get("warnings") or [])
    all_warnings.extend(prematch_out.get("warnings") or [])

    if not skip_block_seed:
        _ensure_blocks_editable(les)
        suggest = suggest_ai_blocks(
            lesson_uid=lesson_uid,
            teaching_intent=bool(profile.get("teaching_intent_on_seed")),
        )
        all_warnings.extend(suggest.get("warnings") or [])
        apply_out = apply_ai_block_segments(
            lesson_uid=lesson_uid,
            segments=suggest.get("segments") or [],
            replace_existing=replace_blocks,
        )
        all_warnings.extend(apply_out.get("warnings") or [])
        block_out = {
            "skipped": False,
            "created_count": apply_out.get("created_count"),
            "removed_count": apply_out.get("removed_count"),
            "blocks": apply_out.get("blocks"),
            "model": suggest.get("model"),
        }

    return {
        "ok": True,
        "lesson_uid": lesson_uid,
        "readiness": readiness,
        "ocr_v2": extract_note,
        "curate": curate_out,
        "prematch": prematch_out,
        "blocks": block_out,
        "warnings": all_warnings,
        "pipeline_profile": profile,
    }
