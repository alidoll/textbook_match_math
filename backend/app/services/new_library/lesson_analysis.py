"""新课分析流水线：文字 OCR → 图片 OCR → 整理 → 块名 → 预判断 → 确认对照。"""
from __future__ import annotations

import copy
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import desc

from ...extensions import db
from ...models import Lesson, LessonContentScan, LessonPage
from ...query.lesson_order import order_lessons_query
from ...services.lesson_lookup import get_lesson_by_uid
from ...services.old_library.annotate.ai_curate_atoms import ai_curate_lesson
from ..lesson_filters import filter_master_class_lessons
from .analysis_pipeline import ANALYSIS_PIPELINE_STEPS, compute_analysis_segments
from .annotate.content_prescan import (
    lesson_atoms_status,
    lesson_ocr_phase_status,
    ocr_lesson_pages,
    refresh_lesson_body_text,
    run_content_prescan_match,
)
from .annotate.pair_review import (
    PAIR_REVIEW_CONFIRMED,
    PAIR_REVIEW_HEAVY_CHANGE,
    PAIR_REVIEW_NO_OLD,
    PAIR_REVIEW_PENDING,
    get_primary_lesson_match,
    pair_review_status,
)
from .annotate.workspace import build_new_annotate_workspace
_analysis_jobs: dict[str, dict[str, Any]] = {}
_analysis_lock = threading.Lock()

PRESCAN_MODE_EDITION_WIDE = "edition_wide"


def _analysis_error_label(exc: Exception) -> str:
    msg = str(exc)
    if "not bound to a Session" in msg or "DetachedInstanceError" in type(exc).__name__:
        return "OCR 会话中断，请重试"
    if "Duplicate entry" in msg and "uk_atoms_lesson_code" in msg:
        return "OCR 原子编号冲突"
    if "PendingRollbackError" in type(exc).__name__:
        return "分析中断，请重试"
    if "Query-invoked autoflush" in msg:
        return "OCR 写入失败，请重试"
    if len(msg) > 64:
        return msg[:61] + "…"
    return msg or "分析失败"


def _latest_scan(new_lesson_id: str):
    from .annotate.content_prescan import latest_content_scan_payload

    return latest_content_scan_payload(new_lesson_id=new_lesson_id)


def _prescan_match_ready_from_payload(scan: dict | None) -> bool:
    if not scan:
        return False
    return bool(scan.get("match_ready") or (scan.get("prescan_step") == "match_done"))


def auto_apply_pair_review_if_clear(
    *,
    new_lesson_id: str,
    suggested_pair_status: str | None,
    coarse_agreement: str | None,
    prescan_mode: str,
) -> str | None:
    """预判断结论明确时自动写入 pair_review。返回已应用 status 或 None。"""
    suggested = (suggested_pair_status or "").strip()
    agreement = (coarse_agreement or "").strip()

    if agreement == "suggest_swap" or suggested == "swap_primary":
        return None
    if agreement == "review_needed" and suggested not in (
        PAIR_REVIEW_CONFIRMED,
        PAIR_REVIEW_HEAVY_CHANGE,
        PAIR_REVIEW_NO_OLD,
        "confirmed",
        "heavy_change",
        "no_old",
    ):
        return None

    status_map = {
        "confirmed": PAIR_REVIEW_CONFIRMED,
        "heavy_change": PAIR_REVIEW_HEAVY_CHANGE,
        "no_old": PAIR_REVIEW_NO_OLD,
    }
    target = status_map.get(suggested)
    if target is None and prescan_mode == PRESCAN_MODE_EDITION_WIDE:
        target = PAIR_REVIEW_NO_OLD
    if target is None and agreement == "consistent":
        target = PAIR_REVIEW_CONFIRMED
    if not target:
        return None

    match = get_primary_lesson_match(new_lesson_id)
    if match:
        if pair_review_status(match) != PAIR_REVIEW_PENDING:
            return pair_review_status(match)
        match.pair_review_status = target
        match.pair_reviewed_at = datetime.utcnow()
        db.session.flush()
        return target
    if target == PAIR_REVIEW_NO_OLD:
        return target
    return None


def _analysis_segments(
    *,
    phase_stat: dict,
    atoms_stat: dict,
    scan_payload: dict | None,
    prescan_ready: bool,
    pair_status: str,
    stage: str,
    batch_progress: dict | None,
) -> dict[str, str]:
    return compute_analysis_segments(
        phase_stat=phase_stat,
        atoms_stat=atoms_stat,
        scan_payload=scan_payload,
        prescan_ready=prescan_ready,
        pair_status=pair_status,
        stage=stage,
        batch_progress=batch_progress,
        pair_review_pending=PAIR_REVIEW_PENDING,
        pair_review_no_old=PAIR_REVIEW_NO_OLD,
    )


def lesson_analysis_dict(    *,
    lesson_id: str,
    lesson_uid: str,
    batch_progress: dict | None = None,
) -> dict:
    """单课分析状态（供 intake / annotate）。"""
    phase_stat = lesson_ocr_phase_status(lesson_id=lesson_id)
    atoms_stat = lesson_atoms_status(lesson_id=lesson_id)
    scan_payload = _latest_scan(lesson_id)
    primary = get_primary_lesson_match(lesson_id)
    pair_status = pair_review_status(primary)
    prescan_ready = _prescan_match_ready_from_payload(scan_payload)
    agreement = (scan_payload or {}).get("coarse_agreement")
    scan = scan_payload or {}

    def _done(payload: dict) -> dict:
        payload["segments"] = _analysis_segments(
            phase_stat=phase_stat,
            atoms_stat=atoms_stat,
            scan_payload=scan_payload,
            prescan_ready=prescan_ready,
            pair_status=pair_status,
            stage=payload.get("stage") or "other",
            batch_progress=batch_progress,
        )
        payload["pipeline_steps"] = ANALYSIS_PIPELINE_STEPS
        summary = (scan.get("summary_text") or "").strip()
        if summary:
            payload["prescan_summary"] = summary
        agreement_label = scan.get("coarse_agreement_label")
        if agreement_label:
            payload["prescan_agreement_label"] = agreement_label
        body_sim = scan.get("lesson_body_similarity")
        if body_sim is not None:
            payload["lesson_body_similarity"] = body_sim
        pair_label = scan.get("suggested_pair_status_label")
        if pair_label:
            payload["prescan_pair_label"] = pair_label
        return payload
    if batch_progress:
        bstate = batch_progress.get("state")
        if bstate == "cancelled":
            batch_progress = None
        elif bstate in ("running", "queued"):
            return _done({
                "stage": "running",
                "label": batch_progress.get("label") or "分析中…",
                "ready_for_blocks": False,
                "needs_review": False,
                "progress": batch_progress.get("progress"),
            })
        else:
            # error / done：仅作历史记录，Intake 以库内真实进度为准（避免课内重试成功后仍显示批量失败）
            batch_progress = None

    pages = LessonPage.query.filter_by(lesson_id=lesson_id).count()
    if pages <= 0:
        return _done({
            "stage": "no_pages",
            "label": "无教材页",
            "ready_for_blocks": False,
            "needs_review": False,
            "progress": None,
        })

    if not phase_stat.get("ocr_complete"):
        done = phase_stat.get("ocr_pages_done") or 0
        total = phase_stat.get("ocr_pages_total") or pages
        return _done({
            "stage": "await_ocr",
            "label": "待 OCR",
            "ready_for_blocks": False,
            "needs_review": False,
            "progress": f"{done}/{total} 页",
        })

    if not atoms_stat.get("atoms_ready"):
        return _done({
            "stage": "await_curate",
            "label": "待 OCR 提取原子",
            "ready_for_blocks": False,
            "needs_review": False,
            "progress": None,
            "atoms_stat": atoms_stat,
        })

    if not atoms_stat.get("curate_ready"):
        curated = atoms_stat.get("pages_curated") or 0
        total = atoms_stat.get("pages_total") or 0
        return _done({
            "stage": "await_curate",
            "label": "待 AI 整理原子",
            "ready_for_blocks": False,
            "needs_review": False,
            "progress": f"{curated}/{total} 页" if total else None,
            "atoms_stat": atoms_stat,
        })

    if not prescan_ready:
        return _done({
            "stage": "await_prescan",
            "label": "待对照预判",
            "ready_for_blocks": False,
            "needs_review": False,
            "progress": None,
            "atoms_stat": atoms_stat,
        })

    if pair_status == PAIR_REVIEW_PENDING:
        if agreement in ("review_needed", "suggest_swap"):
            return _done({
                "stage": "needs_review",
                "label": scan_payload.get("coarse_agreement_label") or "建议复核",
                "ready_for_blocks": False,
                "needs_review": True,
                "progress": None,
            })
        return _done({
            "stage": "await_confirm",
            "label": "待确认对照",
            "ready_for_blocks": False,
            "needs_review": False,
            "progress": None,
        })

    return _done({
        "stage": "ready",
        "label": "分析就绪",
        "ready_for_blocks": True,
        "needs_review": False,
        "progress": None,
    })


def lesson_analysis_map(lessons: list[Lesson], *, volume_code: str | None = None) -> dict[str, dict]:
    batch = get_analysis_batch_job(volume_code) if volume_code else None
    batch_lessons = (batch or {}).get("lessons") or {}
    out: dict[str, dict] = {}
    for les in lessons:
        out[les.lesson_uid] = lesson_analysis_dict(
            lesson_id=les.id,
            lesson_uid=les.lesson_uid,
            batch_progress=batch_lessons.get(les.lesson_uid),
        )
    return out


def summarize_analysis(map_by_uid: dict[str, dict]) -> dict:
    counts = {
        "ready": 0,
        "running": 0,
        "await_ocr": 0,
        "await_curate": 0,
        "await_prescan": 0,
        "await_confirm": 0,
        "needs_review": 0,
        "failed": 0,
        "other": 0,
    }
    for row in map_by_uid.values():
        stage = row.get("stage") or "other"
        if stage in counts:
            counts[stage] += 1
        else:
            counts["other"] += 1
    return {"lesson_count": len(map_by_uid), **counts}


def _sync_batch_lesson_after_single_run(*, lesson_uid: str) -> None:
    """课内单独对照分析成功后，同步更新批量任务里该课的状态（若有）。"""
    uid = (lesson_uid or "").strip()
    if not uid:
        return
    with _analysis_lock:
        for job in _analysis_jobs.values():
            if uid not in (job.get("lessons") or {}):
                continue
            prog = job["lessons"][uid]
            if prog.get("state") in ("error", "queued", "running"):
                prog["state"] = "done"
                prog["label"] = "分析就绪"
                prog["progress"] = None


def run_lesson_analysis(*, lesson_uid: str, skip_ocr: bool = False) -> dict:
    """一键分析本课：OCR → 整理 → 预判断 → 自动确认。"""
    db.session.rollback()
    les = get_lesson_by_uid(lesson_uid, book_type="new")
    lesson_id = les.id

    pages = LessonPage.query.filter_by(lesson_id=lesson_id).count()
    if pages <= 0:
        raise ValueError("本课尚无教材页，请先在 intake 划分页码")

    warnings: list[str] = []
    if not skip_ocr:
        done_text, total, ocr_warnings = ocr_lesson_pages(
            lesson_uid=lesson_uid,
            ocr_scope="lesson_all",
            ocr_phase="text",
        )
        warnings.extend(ocr_warnings)
        done_img, _, img_warnings = ocr_lesson_pages(
            lesson_uid=lesson_uid,
            ocr_scope="lesson_all",
            ocr_phase="images",
        )
        warnings.extend(img_warnings)
        if done_text < total or done_img < total:
            warnings.append(f"OCR 文字 {done_text}/{total} 页，图片 {done_img}/{total} 页")

    refresh_lesson_body_text(lesson_id=lesson_id)
    from .annotate.atom_ocr_cleanup import merge_obvious_paragraph_fragments
    from .annotate.import_atom_split import split_dual_import_question_atoms

    merge_out = merge_obvious_paragraph_fragments(lesson_uid=lesson_uid, book_type="new")
    split_out = split_dual_import_question_atoms(lesson_uid=lesson_uid, book_type="new")
    if split_out.get("split_atoms"):
        warnings.append(f"已拆分 {split_out['split_atoms']} 个导入双设问原子")
    if merge_out.get("merged_groups"):
        warnings.append(f"已合并 {merge_out['merged_groups']} 组相邻 OCR 句段")
    refresh_lesson_body_text(lesson_id=lesson_id)

    curate_out = ai_curate_lesson(lesson_uid=lesson_uid, book_type="new")
    warnings.extend(curate_out.get("warnings") or [])
    curated = int(curate_out.get("curated_count") or 0)
    total_pages = int(curate_out.get("page_count") or 0)
    if curated < total_pages:
        failed = [p for p in (curate_out.get("pages") or []) if not p.get("ok")]
        detail = "; ".join(
            f"第 {p.get('page_index')} 页：{p.get('error') or '失败'}"
            for p in failed[:4]
        )
        msg = f"AI 整理未完成（{curated}/{total_pages} 页）"
        if detail:
            msg = f"{msg}：{detail}"
        raise ValueError(msg)
    refresh_lesson_body_text(lesson_id=lesson_id)

    run_content_prescan_match(lesson_uid=lesson_uid)
    les = get_lesson_by_uid(lesson_uid, book_type="new")
    scan_entity = (
        LessonContentScan.query.filter_by(new_lesson_id=les.id)
        .order_by(desc(LessonContentScan.created_at))
        .first()
    )
    pm = {"mode": PRESCAN_MODE_EDITION_WIDE}
    applied = None
    if scan_entity:
        applied = auto_apply_pair_review_if_clear(
            new_lesson_id=les.id,
            suggested_pair_status=scan_entity.suggested_pair_status,
            coarse_agreement=scan_entity.coarse_agreement,
            prescan_mode=pm.get("mode") or PRESCAN_MODE_EDITION_WIDE,
        )
    db.session.commit()
    _sync_batch_lesson_after_single_run(lesson_uid=lesson_uid)
    result = build_new_annotate_workspace(lesson_uid=lesson_uid)
    result["ok"] = True
    result["analysis_applied_pair_review"] = applied
    result["analysis_warnings"] = warnings
    return result


def get_analysis_batch_job(volume_code: str) -> dict | None:
    code = (volume_code or "").strip()
    with _analysis_lock:
        job = _analysis_jobs.get(code)
        if not job:
            return None
        return copy.deepcopy(job)


def _patch_batch_lesson(volume_code: str, lesson_uid: str, **fields: Any) -> None:
    with _analysis_lock:
        job = _analysis_jobs.get(volume_code)
        if not job:
            return
        prog = job["lessons"].setdefault(lesson_uid, {})
        prog.update(fields)


def _batch_cancel_requested(volume_code: str) -> bool:
    with _analysis_lock:
        job = _analysis_jobs.get(volume_code)
        return bool(job and job.get("cancel_requested"))


def _run_analysis_batch(volume_code: str, lesson_uids: list[str], app) -> None:
    with app.app_context():
        for uid in lesson_uids:
            if _batch_cancel_requested(volume_code):
                _patch_batch_lesson(
                    volume_code,
                    uid,
                    state="cancelled",
                    label="已取消",
                    progress=None,
                )
                continue
            _patch_batch_lesson(
                volume_code,
                uid,
                state="running",
                label="分析中…",
                progress=None,
            )
            try:
                run_lesson_analysis(lesson_uid=uid)
                db.session.commit()
                _patch_batch_lesson(
                    volume_code,
                    uid,
                    state="done",
                    label="分析就绪",
                    progress=None,
                )
            except Exception as exc:
                db.session.rollback()
                db.session.remove()
                _patch_batch_lesson(
                    volume_code,
                    uid,
                    state="error",
                    label=_analysis_error_label(exc),
                    progress=None,
                )
            if _batch_cancel_requested(volume_code):
                break
        with _analysis_lock:
            job = _analysis_jobs.get(volume_code)
            if job:
                if job.get("cancel_requested"):
                    job["status"] = "cancelled"
                    for uid, prog in job.get("lessons", {}).items():
                        if prog.get("state") == "queued":
                            prog["state"] = "cancelled"
                            prog["label"] = "已取消"
                else:
                    job["status"] = "done"
                job["finished_at"] = datetime.utcnow().isoformat()


def cancel_analysis_batch(volume_code: str) -> dict:
    """请求停止批量对照分析（当前课跑完后不再继续后续课时）。"""
    code = (volume_code or "").strip()
    with _analysis_lock:
        job = _analysis_jobs.get(code)
        if not job or job.get("status") != "running":
            return {
                "ok": True,
                "volume_code": code,
                "status": "idle",
                "message": "没有运行中的批量分析",
            }
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        for prog in job.get("lessons", {}).values():
            if prog.get("state") == "queued":
                prog["state"] = "cancelled"
                prog["label"] = "已取消"
        out = copy.deepcopy(job)
        out["ok"] = True
        out["message"] = "已请求取消：当前课完成后停止，排队课时不再执行"
        return out


def start_analysis_batch(
    *,
    volume_code: str,
    lesson_uids: list[str] | None = None,
    only_not_ready: bool = True,
) -> dict:
    from .volumes import get_new_volume_by_code

    volume = get_new_volume_by_code(volume_code)
    lessons = filter_master_class_lessons(
        order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    )
    if lesson_uids:
        want = set(lesson_uids)
        lessons = [les for les in lessons if les.lesson_uid in want]

    targets: list[Lesson] = []
    for les in lessons:
        if LessonPage.query.filter_by(lesson_id=les.id).count() <= 0:
            continue
        st = lesson_analysis_dict(lesson_id=les.id, lesson_uid=les.lesson_uid)
        if only_not_ready and st.get("ready_for_blocks"):
            continue
        if only_not_ready and st.get("stage") == "needs_review":
            continue
        targets.append(les)

    if not targets:
        return {
            "ok": True,
            "volume_code": volume_code,
            "status": "idle",
            "message": "没有需要分析的课时",
            "lesson_count": 0,
        }

    with _analysis_lock:
        existing = _analysis_jobs.get(volume_code)
        if existing and existing.get("status") == "running":
            return {
                "ok": True,
                "volume_code": volume_code,
                **existing,
            }

        job = {
            "ok": True,
            "volume_code": volume_code,
            "status": "running",
            "cancel_requested": False,
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
            "lesson_count": len(targets),
            "lessons": {
                les.lesson_uid: {
                    "state": "queued",
                    "label": "排队中…",
                    "progress": None,
                }
                for les in targets
            },
        }
        _analysis_jobs[volume_code] = job

    from flask import current_app

    app = current_app._get_current_object()
    threading.Thread(
        target=_run_analysis_batch,
        args=(volume_code, [les.lesson_uid for les in targets], app),
        daemon=True,
    ).start()

    return copy.deepcopy(job)
