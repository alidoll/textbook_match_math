"""整册建块后台流水线：OCR → 整理 → 建块（默认可三阶段并行，部分册次单课串行）。"""
from __future__ import annotations

import copy
import logging
import os
import queue
import threading
import time
from datetime import datetime
from typing import Any, Literal

from ...extensions import db
from ...models import Block, CoursewareSlide, Lesson, LessonPage
from ...query.lesson_order import order_lessons_query
from .annotate.ai_bootstrap import ai_bootstrap_lesson, assess_lesson_bootstrap_readiness
from .annotate.ai_curate_atoms import ai_curate_lesson
from .annotate.atoms import extract_atoms_for_page
from .annotate.blocks import unlock_lesson_blocks
from .volumes import get_old_volume_by_code

logger = logging.getLogger(__name__)

BatchMode = Literal["skip", "rebuild"]
PipelineStrategy = Literal["parallel", "sequential"]

# 湘科四下：单课完成 OCR→整理→建块后再处理下一课（便于与并行模式对比耗时）
SEQUENTIAL_PIPELINE_VOLUME_CODES = frozenset({"XK-4X-OLD"})

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def resolve_pipeline_strategy(
    volume_code: str,
    *,
    override: str | None = None,
) -> PipelineStrategy:
    if override in ("parallel", "sequential"):
        return override  # type: ignore[return-value]
    env = (os.getenv("OLD_BLOCK_BATCH_PIPELINE") or "").strip().lower()
    if env in ("parallel", "sequential"):
        return env  # type: ignore[return-value]
    code = (volume_code or "").strip().upper()
    if code in SEQUENTIAL_PIPELINE_VOLUME_CODES:
        return "sequential"
    return "parallel"


def _lesson_progress_default() -> dict[str, Any]:
    return {
        "state": "pending",
        "label": "排队中…",
        "ocr": "pending",
        "curate": "pending",
        "seed": "pending",
        "ocr_page": 0,
        "ocr_total": 0,
        "block_count": None,
    }


def _normalize_lesson_progress(prog: dict[str, Any]) -> dict[str, Any]:
    out = dict(prog)
    if out.get("state") == "done":
        for key in ("ocr", "curate", "seed"):
            if out.get(key) != "skip":
                out[key] = "done"
    return out


def get_block_batch_job(volume_code: str) -> dict[str, Any] | None:
    code = (volume_code or "").strip()
    with _lock:
        job = _jobs.get(code)
        if not job:
            return None
        snap = copy.deepcopy(job)
    snap["lessons"] = {
        uid: _normalize_lesson_progress(prog)
        for uid, prog in (snap.get("lessons") or {}).items()
    }
    return snap


def _patch_lesson(volume_code: str, lesson_uid: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(volume_code)
        if not job:
            return
        prog = job["lessons"].setdefault(lesson_uid, _lesson_progress_default())
        prog.update(fields)


def _set_error(volume_code: str, lesson_uid: str, stage: str, message: str) -> None:
    patch: dict[str, Any] = {
        "state": "error",
        "label": (message or "失败")[:120],
    }
    if stage == "ocr":
        patch["ocr"] = "error"
    elif stage == "curate":
        patch["curate"] = "error"
    else:
        patch["seed"] = "error"
    _patch_lesson(volume_code, lesson_uid, **patch)


def _do_ocr(volume_code: str, lesson_uid: str, bootstrap_mode: str) -> None:
    if bootstrap_mode in ("curate_and_seed", "seed_only"):
        _patch_lesson(
            volume_code,
            lesson_uid,
            state="uploading",
            ocr="skip",
            curate="skip" if bootstrap_mode == "seed_only" else "pending",
            seed="pending",
            label="跳过 OCR/整理" if bootstrap_mode == "seed_only" else "跳过 OCR",
        )
        return

    from ...services.lesson_lookup import get_lesson_by_uid

    les = get_lesson_by_uid(lesson_uid, book_type="old")
    pages = (
        LessonPage.query.filter_by(lesson_id=les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    if not pages:
        raise ValueError("本课无教材页，请先解析 PDF")

    total = len(pages)
    _patch_lesson(
        volume_code,
        lesson_uid,
        state="uploading",
        ocr="running",
        curate="pending",
        seed="pending",
        ocr_page=0,
        ocr_total=total,
        label=f"OCR 0/{total} 页",
    )
    for i, page in enumerate(pages):
        page_index = int(page.page_index)
        _patch_lesson(
            volume_code,
            lesson_uid,
            ocr_page=i,
            ocr_total=total,
            label=f"OCR 第 {page_index} 页（{i + 1}/{total}）…",
        )
        logger.info(
            "block_batch OCR start volume=%s lesson=%s page=%s (%s/%s)",
            volume_code,
            lesson_uid,
            page_index,
            i + 1,
            total,
        )
        extract_atoms_for_page(
            lesson_uid=lesson_uid,
            page_index=page_index,
            replace_page=True,
        )
        db.session.commit()
        _patch_lesson(
            volume_code,
            lesson_uid,
            ocr_page=i + 1,
            ocr_total=total,
            label=f"OCR {i + 1}/{total} 页",
        )
        logger.info(
            "block_batch OCR done volume=%s lesson=%s page=%s (%s/%s)",
            volume_code,
            lesson_uid,
            page_index,
            i + 1,
            total,
        )
    _patch_lesson(
        volume_code,
        lesson_uid,
        ocr="done",
        label="OCR 完成，等待整理",
    )


def _do_prematch(volume_code: str, lesson_uid: str, bootstrap_mode: str) -> None:
    if bootstrap_mode == "seed_only":
        return
    _patch_lesson(
        volume_code,
        lesson_uid,
        label="课件预匹配…",
    )
    from .annotate.atom_prematch_curate import atom_prematch_lesson

    atom_prematch_lesson(lesson_uid=lesson_uid, book_type="old")
    db.session.commit()
    _patch_lesson(
        volume_code,
        lesson_uid,
        label="预匹配完成，等待建块",
    )


def _do_curate(volume_code: str, lesson_uid: str, bootstrap_mode: str) -> None:
    if bootstrap_mode == "seed_only":
        _patch_lesson(
            volume_code,
            lesson_uid,
            curate="skip",
            seed="pending",
            label="跳过整理",
        )
        return

    _patch_lesson(
        volume_code,
        lesson_uid,
        curate="running",
        label="AI 整理…",
    )
    ai_curate_lesson(lesson_uid=lesson_uid, extract_first=False, book_type="old")
    db.session.commit()
    _patch_lesson(
        volume_code,
        lesson_uid,
        curate="done",
        label="整理完成，等待建块",
    )


def _do_seed(
    volume_code: str,
    lesson_uid: str,
    *,
    batch_mode: BatchMode,
    blocks_locked: bool,
) -> None:
    _patch_lesson(volume_code, lesson_uid, seed="running", label="AI 建块…")
    if batch_mode == "rebuild" and blocks_locked:
        _patch_lesson(volume_code, lesson_uid, label="解锁中…")
        unlock_lesson_blocks(lesson_uid=lesson_uid)
        db.session.commit()

    result = ai_bootstrap_lesson(
        lesson_uid=lesson_uid,
        extract_first=False,
        skip_curate=True,
        replace_blocks=True,
        book_type="old",
    )
    db.session.commit()

    from ...services.lesson_lookup import get_lesson_by_uid

    les = get_lesson_by_uid(lesson_uid, book_type="old")
    block_count = Block.query.filter_by(lesson_id=les.id).count()
    created = (result.get("blocks") or {}).get("created_count")
    if created is not None:
        block_count = max(block_count, int(created))

    prog = get_block_batch_job(volume_code)
    prev = (prog or {}).get("lessons", {}).get(lesson_uid, {})
    ocr = prev.get("ocr", "done")
    curate = prev.get("curate", "done")
    _patch_lesson(
        volume_code,
        lesson_uid,
        state="done",
        ocr=ocr if ocr != "pending" else "done",
        curate=curate if curate != "pending" else "done",
        seed="done",
        block_count=block_count,
        label=f"完成 · {block_count} 区块",
    )


def _safe_rollback() -> None:
    try:
        db.session.rollback()
    except RuntimeError:
        pass


def _finalize_job(
    volume_code: str,
    to_run: list[dict[str, Any]],
    failed: set[str],
    *,
    elapsed_seconds: float,
    lesson_timings: list[dict[str, Any]] | None = None,
) -> None:
    ok_count = len(to_run) - len(failed)
    skip_count = 0
    with _lock:
        job = _jobs.get(volume_code)
        if job:
            skip_count = sum(
                1 for p in job["lessons"].values() if p.get("state") == "skip"
            )
            job["summary"] = {
                "ok_count": ok_count,
                "skip_count": skip_count,
                "err_count": len(failed),
                "elapsed_seconds": round(elapsed_seconds, 1),
                "pipeline_strategy": job.get("pipeline_strategy", "parallel"),
                "lesson_timings": lesson_timings or [],
            }
            job["running"] = False
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _run_pipeline_sequential(
    volume_code: str,
    to_run: list[dict[str, Any]],
    batch_mode: BatchMode,
    app,
) -> None:
    failed: set[str] = set()
    lesson_timings: list[dict[str, Any]] = []
    t0 = time.monotonic()
    with app.app_context():
        try:
            for item in to_run:
                uid = item["lesson_uid"]
                bootstrap_mode = item.get("bootstrap_mode") or "full"
                les_t0 = time.monotonic()
                stage: Literal["ocr", "curate", "seed"] = "ocr"
                try:
                    _do_ocr(volume_code, uid, bootstrap_mode)
                    if bootstrap_mode != "seed_only":
                        stage = "curate"
                        _do_curate(volume_code, uid, bootstrap_mode)
                        _do_prematch(volume_code, uid, bootstrap_mode)
                    stage = "seed"
                    _do_seed(
                        volume_code,
                        uid,
                        batch_mode=batch_mode,
                        blocks_locked=bool(item.get("blocks_locked")),
                    )
                except Exception as exc:
                    _safe_rollback()
                    failed.add(uid)
                    _set_error(volume_code, uid, stage, str(exc))
                    continue
                elapsed = round(time.monotonic() - les_t0, 1)
                _patch_lesson(volume_code, uid, elapsed_seconds=elapsed)
                lesson_timings.append({"lesson_uid": uid, "elapsed_seconds": elapsed})
        finally:
            db.session.remove()
    _finalize_job(
        volume_code,
        to_run,
        failed,
        elapsed_seconds=time.monotonic() - t0,
        lesson_timings=lesson_timings,
    )


def _run_pipeline(
    volume_code: str,
    to_run: list[dict[str, Any]],
    batch_mode: BatchMode,
    app,
) -> None:
    t0 = time.monotonic()
    curate_q: queue.Queue[dict[str, Any]] = queue.Queue()
    seed_q: queue.Queue[dict[str, Any]] = queue.Queue()
    failed: set[str] = set()
    ocr_done = threading.Event()
    curate_done = threading.Event()

    def ocr_worker() -> None:
        with app.app_context():
            try:
                for item in to_run:
                    uid = item["lesson_uid"]
                    try:
                        _do_ocr(volume_code, uid, item.get("bootstrap_mode") or "full")
                        if uid not in failed:
                            curate_q.put(item)
                    except Exception as exc:
                        _safe_rollback()
                        failed.add(uid)
                        _set_error(volume_code, uid, "ocr", str(exc))
            finally:
                ocr_done.set()
                db.session.remove()

    def curate_worker() -> None:
        with app.app_context():
            try:
                while True:
                    try:
                        item = curate_q.get(timeout=0.25)
                    except queue.Empty:
                        if ocr_done.is_set() and curate_q.empty():
                            break
                        continue
                    uid = item["lesson_uid"]
                    if uid in failed:
                        continue
                    try:
                        _do_curate(volume_code, uid, item.get("bootstrap_mode") or "full")
                        if uid not in failed:
                            seed_q.put(item)
                    except Exception as exc:
                        _safe_rollback()
                        failed.add(uid)
                        _set_error(volume_code, uid, "curate", str(exc))
            finally:
                curate_done.set()
                db.session.remove()

    def seed_worker() -> None:
        with app.app_context():
            try:
                while True:
                    try:
                        item = seed_q.get(timeout=0.25)
                    except queue.Empty:
                        if curate_done.is_set() and seed_q.empty():
                            break
                        continue
                    uid = item["lesson_uid"]
                    if uid in failed:
                        continue
                    try:
                        _do_seed(
                            volume_code,
                            uid,
                            batch_mode=batch_mode,
                            blocks_locked=bool(item.get("blocks_locked")),
                        )
                    except Exception as exc:
                        _safe_rollback()
                        failed.add(uid)
                        _set_error(volume_code, uid, "seed", str(exc))
            finally:
                db.session.remove()

    workers = [
        threading.Thread(target=ocr_worker, name=f"bb-ocr-{volume_code}", daemon=True),
        threading.Thread(target=curate_worker, name=f"bb-curate-{volume_code}", daemon=True),
        threading.Thread(target=seed_worker, name=f"bb-seed-{volume_code}", daemon=True),
    ]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    _finalize_job(
        volume_code,
        to_run,
        failed,
        elapsed_seconds=time.monotonic() - t0,
    )


def cancel_block_batch_job(volume_code: str) -> bool:
    """清除内存中的整册建块任务（含卡死的 running 状态）。"""
    code = (volume_code or "").strip()
    with _lock:
        if code not in _jobs:
            return False
        del _jobs[code]
        return True


def start_volume_block_batch(
    volume_code: str,
    *,
    mode: BatchMode,
    app,
    pipeline: str | None = None,
) -> dict[str, Any]:
    code = (volume_code or "").strip()
    if not code:
        raise ValueError("缺少 volume_code")

    with _lock:
        existing = _jobs.get(code)
        if existing and existing.get("running"):
            raise ValueError("本册建块任务进行中，请稍候")

    strategy = resolve_pipeline_strategy(code, override=pipeline)
    volume = get_old_volume_by_code(code)
    lessons = order_lessons_query(
        Lesson.query.filter_by(volume_id=volume.id)
    ).all()

    job: dict[str, Any] = {
        "volume_code": code,
        "mode": mode,
        "pipeline_strategy": strategy,
        "running": True,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "summary": None,
        "lessons": {},
    }
    to_run: list[dict[str, Any]] = []

    for les in lessons:
        slide_count = CoursewareSlide.query.filter_by(lesson_id=les.id).count()
        if slide_count <= 0:
            continue
        uid = les.lesson_uid
        block_count = Block.query.filter_by(lesson_id=les.id).count()
        locked = bool(les.blocks_locked_at)
        will_skip = mode == "skip" and (block_count > 0 or locked)
        if will_skip:
            job["lessons"][uid] = {
                "state": "skip",
                "label": "已保存，跳过" if locked else "已有区块，跳过",
                "ocr": "skip",
                "curate": "skip",
                "seed": "skip",
                "ocr_page": 0,
                "ocr_total": 0,
                "block_count": block_count,
            }
        else:
            job["lessons"][uid] = _lesson_progress_default()
            readiness = assess_lesson_bootstrap_readiness(
                lesson_uid=uid,
                book_type="old",
            )
            to_run.append(
                {
                    "lesson_uid": uid,
                    "blocks_locked": locked,
                    "bootstrap_mode": readiness["recommended_mode"],
                }
            )

    with _lock:
        _jobs[code] = job

    def worker() -> None:
        with app.app_context():
            try:
                if strategy == "sequential":
                    _run_pipeline_sequential(code, to_run, mode, app)
                else:
                    _run_pipeline(code, to_run, mode, app)
            except Exception:
                _safe_rollback()
                with _lock:
                    j = _jobs.get(code)
                    if j:
                        j["running"] = False
                        j["finished_at"] = datetime.now().isoformat(timespec="seconds")
            finally:
                db.session.remove()

    threading.Thread(
        target=worker,
        name=f"block-batch-{code}",
        daemon=True,
    ).start()

    snap = get_block_batch_job(code)
    assert snap is not None
    return snap
