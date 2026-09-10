"""课级流水线审计：每步 pass/fail 与 DB 字段证据（单一真相来源）。"""
from __future__ import annotations

from typing import Any

from ...models import Block, Lesson, LessonPage, TextbookAtom
from ...services.lesson_lookup import get_lesson_by_uid
from ..old_library.annotate.ai_curate_atoms import AI_CURATED_AT_KEY, page_ai_curated
from .annotate.content_prescan import (
    lesson_atoms_status,
    lesson_ocr_phase_status,
    latest_content_scan_payload,
)
from .annotate.pair_review import get_primary_lesson_match, pair_review_status
from .block_pipeline import should_use_old_mirror_path
from .block_pipeline_run import lesson_block_pipeline_dict
from .lesson_analysis import lesson_analysis_dict


def _step(
    *,
    key: str,
    label: str,
    ok: bool,
    detail: str,
    evidence: dict[str, Any] | None = None,
    blocks_next: bool = True,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": ok,
        "detail": detail,
        "evidence": evidence or {},
        "blocks_next": blocks_next,
    }


def audit_lesson_pipeline(*, lesson_uid: str) -> dict[str, Any]:
    """返回课级预处理→对照分析→建块各步审计结果。"""
    les = get_lesson_by_uid(lesson_uid, book_type="new")
    lesson_id = les.id
    steps: list[dict[str, Any]] = []

    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    pages_ok = len(pages) > 0 and all(p.blob_id for p in pages)
    steps.append(
        _step(
            key="lesson_pages",
            label="教材页 PNG",
            ok=pages_ok,
            detail=f"{len(pages)} 页" if pages else "无 lesson_pages",
            evidence={"page_count": len(pages)},
            blocks_next=pages_ok,
        )
    )

    phase = lesson_ocr_phase_status(lesson_id=lesson_id)
    text_ok = bool(phase.get("text_ocr_complete"))
    steps.append(
        _step(
            key="text_ocr",
            label="文字 OCR",
            ok=text_ok,
            detail=f"{phase.get('text_pages_done', 0)}/{phase.get('ocr_pages_total', 0)} 页",
            evidence={
                "text_ocr_complete": text_ok,
                "text_pages_done": phase.get("text_pages_done"),
            },
            blocks_next=text_ok,
        )
    )

    image_ok = bool(phase.get("image_ocr_complete"))
    steps.append(
        _step(
            key="image_ocr",
            label="图片 OCR",
            ok=image_ok,
            detail="完成" if image_ok else "未完成或无图跳过",
            evidence={
                "image_ocr_complete": image_ok,
                "lesson_has_images": phase.get("lesson_has_images"),
            },
            blocks_next=image_ok,
        )
    )

    atoms_stat = lesson_atoms_status(lesson_id=lesson_id)
    curate_ok = bool(atoms_stat.get("curate_ready"))
    steps.append(
        _step(
            key="curate",
            label="AI 整理原子",
            ok=curate_ok,
            detail=(
                f"已整理 {atoms_stat.get('pages_curated', 0)}/"
                f"{atoms_stat.get('pages_total', 0)} 页"
            ),
            evidence={
                "atoms_ready": atoms_stat.get("atoms_ready"),
                "curate_ready": curate_ok,
                "atom_count": atoms_stat.get("atom_count"),
                "curate_meta_count": atoms_stat.get("curate_meta_count"),
            },
            blocks_next=True,
        )
    )

    body_ok = bool((les.body_text or "").strip())
    steps.append(
        _step(
            key="body_text",
            label="课正文 body_text",
            ok=body_ok,
            detail=f"{len(les.body_text or '')} 字符" if body_ok else "空",
            evidence={"body_text_len": len(les.body_text or "")},
            blocks_next=body_ok,
        )
    )

    scan_payload = latest_content_scan_payload(new_lesson_id=lesson_id)
    prescan_ok = bool(
        scan_payload
        and (
            scan_payload.get("coarse_agreement")
            or scan_payload.get("prescan_step") == "match_done"
        )
    )
    steps.append(
        _step(
            key="prescan",
            label="对照预判断",
            ok=prescan_ok,
            detail=(scan_payload or {}).get("coarse_agreement_label")
            or ((scan_payload or {}).get("coarse_agreement") or "未运行"),
            evidence={
                "coarse_agreement": (scan_payload or {}).get("coarse_agreement"),
                "recommended_old_lesson_id": (scan_payload or {}).get(
                    "recommended_old_lesson_id"
                ),
            },
            blocks_next=prescan_ok,
        )
    )

    primary = get_primary_lesson_match(lesson_id)
    pair_st = pair_review_status(primary)
    pair_ok = pair_st != "pending"
    steps.append(
        _step(
            key="pair_review",
            label="对照确认",
            ok=pair_ok,
            detail=pair_st,
            evidence={"pair_review_status": pair_st},
            blocks_next=pair_ok,
        )
    )

    analysis = lesson_analysis_dict(lesson_id=lesson_id, lesson_uid=lesson_uid)
    ready_for_blocks = bool(analysis.get("ready_for_blocks"))
    steps.append(
        _step(
            key="analysis_ready",
            label="分析就绪闸门",
            ok=ready_for_blocks,
            detail=analysis.get("label") or analysis.get("stage") or "",
            evidence={
                "stage": analysis.get("stage"),
                "segments": analysis.get("segments"),
            },
            blocks_next=ready_for_blocks,
        )
    )

    blocks = Block.query.filter_by(lesson_id=lesson_id).order_by(Block.sort_order).all()
    block_ok = len(blocks) > 0
    anchored = sum(
        1 for b in blocks if (b.metadata_json or {}).get("anchor_old_refs")
    )
    path_info = should_use_old_mirror_path(new_lesson_id=lesson_id)
    use_mirror = path_info.get("path") == "old_mirror"
    anchor_needed = use_mirror and block_ok
    anchor_ok = (not anchor_needed) or (anchored >= len(blocks) and len(blocks) > 0)
    suspicious = [
        b.block_code
        for b in blocks
        if (b.metadata_json or {}).get("suspicious")
    ]
    block_pipe = lesson_block_pipeline_dict(lesson_id=lesson_id, lesson_uid=lesson_uid)

    steps.append(
        _step(
            key="blocks",
            label="新区块",
            ok=block_ok,
            detail=f"{len(blocks)} 个区块",
            evidence={"block_count": len(blocks)},
            blocks_next=False,
        )
    )
    steps.append(
        _step(
            key="anchor",
            label="锚定旧块",
            ok=anchor_ok if block_ok else False,
            detail=(
                f"锚定 {anchored}/{len(blocks)}"
                if anchor_needed
                else ("跳过（new_cluster）" if block_ok else "无区块")
            ),
            evidence={
                "path": path_info.get("path"),
                "anchored_block_count": anchored,
                "block_count": len(blocks),
            },
            blocks_next=False,
        )
    )
    steps.append(
        _step(
            key="validate",
            label="建块校验",
            ok=block_ok and not suspicious,
            detail=f"可疑 {len(suspicious)} 个" if suspicious else "无 suspicious",
            evidence={"suspicious_blocks": suspicious[:12]},
            blocks_next=False,
        )
    )
    locked_ok = bool(les.blocks_locked_at)
    steps.append(
        _step(
            key="lock",
            label="区块锁定",
            ok=locked_ok,
            detail=str(les.blocks_locked_at) if locked_ok else "未锁定",
            evidence={"blocks_locked_at": str(les.blocks_locked_at or "")},
            blocks_next=False,
        )
    )

    curate_pages_detail = []
    for lp in pages:
        pi = int(lp.page_index)
        curated = page_ai_curated(lesson_id=lesson_id, page_index=pi, lp=lp)
        marker = (lp.atom_lineage_json or {}).get(AI_CURATED_AT_KEY)
        atom_n = TextbookAtom.query.filter_by(lesson_id=lesson_id, page_index=pi).count()
        curate_pages_detail.append(
            {
                "page_index": pi,
                "atom_count": atom_n,
                "ai_curated": curated,
                "curated_at": marker,
            }
        )

    blocking = next(
        (
            s
            for s in steps
            if s["key"]
            in (
                "lesson_pages",
                "text_ocr",
                "image_ocr",
                "curate",
                "prescan",
                "pair_review",
                "analysis_ready",
            )
            and not s["ok"]
        ),
        None,
    )

    failed_gates = [
        s["key"]
        for s in steps
        if s["key"]
        in (
            "lesson_pages",
            "text_ocr",
            "image_ocr",
            "curate",
            "prescan",
            "pair_review",
            "analysis_ready",
        )
        and not s["ok"]
    ]

    return {
        "ok": True,
        "lesson_uid": lesson_uid,
        "lesson_name": les.lesson_name,
        "overall_analysis_ok": ready_for_blocks,
        "overall_blocks_ok": block_ok and anchor_ok and not suspicious,
        "blocking_step": blocking["key"] if blocking else None,
        "blocking_detail": blocking["detail"] if blocking else None,
        "analysis": analysis,
        "block_pipeline": block_pipe,
        "path_info": path_info,
        "atoms_stat": atoms_stat,
        "ocr_phase": phase,
        "curate_pages": curate_pages_detail,
        "steps": steps,
        "failed_gates": [s["key"] for s in failed_gates],
    }
