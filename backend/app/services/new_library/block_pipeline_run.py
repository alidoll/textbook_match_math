"""建块流水线执行：异步任务 + 整课 old_mirror 路径。"""
from __future__ import annotations

import copy
import threading
import time
from datetime import datetime
from typing import Any

from ...extensions import db
from ...models import Block, Lesson, LessonPage
from ...query.lesson_order import order_lessons_query
from ..lesson_filters import filter_master_class_lessons
from ..lesson_lookup import get_lesson_by_uid
from ..old_library.annotate.workspace import _block_dict
from .annotate.anchor_validate import mark_suspicious_blocks, run_anchor_validation
from .annotate.pair_review import get_primary_lesson_match
from .annotate.atom_ocr_cleanup import cleanup_lesson_atom_ocr_text
from .annotate.seed_from_old_page import (
    anchor_existing_blocks_on_page,
    apply_anchored_block_display_names,
    clear_lesson_blocks_for_pipeline,
    compute_lesson_atom_coverage,
    rebalance_lesson_atoms_by_anchor_profile,
    reconcile_lesson_atom_coverage,
    seed_blocks_from_old_page,
    seed_unassigned_atoms_as_new_blocks,
)
from .annotate.workspace import build_new_annotate_workspace
from .block_pipeline_prepare import (
    clear_lesson_prepare,
    run_balance_step,
    run_column_detect_step,
    run_topic_cluster_step,
    seed_blocks_from_page_clusters,
)
from .block_pipeline import (
    ATOM_COVERAGE_MIN,
    BLOCK_PIPELINE_STEPS,
    apply_block_pipeline_metadata,
    build_blocking_summary,
    compute_block_progress,
    empty_block_segments,
    should_use_old_mirror_path,
)

_block_jobs: dict[str, dict[str, Any]] = {}
_block_lock = threading.Lock()
_batch_volume_context: dict[str, str] = {}


def _sync_block_batch_from_lesson_job(volume_code: str, lesson_uid: str) -> None:
    job = get_block_pipeline_job(lesson_uid)
    if not job:
        return
    fields: dict[str, Any] = {}
    if job.get("segments"):
        fields["segments"] = copy.deepcopy(job["segments"])
    if job.get("label"):
        fields["label"] = job["label"]
    if fields:
        _patch_block_batch_lesson(volume_code, lesson_uid, **fields)

FAST_SKIP_STEP_DELAY = 0.12  # 保留常量兼容；1–3 步已改为真实执行


def _block_error_label(exc: Exception) -> str:
    msg = str(exc)
    if len(msg) > 80:
        return msg[:77] + "…"
    return msg or "建块失败"


def get_block_pipeline_job(lesson_uid: str) -> dict | None:
    uid = (lesson_uid or "").strip()
    with _block_lock:
        job = _block_jobs.get(uid)
        if not job:
            return None
        return copy.deepcopy(job)


def _patch_job(lesson_uid: str, **fields: Any) -> None:
    with _block_lock:
        job = _block_jobs.get(lesson_uid)
        if not job:
            return
        job.update(fields)
    vol = _batch_volume_context.get(lesson_uid)
    if vol and ("label" in fields or "segments" in fields):
        _sync_block_batch_from_lesson_job(vol, lesson_uid)


def _set_segment(lesson_uid: str, step_key: str, status: str) -> None:
    with _block_lock:
        job = _block_jobs.get(lesson_uid)
        if not job:
            return
        job["segments"][step_key] = status
        if status == "done":
            job["progress"] = round(compute_block_progress(step_key, 1.0) * 100)
        elif status == "running":
            job["progress"] = round(compute_block_progress(step_key, 0.5) * 100)
    vol = _batch_volume_context.get(lesson_uid)
    if vol:
        _sync_block_batch_from_lesson_job(vol, lesson_uid)


def _finalize_block_metadata(
    *,
    lesson_id: str,
    source_path: str,
    ai_step: str,
) -> list[dict]:
    blocks = (
        Block.query.filter_by(lesson_id=lesson_id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    out: list[dict] = []
    for block in blocks:
        meta = block.metadata_json or {}
        bp = meta.get("block_pipeline") or {}
        if bp.get("ai_step") == "edit_ready":
            out.append(_block_dict(block))
            continue
        match_score = meta.get("match_score")
        top2 = meta.get("match_score_top2")
        meta = apply_block_pipeline_metadata(
            meta,
            source_path=bp.get("source_path") or source_path,
            ai_step=ai_step,
            match_score=match_score,
            match_score_top2=top2,
            suspicious=bool(meta.get("suspicious")),
            suspicious_reason=str(meta.get("suspicious_reason") or ""),
            stage_ref=block.block_name or "",
        )
        block.metadata_json = meta
        out.append(_block_dict(block))
    return out


def _run_prepare_steps(
    *,
    lesson_uid: str,
    lesson_id: str,
    lesson_name: str,
) -> None:
    """①–③ 栏目识别 → 主题聚类 → 大小均衡（全路径共用）。"""
    _set_segment(lesson_uid, "column_detect", "running")
    run_column_detect_step(
        lesson_id=lesson_id,
        lesson_uid=lesson_uid,
        lesson_name=lesson_name,
    )
    _set_segment(lesson_uid, "column_detect", "done")

    _set_segment(lesson_uid, "topic_cluster", "running")
    run_topic_cluster_step(lesson_id=lesson_id, lesson_uid=lesson_uid)
    _set_segment(lesson_uid, "topic_cluster", "done")

    _set_segment(lesson_uid, "balance", "running")
    run_balance_step(lesson_uid=lesson_uid)
    _set_segment(lesson_uid, "balance", "done")


def _run_mirror_after_prepare(
    *,
    lesson_uid: str,
    lesson_id: str,
    path_info: dict,
) -> dict:
    """旧块镜像：④ 建块 → ⑤ 锚定 → ⑥⑦ 校验。"""
    cleanup_lesson_atom_ocr_text(lesson_id=lesson_id)
    from .annotate.import_atom_split import split_dual_import_question_atoms

    split_dual_import_question_atoms(lesson_uid=lesson_uid, book_type="new")
    db.session.flush()

    clear_lesson_blocks_for_pipeline(lesson_uid=lesson_uid)
    db.session.commit()

    _set_segment(lesson_uid, "attributes", "running")
    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    created_total = 0
    seed_errors: list[str] = []
    page_total = max(len(pages), 1)
    for idx, page in enumerate(pages):
        sub = (idx + 0.5) / page_total
        with _block_lock:
            job = _block_jobs.get(lesson_uid)
            if job:
                job["progress"] = round(compute_block_progress("attributes", sub) * 100)
        try:
            result = seed_blocks_from_old_page(
                lesson_uid=lesson_uid,
                new_page_index=int(page.page_index),
                old_page_index=int(page.page_index),
                replace_existing=False,
            )
            created_total += int(result.get("created_count") or 0)
        except ValueError as exc:
            db.session.rollback()
            seed_errors.append(f"第{page.page_index}页建块: {exc}")

    new_content_created = 0
    for page in pages:
        try:
            orphan = seed_unassigned_atoms_as_new_blocks(
                lesson_uid=lesson_uid,
                page_index=int(page.page_index),
            )
            new_content_created += int(orphan.get("created_count") or 0)
        except ValueError:
            db.session.rollback()

    rebalance = rebalance_lesson_atoms_by_anchor_profile(lesson_uid=lesson_uid)
    reconcile = reconcile_lesson_atom_coverage(lesson_uid=lesson_uid)
    for page in pages:
        try:
            orphan = seed_unassigned_atoms_as_new_blocks(
                lesson_uid=lesson_uid,
                page_index=int(page.page_index),
            )
            new_content_created += int(orphan.get("created_count") or 0)
        except ValueError:
            db.session.rollback()
    if new_content_created:
        reconcile = reconcile_lesson_atom_coverage(lesson_uid=lesson_uid)
    apply_anchored_block_display_names(lesson_uid=lesson_uid)
    coverage = reconcile.get("coverage") or compute_lesson_atom_coverage(
        lesson_id=lesson_id
    )
    if seed_errors and created_total == 0:
        _set_segment(lesson_uid, "attributes", "error")
        raise ValueError("；".join(seed_errors))
    if float(coverage.get("coverage_rate") or 0) < ATOM_COVERAGE_MIN:
        _set_segment(lesson_uid, "attributes", "error")
        pct = int(float(coverage.get("coverage_rate") or 0) * 100)
        raise ValueError(
            f"原子入块覆盖率 {pct}% 低于 {int(ATOM_COVERAGE_MIN * 100)}% 阈值"
            + (f"；{'; '.join(seed_errors)}" if seed_errors else "")
        )
    _set_segment(lesson_uid, "attributes", "done")

    _set_segment(lesson_uid, "anchor", "running")
    anchor_errors: list[str] = []
    anchor_updated = 0
    for page in pages:
        try:
            result = anchor_existing_blocks_on_page(
                lesson_uid=lesson_uid,
                new_page_index=int(page.page_index),
                old_page_index=int(page.page_index),
                replace_existing=True,
            )
            anchor_updated += int(result.get("updated_count") or 0)
        except ValueError as exc:
            db.session.rollback()
            anchor_errors.append(f"第{page.page_index}页锚定: {exc}")

    coverage = compute_lesson_atom_coverage(lesson_id=lesson_id)
    anchored = int(coverage.get("anchored_block_count") or 0)
    block_count = int(coverage.get("block_count") or 0)
    if anchor_updated == 0 and block_count > 0:
        _set_segment(lesson_uid, "anchor", "error")
        raise ValueError(
            "未能锚定任何区块"
            + (f"；{'; '.join(anchor_errors)}" if anchor_errors else "")
        )
    if anchored < block_count:
        _set_segment(lesson_uid, "anchor", "error")
        raise ValueError(
            f"仅 {anchored}/{block_count} 个区块完成锚定"
            + (f"；{'; '.join(anchor_errors)}" if anchor_errors else "")
        )
    if int(coverage.get("duplicate_count") or 0) > 0:
        _set_segment(lesson_uid, "anchor", "error")
        raise ValueError(
            f"仍有 {coverage['duplicate_count']} 个原子重复绑定到多个区块"
        )
    _set_segment(lesson_uid, "anchor", "done")

    from .annotate.anchor_multi import enrich_lesson_ordered_multi_anchors

    enrich_lesson_ordered_multi_anchors(lesson_uid=lesson_uid)
    db.session.flush()

    _set_segment(lesson_uid, "validate", "running")
    new_blocks = Block.query.filter_by(lesson_id=lesson_id).all()
    match = get_primary_lesson_match(lesson_id)
    old_blocks: list[Block] = []
    if match and match.old_lesson_id:
        old_blocks = Block.query.filter_by(lesson_id=match.old_lesson_id).all()
    suspicious = run_anchor_validation(new_blocks, old_blocks=old_blocks)
    mark_suspicious_blocks(new_blocks, suspicious)
    db.session.flush()
    _set_segment(lesson_uid, "validate", "done")

    _set_segment(lesson_uid, "edit_ready", "running")
    block_dicts = _finalize_block_metadata(
        lesson_id=lesson_id,
        source_path="old_mirror",
        ai_step="edit_ready",
    )
    db.session.commit()
    _set_segment(lesson_uid, "edit_ready", "done")

    return {
        "created_total": created_total,
        "block_dicts": block_dicts,
        "suspicious": suspicious,
        "path_info": path_info,
        "atom_coverage": coverage,
        "reconcile": reconcile,
        "rebalance": rebalance,
        "new_content_created": new_content_created,
        "seed_errors": seed_errors,
        "anchor_errors": anchor_errors,
    }


def _run_cluster_after_prepare(
    *,
    lesson_uid: str,
    lesson_id: str,
    path_info: dict,
) -> dict:
    """主题聚类路径：④ 按聚类建块；无旧课锚定。"""
    from .block_pipeline_prepare import get_page_prepare

    _set_segment(lesson_uid, "attributes", "running")
    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    created_total = 0
    page_total = max(len(pages), 1)
    for idx, page in enumerate(pages):
        sub = (idx + 0.5) / page_total
        with _block_lock:
            job = _block_jobs.get(lesson_uid)
            if job:
                job["progress"] = round(compute_block_progress("attributes", sub) * 100)
        ctx = get_page_prepare(lesson_uid, int(page.page_index))
        if not ctx:
            continue
        try:
            result = seed_blocks_from_page_clusters(
                lesson_uid=lesson_uid,
                page_index=int(page.page_index),
                page_ctx=ctx,
                replace_existing=False,
            )
            created_total += int(result.get("created_count") or 0)
        except ValueError:
            db.session.rollback()
    _set_segment(lesson_uid, "attributes", "done")

    _set_segment(lesson_uid, "anchor", "skip")

    _set_segment(lesson_uid, "validate", "running")
    new_blocks = Block.query.filter_by(lesson_id=lesson_id).all()
    suspicious = run_anchor_validation(new_blocks, old_blocks=[])
    mark_suspicious_blocks(new_blocks, suspicious)
    db.session.flush()
    _set_segment(lesson_uid, "validate", "done")

    _set_segment(lesson_uid, "edit_ready", "running")
    block_dicts = _finalize_block_metadata(
        lesson_id=lesson_id,
        source_path="new_cluster",
        ai_step="edit_ready",
    )
    db.session.commit()
    _set_segment(lesson_uid, "edit_ready", "done")

    return {
        "created_total": created_total,
        "block_dicts": block_dicts,
        "suspicious": suspicious,
        "path_info": path_info,
    }


def _run_block_pipeline(*, lesson_uid: str, lesson_id: str) -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type="new")
    path_info = should_use_old_mirror_path(new_lesson_id=lesson_id)
    _patch_job(lesson_uid, path_info=path_info)

    try:
        _run_prepare_steps(
            lesson_uid=lesson_uid,
            lesson_id=lesson_id,
            lesson_name=les.lesson_name or "",
        )
        if path_info["path"] == "old_mirror":
            return _run_mirror_after_prepare(
                lesson_uid=lesson_uid,
                lesson_id=lesson_id,
                path_info=path_info,
            )
        return _run_cluster_after_prepare(
            lesson_uid=lesson_uid,
            lesson_id=lesson_id,
            path_info=path_info,
        )
    finally:
        clear_lesson_prepare(lesson_uid)


def _run_old_mirror_pipeline(*, lesson_uid: str, lesson_id: str) -> dict:
    """兼容旧调用名。"""
    return _run_block_pipeline(lesson_uid=lesson_uid, lesson_id=lesson_id)


def _run_block_pipeline_worker(lesson_uid: str, app) -> None:
    started = time.time()
    with app.app_context():
        try:
            db.session.rollback()
            les = get_lesson_by_uid(lesson_uid, book_type="new")
            result = _run_block_pipeline(
                lesson_uid=lesson_uid,
                lesson_id=les.id,
            )
            duration = time.time() - started
            path = result["path_info"].get("path") or "old_mirror"
            summary = build_blocking_summary(
                result["block_dicts"],
                result["suspicious"],
                source_path=path,
                duration_seconds=duration,
                path_reason=result["path_info"].get("reason") or "",
            )
            if result.get("atom_coverage"):
                summary["atom_coverage"] = result["atom_coverage"]
            _patch_job(
                lesson_uid,
                state="done",
                label="建块完成",
                progress=100,
                summary=summary,
                created_total=result["created_total"],
                finished_at=datetime.utcnow().isoformat(),
            )
        except Exception as exc:
            db.session.rollback()
            db.session.remove()
            _patch_job(
                lesson_uid,
                state="error",
                label=_block_error_label(exc),
                error=str(exc),
                finished_at=datetime.utcnow().isoformat(),
            )


def start_block_pipeline(lesson_uid: str, app) -> dict:
    uid = (lesson_uid or "").strip()
    with _block_lock:
        existing = _block_jobs.get(uid)
        if existing and existing.get("state") == "running":
            return copy.deepcopy(existing)

        job: dict[str, Any] = {
            "lesson_uid": uid,
            "state": "running",
            "label": "建块中…",
            "progress": 0,
            "segments": empty_block_segments(),
            "steps": BLOCK_PIPELINE_STEPS,
            "started_at": datetime.utcnow().isoformat(),
            "summary": None,
            "path_info": None,
            "error": None,
        }
        _block_jobs[uid] = job

    thread = threading.Thread(
        target=_run_block_pipeline_worker,
        args=(uid, app),
        daemon=True,
    )
    thread.start()
    return copy.deepcopy(_block_jobs[uid])


def block_pipeline_path_check(lesson_uid: str) -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type="new")
    info = should_use_old_mirror_path(new_lesson_id=les.id)
    return {"ok": True, "lesson_uid": lesson_uid, **info}


def block_pipeline_job_payload(lesson_uid: str) -> dict:
    job = get_block_pipeline_job(lesson_uid)
    if not job:
        return {"ok": True, "job": None}
    payload: dict[str, Any] = {"ok": True, "job": job}
    if job.get("state") in ("done", "error"):
        payload["workspace"] = build_new_annotate_workspace(lesson_uid=lesson_uid)
        payload["workspace"]["block_pipeline"] = {
            "segments": job.get("segments"),
            "path_info": job.get("path_info"),
            "summary": job.get("summary"),
            "state": job.get("state"),
            "error": job.get("error"),
        }
    return payload


def _derive_legacy_block_segments(
    *,
    blocks: list,
    path_info: dict[str, Any],
    lesson: Lesson,
) -> dict[str, str]:
    """从 blocks 表真实字段推断 legacy 建块进度（不假装 column/topic/balance 已完成）。"""
    segs = empty_block_segments()
    if not blocks:
        return segs

    segs["attributes"] = "done"
    bc = len(blocks)
    anchored = sum(
        1 for b in blocks if (b.metadata_json or {}).get("anchor_old_refs")
    )
    use_mirror = path_info.get("path") == "old_mirror"

    if not use_mirror:
        segs["anchor"] = "skip"
    elif anchored >= bc:
        segs["anchor"] = "done"
    elif anchored > 0:
        segs["anchor"] = "running"

    suspicious = any((b.metadata_json or {}).get("suspicious") for b in blocks)
    if segs["anchor"] in ("done", "skip"):
        segs["validate"] = "done" if not suspicious else "running"
        if lesson.blocks_locked_at:
            segs["edit_ready"] = "done"
        elif segs["validate"] == "done":
            segs["edit_ready"] = "running"
    return segs


def lesson_block_pipeline_dict(*, lesson_id: str, lesson_uid: str) -> dict:
    """静态建块进度（无运行中任务时从区块 metadata 推断）。"""
    job = get_block_pipeline_job(lesson_uid)
    if job:
        return {
            "state": job.get("state"),
            "segments": job.get("segments") or empty_block_segments(),
            "progress": job.get("progress") or 0,
            "label": job.get("label") or "",
            "path_info": job.get("path_info"),
            "summary": job.get("summary"),
            "error": job.get("error"),
        }

    blocks = Block.query.filter_by(lesson_id=lesson_id).all()
    segs = empty_block_segments()
    if not blocks:
        path_info = should_use_old_mirror_path(new_lesson_id=lesson_id)
        return {
            "state": "idle",
            "segments": segs,
            "progress": 0,
            "label": "",
            "path_info": path_info,
            "summary": None,
            "error": None,
        }

    has_pipeline = any((b.metadata_json or {}).get("block_pipeline") for b in blocks)
    if has_pipeline:
        for key in ("column_detect", "topic_cluster", "balance", "attributes", "anchor", "validate", "edit_ready"):
            segs[key] = "done"
        source = "old_mirror"
        for b in blocks:
            bp = (b.metadata_json or {}).get("block_pipeline") or {}
            if bp.get("source_path"):
                source = bp["source_path"]
                break
        suspicious = [
            {
                "block_code": b.block_code,
                "reason": (b.metadata_json or {}).get("suspicious_reason") or "",
            }
            for b in blocks
            if (b.metadata_json or {}).get("suspicious")
        ]
        summary = build_blocking_summary(
            [_block_dict(b) for b in blocks],
            suspicious,
            source_path=source,
        )
        return {
            "state": "done",
            "segments": segs,
            "progress": 100,
            "label": "建块完成",
            "path_info": should_use_old_mirror_path(new_lesson_id=lesson_id),
            "summary": summary,
            "error": None,
        }

    return {
        "state": "legacy",
        "segments": _derive_legacy_block_segments(
            blocks=blocks,
            path_info=should_use_old_mirror_path(new_lesson_id=lesson_id),
            lesson=Lesson.query.get(lesson_id) or Lesson(id=lesson_id),
        ),
        "progress": 0,
        "label": "历史建块（无 pipeline 元数据）",
        "path_info": should_use_old_mirror_path(new_lesson_id=lesson_id),
        "summary": None,
        "error": None,
    }


_block_batch_jobs: dict[str, dict[str, Any]] = {}
_block_batch_lock = threading.Lock()


def get_block_batch_job(volume_code: str) -> dict | None:
    code = (volume_code or "").strip()
    with _block_batch_lock:
        job = _block_batch_jobs.get(code)
        if not job:
            return None
        return copy.deepcopy(job)


def _patch_block_batch_lesson(volume_code: str, lesson_uid: str, **fields: Any) -> None:
    with _block_batch_lock:
        job = _block_batch_jobs.get(volume_code)
        if not job:
            return
        prog = job["lessons"].setdefault(lesson_uid, {})
        prog.update(fields)


def _block_batch_cancel_requested(volume_code: str) -> bool:
    with _block_batch_lock:
        job = _block_batch_jobs.get(volume_code)
        return bool(job and job.get("cancel_requested"))


def _lesson_needs_block_pipeline(
    *,
    les: Lesson,
    volume_code: str,
    only_pending: bool,
) -> bool:
    from ..course_match.service import build_hint_for_lesson
    from .lesson_analysis import lesson_analysis_dict
    from .volumes import _anchored_block_count

    if build_hint_for_lesson(volume_code=volume_code, lesson_uid=les.lesson_uid) == "fully_new":
        return False
    if LessonPage.query.filter_by(lesson_id=les.id).count() <= 0:
        return False
    analysis = lesson_analysis_dict(lesson_id=les.id, lesson_uid=les.lesson_uid)
    if not analysis.get("ready_for_blocks"):
        return False
    block_count = Block.query.filter_by(lesson_id=les.id).count()
    anchored = _anchored_block_count(les.id)
    if not only_pending:
        return True
    if block_count <= 0:
        return True
    return anchored < block_count


def _run_block_batch(volume_code: str, lesson_uids: list[str], app) -> None:
    from .volumes import get_new_volume_by_code

    with app.app_context():
        volume = get_new_volume_by_code(volume_code)
        uid_set = set(lesson_uids)
        lessons = filter_master_class_lessons(
            order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
        )
        if uid_set:
            lessons = [les for les in lessons if les.lesson_uid in uid_set]

        for les in lessons:
            if _block_batch_cancel_requested(volume_code):
                _patch_block_batch_lesson(
                    volume_code,
                    les.lesson_uid,
                    state="cancelled",
                    label="已取消",
                )
                continue
            if not _lesson_needs_block_pipeline(
                les=les,
                volume_code=volume_code,
                only_pending=True,
            ):
                _patch_block_batch_lesson(
                    volume_code,
                    les.lesson_uid,
                    state="skip",
                    label="无需建块",
                )
                continue

            _patch_block_batch_lesson(
                volume_code,
                les.lesson_uid,
                state="running",
                label="建块中…",
            )
            with _block_lock:
                _block_jobs[les.lesson_uid] = {
                    "lesson_uid": les.lesson_uid,
                    "state": "running",
                    "label": "建块中…",
                    "progress": 0,
                    "segments": empty_block_segments(),
                    "steps": BLOCK_PIPELINE_STEPS,
                    "started_at": datetime.utcnow().isoformat(),
                    "summary": None,
                    "path_info": None,
                    "error": None,
                }
            _batch_volume_context[les.lesson_uid] = volume_code
            try:
                db.session.rollback()
                result = _run_block_pipeline(
                    lesson_uid=les.lesson_uid,
                    lesson_id=les.id,
                )
                job = get_block_pipeline_job(les.lesson_uid)
                _patch_block_batch_lesson(
                    volume_code,
                    les.lesson_uid,
                    state="done",
                    label="建块完成",
                    segments=(job or {}).get("segments"),
                    created_total=result.get("created_total"),
                )
            except Exception as exc:
                db.session.rollback()
                db.session.remove()
                _patch_block_batch_lesson(
                    volume_code,
                    les.lesson_uid,
                    state="error",
                    label=_block_error_label(exc),
                )
            finally:
                _batch_volume_context.pop(les.lesson_uid, None)
            if _block_batch_cancel_requested(volume_code):
                break

        with _block_batch_lock:
            job = _block_batch_jobs.get(volume_code)
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


def cancel_block_batch(volume_code: str) -> dict:
    code = (volume_code or "").strip()
    with _block_batch_lock:
        job = _block_batch_jobs.get(code)
        if not job or job.get("status") != "running":
            return {
                "ok": True,
                "volume_code": code,
                "status": "idle",
                "message": "没有运行中的批量建块",
            }
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        for prog in job.get("lessons", {}).values():
            if prog.get("state") == "queued":
                prog["state"] = "cancelled"
                prog["label"] = "已取消"
        out = copy.deepcopy(job)
        out["ok"] = True
        out["message"] = "已请求取消：当前课完成后停止"
        return out


def start_block_batch(
    *,
    volume_code: str,
    lesson_uids: list[str] | None = None,
    only_pending: bool = True,
    app,
) -> dict:
    from .volumes import get_new_volume_by_code

    volume = get_new_volume_by_code(volume_code)
    lessons = filter_master_class_lessons(
        order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    )
    if lesson_uids:
        want = set(lesson_uids)
        lessons = [les for les in lessons if les.lesson_uid in want]

    targets = [
        les
        for les in lessons
        if _lesson_needs_block_pipeline(
            les=les,
            volume_code=volume_code,
            only_pending=only_pending,
        )
    ]

    if not targets:
        return {
            "ok": True,
            "volume_code": volume_code,
            "status": "idle",
            "message": "没有需要建块/锚定的课时",
            "lesson_count": 0,
        }

    with _block_batch_lock:
        existing = _block_batch_jobs.get(volume_code)
        if existing and existing.get("status") == "running":
            return {"ok": True, "volume_code": volume_code, **existing}

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
                    "segments": empty_block_segments(),
                }
                for les in targets
            },
        }
        _block_batch_jobs[volume_code] = job

    threading.Thread(
        target=_run_block_batch,
        args=(volume_code, [les.lesson_uid for les in targets], app),
        daemon=True,
    ).start()
    return copy.deepcopy(_block_batch_jobs[volume_code])
