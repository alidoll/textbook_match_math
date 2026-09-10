# -*- coding: utf-8 -*-
"""教材库整册 OCR（+可选建块）后台任务：课级串行、可离开页面继续跑。"""
from __future__ import annotations

import copy
import logging
import threading
import time
from datetime import datetime
from typing import Any

from ...extensions import db
from ...models import Lesson, LessonPage, Volume
from ...query.lesson_order import order_lessons_query
from .lesson_work import build_library_lesson_blocks, ocr_library_lesson_all_pages

logger = logging.getLogger(__name__)

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_cancel: set[str] = set()


def _pipeline_label(*, with_blocks: bool) -> str:
    return "整册 OCR+建块" if with_blocks else "整册 OCR"


def _lesson_progress_default() -> dict[str, Any]:
    return {
        "state": "pending",
        "label": "排队中…",
        "text": "pending",
        "images": "pending",
        "blocks": "pending",
        "page_count": 0,
        "block_count": 0,
    }


def get_library_volume_ocr_job(volume_code: str) -> dict[str, Any] | None:
    code = (volume_code or "").strip()
    with _lock:
        job = _jobs.get(code)
        if not job:
            return None
        return copy.deepcopy(job)


def cancel_library_volume_ocr_job(volume_code: str) -> bool:
    code = (volume_code or "").strip()
    with _lock:
        if code not in _jobs:
            return False
        _cancel.add(code)
        job = _jobs[code]
        if job.get("running"):
            job["message"] = "正在停止…"
        else:
            del _jobs[code]
            _cancel.discard(code)
        return True


def _patch_lesson(volume_code: str, lesson_uid: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(volume_code)
        if not job:
            return
        prog = job["lessons"].setdefault(lesson_uid, _lesson_progress_default())
        prog.update(fields)


def _lessons_with_page_images(volume: Volume) -> list[Lesson]:
    out: list[Lesson] = []
    for les in order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all():
        if LessonPage.query.filter_by(lesson_id=les.id).first():
            out.append(les)
    return out


def _filter_lessons_by_unit(lessons: list[Lesson], only_unit: str | None) -> list[Lesson]:
    """only_unit: '13' / 'U13' → lesson_uid 含 -U13-。"""
    raw = (only_unit or "").strip()
    if not raw:
        return lessons
    token = raw.upper()
    if not token.startswith("U"):
        token = f"U{token}"
    needle = f"-{token}-"
    return [les for les in lessons if needle in (les.lesson_uid or "").upper()]


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _run_job(
    volume_code: str,
    *,
    skip_cached: bool,
    with_blocks: bool,
    replace_existing_blocks: bool,
    only_unit: str | None = None,
    app,
) -> None:
    t0 = time.monotonic()
    errors: list[str] = []
    done = 0
    pipe = _pipeline_label(with_blocks=with_blocks)
    try:
        with app.app_context():
            volume = Volume.query.filter_by(volume_code=volume_code).first()
            if not volume:
                raise ValueError(f"未找到册次 {volume_code}")
            lessons = _filter_lessons_by_unit(
                _lessons_with_page_images(volume), only_unit
            )
            if not lessons:
                raise ValueError(
                    f"尚无带页图的课时" + (f"（only_unit={only_unit}）" if only_unit else "，请先生成页图")
                )

            total = len(lessons)
            with _lock:
                job = _jobs.get(volume_code)
                if job:
                    job["lesson_total"] = total
                    job["with_blocks"] = with_blocks
                    job["only_unit"] = only_unit or None
                    job["message"] = f"{pipe} 已启动：共 {total} 课" + (
                        f"（第{only_unit}章）" if only_unit else ""
                    )
                    # 仅跑子集时，进度表只保留这些课
                    job["lessons"] = {
                        les.lesson_uid: job["lessons"].get(
                            les.lesson_uid, _lesson_progress_default()
                        )
                        for les in lessons
                    }

            from concurrent.futures import Future, ThreadPoolExecutor

            from ..llm.config import lib_volume_ocr_pipeline_blocks

            pipeline = bool(with_blocks and lib_volume_ocr_pipeline_blocks())
            prefetch_pool = ThreadPoolExecutor(max_workers=1) if pipeline else None
            prefetch_fut: Future | None = None
            prefetch_uid: str | None = None
            prefetch_label: str | None = None

            def _run_lesson_ocr(lesson_uid: str) -> dict[str, Any]:
                with app.app_context():
                    return ocr_library_lesson_all_pages(
                        lesson_uid=lesson_uid,
                        phases=["text", "images"],
                        skip_cached=skip_cached,
                    )

            try:
                for i, les in enumerate(lessons):
                    if volume_code in _cancel:
                        break
                    uid = les.lesson_uid
                    label = (
                        f"{les.lesson_no or ''} {les.lesson_name or ''}".strip() or uid
                    )
                    _patch_lesson(
                        volume_code,
                        uid,
                        state="running",
                        label=f"{label} · OCR",
                        text="running",
                        images="running",
                        blocks="pending" if with_blocks else "skipped",
                    )
                    with _lock:
                        job = _jobs.get(volume_code)
                        if job:
                            elapsed = _format_elapsed(time.monotonic() - t0)
                            job["message"] = (
                                f"{pipe}：{label}（{i + 1}/{total} · OCR）· 已用 {elapsed}"
                            )
                            job["lesson_done"] = i

                    try:
                        if prefetch_fut is not None and prefetch_uid == uid:
                            result = prefetch_fut.result()
                            prefetch_fut = None
                            prefetch_uid = None
                            prefetch_label = None
                        else:
                            if prefetch_fut is not None:
                                try:
                                    prefetch_fut.result()
                                except Exception:
                                    logger.exception(
                                        "volume pipeline prefetch failed %s",
                                        prefetch_uid,
                                    )
                                prefetch_fut = None
                                prefetch_uid = None
                                prefetch_label = None
                            result = _run_lesson_ocr(uid)

                        page_count = int(result.get("page_count") or 0)
                        _patch_lesson(
                            volume_code,
                            uid,
                            text="done",
                            images="done",
                            page_count=page_count,
                            label=f"{label} · OCR 完成",
                        )

                        # D：本课建块前先把下一课 OCR 丢到后台
                        if (
                            pipeline
                            and prefetch_pool is not None
                            and i + 1 < total
                            and volume_code not in _cancel
                        ):
                            nxt = lessons[i + 1]
                            prefetch_uid = nxt.lesson_uid
                            prefetch_label = (
                                f"{nxt.lesson_no or ''} {nxt.lesson_name or ''}".strip()
                                or prefetch_uid
                            )
                            _patch_lesson(
                                volume_code,
                                prefetch_uid,
                                state="running",
                                label=f"{prefetch_label} · OCR(预跑)",
                                text="running",
                                images="running",
                                blocks="pending",
                            )
                            prefetch_fut = prefetch_pool.submit(
                                _run_lesson_ocr, prefetch_uid
                            )

                        block_count = 0
                        if with_blocks:
                            if volume_code in _cancel:
                                break
                            _patch_lesson(
                                volume_code,
                                uid,
                                state="running",
                                label=f"{label} · 建块",
                                blocks="running",
                            )
                            with _lock:
                                job = _jobs.get(volume_code)
                                if job:
                                    elapsed = _format_elapsed(time.monotonic() - t0)
                                    extra = (
                                        f" · 预跑 {prefetch_label}"
                                        if prefetch_fut is not None and prefetch_label
                                        else ""
                                    )
                                    job["message"] = (
                                        f"{pipe}：{label}（{i + 1}/{total} · 建块）"
                                        f"{extra} · 已用 {elapsed}"
                                    )
                            blocks_out = build_library_lesson_blocks(
                                lesson_uid=uid,
                                replace_existing=replace_existing_blocks,
                            )
                            block_count = int(
                                blocks_out.get("block_count")
                                or blocks_out.get("created_count")
                                or 0
                            )
                            _patch_lesson(
                                volume_code,
                                uid,
                                blocks="done",
                                block_count=block_count,
                            )

                        _patch_lesson(
                            volume_code,
                            uid,
                            state="done",
                            label="完成",
                            page_count=page_count,
                            block_count=block_count,
                        )
                        done += 1
                    except Exception as exc:
                        logger.exception("volume pipeline lesson failed %s", uid)
                        db.session.rollback()
                        errors.append(f"{label}: {exc}")
                        _patch_lesson(
                            volume_code,
                            uid,
                            state="error",
                            label=str(exc)[:120],
                            text="error",
                            images="error",
                            blocks="error" if with_blocks else "skipped",
                        )
            finally:
                if prefetch_fut is not None:
                    try:
                        prefetch_fut.result()
                    except Exception:
                        logger.exception(
                            "volume pipeline prefetch drain failed %s", prefetch_uid
                        )
                if prefetch_pool is not None:
                    prefetch_pool.shutdown(wait=True)

            elapsed = time.monotonic() - t0
            cancelled = volume_code in _cancel
            with _lock:
                job = _jobs.get(volume_code)
                if not job:
                    return
                job["running"] = False
                job["finished_at"] = datetime.now().isoformat(timespec="seconds")
                job["elapsed_seconds"] = round(elapsed, 1)
                job["elapsed_label"] = _format_elapsed(elapsed)
                job["lesson_done"] = done
                job["errors"] = errors
                if cancelled:
                    job["status"] = "cancelled"
                    job["message"] = (
                        f"{pipe} 已停止：{done}/{total} 课完成 · 已用 {job['elapsed_label']}"
                    )
                elif errors:
                    job["status"] = "done_with_errors"
                    job["message"] = (
                        f"{pipe} 完成（{len(errors)} 课失败）："
                        f"{done}/{total} 课 · 总耗时 {job['elapsed_label']}"
                    )
                else:
                    job["status"] = "done"
                    job["message"] = (
                        f"{pipe} 完成：{total} 课 · 总耗时 {job['elapsed_label']}"
                    )
                job["summary"] = {
                    "lesson_total": total,
                    "lesson_done": done,
                    "error_count": len(errors),
                    "elapsed_label": job["elapsed_label"],
                    "with_blocks": with_blocks,
                }
    except Exception as exc:
        logger.exception("volume pipeline job failed %s", volume_code)
        with _lock:
            job = _jobs.get(volume_code)
            if job:
                job["running"] = False
                job["status"] = "error"
                job["finished_at"] = datetime.now().isoformat(timespec="seconds")
                job["message"] = f"{pipe} 失败：{exc}"
                job["last_error"] = str(exc)
    finally:
        with _lock:
            _cancel.discard(volume_code)
        db.session.remove()


def start_library_volume_ocr(
    volume_code: str,
    *,
    skip_cached: bool = True,
    with_blocks: bool = True,
    replace_existing_blocks: bool = True,
    only_unit: str | None = None,
    app,
) -> dict[str, Any]:
    code = (volume_code or "").strip()
    if not code:
        raise ValueError("缺少 volume_code")

    with _lock:
        existing = _jobs.get(code)
        if existing and existing.get("running"):
            snap = copy.deepcopy(existing)
            snap["already_running"] = True
            return snap

    volume = Volume.query.filter_by(volume_code=code).first()
    if not volume:
        raise ValueError(f"未找到册次 {code}")
    lessons = _filter_lessons_by_unit(_lessons_with_page_images(volume), only_unit)
    if not lessons:
        raise ValueError(
            f"尚无带页图的课时" + (f"（only_unit={only_unit}）" if only_unit else "，请先生成页图")
        )

    pipe = _pipeline_label(with_blocks=with_blocks)
    unit_note = f"（第{only_unit}章）" if only_unit else ""
    job: dict[str, Any] = {
        "volume_code": code,
        "running": True,
        "status": "running",
        "with_blocks": bool(with_blocks),
        "only_unit": (only_unit or "").strip() or None,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "elapsed_seconds": 0,
        "elapsed_label": "0:00",
        "lesson_total": len(lessons),
        "lesson_done": 0,
        "message": f"{pipe} 排队中：共 {len(lessons)} 课{unit_note}",
        "summary": None,
        "errors": [],
        "lessons": {les.lesson_uid: _lesson_progress_default() for les in lessons},
    }
    with _lock:
        _jobs[code] = job
        _cancel.discard(code)

    thread = threading.Thread(
        target=_run_job,
        args=(code,),
        kwargs={
            "skip_cached": skip_cached,
            "with_blocks": bool(with_blocks),
            "replace_existing_blocks": bool(replace_existing_blocks),
            "only_unit": (only_unit or "").strip() or None,
            "app": app,
        },
        name=f"lib-vol-ocr-{code}",
        daemon=True,
    )
    thread.start()
    return copy.deepcopy(job)
