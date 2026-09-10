"""本课时 / 整册一键对比：按页跑 ①→②→③→④（有缓存跳过；可页级并行）。"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Literal

from flask import current_app

from ...extensions import db
from ...models import DiffLessonPair, Lesson, Volume
from .atom_compare import (
    compare_cached_page_text,
    load_page_atom_cache,
    run_diff_page_ocr_both,
)
from .page_image_compare import compare_cached_page_images
from .view_workspace import _resolve_diff_lesson, _resolve_old_lesson_for_new
from .volumes import get_diff_volume_by_code
from .workbook import mark_pair_ready_for_review

_log = logging.getLogger(__name__)

Scope = Literal["lesson", "volume"]


def _resolve_pipeline_old_vol(old_vol: Volume) -> Volume:
    """小科 DOLD 无 PDF：OCR/比对用旧库册；其他学科原样。"""
    from .xiaoke_view import is_xiaoke_volume, resolve_xiaoke_ocr_old_volume

    if is_xiaoke_volume(old_vol):
        return resolve_xiaoke_ocr_old_volume(old_vol)
    return old_vol

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _pipeline_page_workers() -> int:
    """整册/课时页级并发数。环境变量 DIFF_PIPELINE_PAGE_WORKERS，默认 5，上限 8。"""
    raw = (os.getenv("DIFF_PIPELINE_PAGE_WORKERS") or "5").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 5
    return max(1, min(n, 8))


def _format_duration(sec: float | int | None) -> str:
    s = max(0, int(round(float(sec or 0))))
    if s < 60:
        return f"{s} 秒"
    m, rem = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {rem} 秒" if rem else f"{m} 分钟"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分" if m else f"{h} 小时"


def _estimate_remaining_sec(
    *,
    pages_done: int,
    pages_total: int,
    page_seconds: list[float],
    workers: int,
    started_at: float,
) -> float:
    remain_pages = max(0, int(pages_total) - int(pages_done))
    if remain_pages <= 0:
        return 0.0
    w = max(1, int(workers or 1))
    samples = [float(x) for x in (page_seconds or []) if float(x) > 0]
    if samples:
        window = samples[-20:]
        avg = sum(window) / len(window)
        return avg * remain_pages / w
    elapsed = max(0.0, time.time() - float(started_at or time.time()))
    if pages_done <= 0 and elapsed < 5:
        return 120.0 * remain_pages / w
    if pages_done > 0 and elapsed > 0:
        return elapsed / pages_done * remain_pages
    return 120.0 * remain_pages / w


def _pair_prefix(old_code: str, new_code: str) -> str:
    return f"{old_code}::{new_code}"


def _job_key(
    old_code: str,
    new_code: str,
    *,
    scope: Scope,
    new_lesson_uid: str | None = None,
) -> str:
    prefix = _pair_prefix(old_code, new_code)
    if scope == "volume":
        return f"{prefix}::volume"
    uid = (new_lesson_uid or "").strip()
    return f"{prefix}::lesson::{uid}"


def _jobs_for_pair(old_code: str, new_code: str) -> list[dict[str, Any]]:
    prefix = _pair_prefix(old_code, new_code) + "::"
    with _lock:
        return [dict(j) for k, j in _jobs.items() if k.startswith(prefix)]


def get_pipeline_job(*, old_code: str, new_code: str) -> dict[str, Any] | None:
    """兼容旧前端：若仅有一个任务则返回它，否则返回聚合快照。"""
    snap = get_pipeline_snapshot(old_code=old_code, new_code=new_code)
    if not snap.get("jobs"):
        return None
    return snap


def get_pipeline_snapshot(*, old_code: str, new_code: str) -> dict[str, Any]:
    """本册全部一键任务快照：支持多课时并行。"""
    jobs = _jobs_for_pair(old_code, new_code)
    running = [j for j in jobs if (j.get("status") or "") == "running"]
    lessons: dict[str, dict[str, Any]] = {}

    def _merge_lesson(uid: str, info: dict[str, Any]) -> None:
        if not uid:
            return
        prev = lessons.get(uid)
        if not prev:
            lessons[uid] = dict(info)
            return
        pst, st = prev.get("status"), info.get("status")
        if st == "running" or (pst != "running" and int(info.get("done") or 0) >= int(prev.get("done") or 0)):
            lessons[uid] = dict(info)

    for j in jobs:
        for uid, info in (j.get("lessons") or {}).items():
            _merge_lesson(str(uid), info if isinstance(info, dict) else {})
        if j.get("scope") == "lesson" and j.get("new_lesson_uid"):
            uid = str(j["new_lesson_uid"])
            total = int(j.get("total") or 0) or 1
            pages_done = int(j.get("pages_done") or 0)
            st = j.get("status") or "pending"
            if st == "running":
                done = pages_done
                lesson_st = "running"
            elif st in ("done", "done_with_errors"):
                done = total
                lesson_st = "error" if st == "done_with_errors" else "done"
            elif st == "error":
                done = max(pages_done, int(j.get("current_index") or 0))
                lesson_st = "error"
            else:
                done = 0
                lesson_st = "pending"
            _merge_lesson(
                uid,
                {
                    "done": done,
                    "total": total,
                    "status": lesson_st,
                    "lesson_page_index": j.get("lesson_page_index") or 0,
                    "lesson_page_count": j.get("lesson_page_count") or 0,
                    "current_label": j.get("current_label") or "",
                    "page_phase": j.get("page_phase") or "",
                    "page_phase_label": j.get("page_phase_label") or "",
                },
            )
        elif (j.get("status") or "") == "running" and j.get("scope") == "volume":
            active_uids = j.get("active_lesson_uids") or []
            if not active_uids and j.get("current_new_lesson_uid"):
                active_uids = [j.get("current_new_lesson_uid")]
            for uid in active_uids:
                uid = str(uid or "")
                if not uid:
                    continue
                prev = lessons.get(uid) or {}
                _merge_lesson(
                    uid,
                    {
                        **prev,
                        "status": "running",
                        "lesson_page_index": j.get("lesson_page_index") or prev.get("lesson_page_index") or 0,
                        "lesson_page_count": j.get("lesson_page_count") or prev.get("lesson_page_count") or 0,
                        "page_phase": j.get("page_phase") or "",
                        "page_phase_label": j.get("page_phase_label") or "",
                        "current_label": j.get("current_label") or prev.get("current_label") or "",
                    },
                )

    volume_running = any(
        j.get("scope") == "volume" and j.get("status") == "running" for j in jobs
    )
    running_lesson_uids = [
        str(j.get("new_lesson_uid"))
        for j in running
        if j.get("scope") == "lesson" and j.get("new_lesson_uid")
    ]
    for j in running:
        started = float(j.get("started_at") or j.get("created_at") or time.time())
        pages_done = int(j.get("pages_done") or 0)
        pages_total = int(j.get("total") or 0)
        workers = int(j.get("workers") or 1)
        page_seconds = list(j.get("page_seconds") or [])
        remain = _estimate_remaining_sec(
            pages_done=pages_done,
            pages_total=pages_total,
            page_seconds=page_seconds,
            workers=workers,
            started_at=started,
        )
        elapsed = max(0.0, time.time() - started)
        j["elapsed_sec"] = int(round(elapsed))
        j["eta_remaining_sec"] = int(round(remain))
        j["eta_label"] = (
            f"预计还需约 {_format_duration(remain)}（已用 {_format_duration(elapsed)}）"
            if pages_total > 0
            else ""
        )

    eta_job = None
    for j in running:
        if j.get("scope") == "volume":
            eta_job = j
            break
    if eta_job is None and running:
        eta_job = max(running, key=lambda x: int(x.get("eta_remaining_sec") or 0))

    if running:
        status = "running"
        if len(running) == 1:
            message = running[0].get("message") or "一键对比进行中…"
        else:
            message = f"{len(running)} 个任务并行中…"
        if eta_job and eta_job.get("eta_label"):
            message = f"{message} · {eta_job['eta_label']}"
        focus = eta_job or running[0]
        elapsed_sec = int(focus.get("elapsed_sec") or 0)
        elapsed_label = focus.get("elapsed_label") or (
            _format_duration(elapsed_sec) if elapsed_sec else ""
        )
        pages_done = int(focus.get("pages_done") or 0)
        pages_total = int(focus.get("total") or 0)
        workers = int(focus.get("workers") or 1)
        eta_remaining_sec = int(focus.get("eta_remaining_sec") or 0)
        eta_label = focus.get("eta_label") or ""
    elif jobs:
        latest = max(jobs, key=lambda x: float(x.get("updated_at") or 0))
        status = latest.get("status") or "done"
        message = latest.get("message") or ""
        focus = latest
        # 终态任务补齐实际耗时展示字段
        elapsed_sec = int(latest.get("elapsed_sec") or 0)
        if not elapsed_sec and latest.get("started_at") and latest.get("finished_at"):
            elapsed_sec = int(round(float(latest["finished_at"]) - float(latest["started_at"])))
        elapsed_label = latest.get("elapsed_label") or (
            _format_duration(elapsed_sec) if elapsed_sec else ""
        )
        if elapsed_label and "实际耗时" not in message:
            message = f"{message} · 实际耗时 {elapsed_label}".strip(" ·")
        pages_done = int(latest.get("pages_done") or latest.get("total") or 0)
        pages_total = int(latest.get("total") or 0)
        workers = int(latest.get("workers") or 1)
        eta_remaining_sec = 0
        eta_label = ""
    else:
        status = None
        message = ""
        focus = {}
        elapsed_sec = 0
        elapsed_label = ""
        pages_done = 0
        pages_total = 0
        workers = 1
        eta_remaining_sec = 0
        eta_label = ""

    failed_lesson_uids = sorted(
        uid for uid, info in lessons.items() if (info or {}).get("status") == "error"
    )

    return {
        "jobs": jobs,
        "job_count": len(jobs),
        "running_count": len(running),
        "status": status,
        "message": message,
        "lessons": lessons,
        "failed_lesson_uids": failed_lesson_uids,
        "volume_running": volume_running,
        "running_lesson_uids": running_lesson_uids,
        "workers": workers,
        "pages_done": pages_done,
        "pages_total": pages_total,
        "elapsed_sec": elapsed_sec,
        "elapsed_label": elapsed_label,
        "eta_remaining_sec": eta_remaining_sec,
        "eta_label": eta_label,
        "scope": running[0].get("scope") if len(running) == 1 else ("volume" if volume_running else (focus.get("scope") if focus else "lesson")),
        "new_lesson_uid": running[0].get("new_lesson_uid") if len(running) == 1 else None,
        "current_new_lesson_uid": running[0].get("current_new_lesson_uid") if len(running) == 1 else None,
        "current_index": running[0].get("current_index") if len(running) == 1 else 0,
        "total": running[0].get("total") if len(running) == 1 else pages_total,
        "lesson_page_index": running[0].get("lesson_page_index") if len(running) == 1 else 0,
        "lesson_page_count": running[0].get("lesson_page_count") if len(running) == 1 else 0,
    }


def list_lesson_page_steps(
    *,
    old_vol: Volume,
    new_vol: Volume,
    new_lesson_uid: str,
) -> list[dict[str, Any]]:
    """课内按新课页序生成旧↔新 PDF 页对（与对比页翻页一致）。"""
    new_les = _resolve_diff_lesson(new_lesson_uid, "diff_new")
    old_les, match_method = _resolve_old_lesson_for_new(
        old_vol=old_vol, new_vol=new_vol, new_les=new_les
    )
    if not new_les.page_start or not old_les.page_start:
        raise ValueError("课时尚无页码范围，请先在 intake 完成解析")

    new_ps = int(new_les.page_start)
    new_pe = int(new_les.page_end or new_les.page_start)
    old_ps = int(old_les.page_start)
    old_pe = int(old_les.page_end or old_les.page_start)
    new_page_count = max(1, new_pe - new_ps + 1)
    old_page_count = max(1, old_pe - old_ps + 1)

    steps: list[dict[str, Any]] = []
    for pi in range(1, new_page_count + 1):
        old_pi = max(1, min(pi, old_page_count))
        steps.append(
            {
                "page_index": pi,
                "page_count": new_page_count,
                "old_page": old_ps + old_pi - 1,
                "new_page": new_ps + pi - 1,
                "new_lesson_uid": new_les.lesson_uid,
                "old_lesson_uid": old_les.lesson_uid,
                "match_method": match_method,
                "label": f"{new_les.lesson_name or new_les.lesson_uid} · 第 {pi}/{new_page_count} 页",
            }
        )
    return steps


def list_volume_page_steps(
    *,
    old_vol: Volume,
    new_vol: Volume,
    only_new_lesson_uids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """整册：默认仅展开「待对比」(suggested) 课对页。

    only_new_lesson_uids：失败重启时只跑这些新课 uid（允许待确认/已确认，便于重试中途失败课）。
    """
    only_uids = {
        str(u).strip() for u in (only_new_lesson_uids or []) if str(u).strip()
    } or None
    rows = (
        DiffLessonPair.query.filter_by(
            old_volume_id=old_vol.id,
            new_volume_id=new_vol.id,
        )
        .order_by(DiffLessonPair.sort_order, DiffLessonPair.created_at)
        .all()
    )
    steps: list[dict[str, Any]] = []
    skipped_other = 0
    skipped_confirmed = 0
    skipped_pending = 0
    for row in rows:
        if not row.old_lesson_id or not row.new_lesson_id:
            continue
        st = (row.pair_status or "suggested").strip() or "suggested"
        if st == "rejected":
            continue
        new_les = Lesson.query.get(row.new_lesson_id)
        if not new_les or not new_les.lesson_uid:
            continue
        if only_uids is not None:
            if new_les.lesson_uid not in only_uids:
                continue
        else:
            # 整册一键只跑「待对比」；待确认 / 已确认不再重跑
            if st != "suggested":
                skipped_other += 1
                if st == "confirmed":
                    skipped_confirmed += 1
                elif st == "pending_review":
                    skipped_pending += 1
                continue
        try:
            lesson_steps = list_lesson_page_steps(
                old_vol=old_vol,
                new_vol=new_vol,
                new_lesson_uid=new_les.lesson_uid,
            )
        except ValueError as exc:
            _log.warning("skip lesson %s: %s", new_les.lesson_uid, exc)
            continue
        steps.extend(lesson_steps)
    _log.info(
        "volume pipeline steps=%s only_uids=%s (skip confirmed=%s pending_review=%s other=%s)",
        len(steps),
        len(only_uids) if only_uids is not None else None,
        skipped_confirmed,
        skipped_pending,
        max(0, skipped_other - skipped_confirmed - skipped_pending),
    )
    if not steps:
        if only_uids is not None:
            raise ValueError("失败重启：指定课时没有可跑的页（请确认粗分课对仍在）")
        if skipped_other:
            raise ValueError(
                "没有「待对比」的课对可跑：其余课对已是待确认/已确认。"
                "若要重跑某课，请把状态改回待对比后再跑整册。"
            )
        raise ValueError("没有可跑的课对页：请先粗分并为新课选择旧课对应")
    return steps


def run_page_one_click(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: str = "full",
    preview_blob_id: str | None = None,
    on_phase: Any | None = None,
) -> dict[str, Any]:
    """单页完整流水线（与对比页「本页一键」一致）。"""

    def _phase(code: str, label: str) -> None:
        if callable(on_phase):
            on_phase(code, label)

    cached = load_page_atom_cache(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    ) or {}
    done: list[str] = []

    text_done = bool(cached.get("old_text_ocr_done") and cached.get("new_text_ocr_done"))
    if not text_done:
        _phase("①", "①文字OCR")
        run_diff_page_ocr_both(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            ocr_phase="text",
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
        )
        done.append("①")
    else:
        _phase("①", "①文字OCR（缓存）")
        done.append("①缓存")

    _phase("②", "②文字比对")
    text_cmp = compare_cached_page_text(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        force=False,
    )
    done.append("②缓存" if text_cmp.get("from_cache") else "②")

    cached2 = load_page_atom_cache(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    ) or {}
    image_done = bool(cached2.get("old_image_ocr_done") and cached2.get("new_image_ocr_done"))
    if not image_done:
        _phase("③", "③图片OCR")
        run_diff_page_ocr_both(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            ocr_phase="images",
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
        )
        done.append("③")
    else:
        _phase("③", "③图片OCR（缓存）")
        done.append("③缓存")

    _phase("④", "④图片比对")
    img_cmp = compare_cached_page_images(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    done.append("④缓存" if img_cmp.get("from_cache") else "④")

    return {
        "old_page": old_page,
        "new_page": new_page,
        "steps": done,
        "text_verdict": (text_cmp.get("compare") or {}).get("verdict"),
        "image_verdict": (img_cmp.get("compare") or {}).get("verdict"),
    }


def _update_job(key: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(key)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()


def _refresh_eta_locked(job: dict[str, Any]) -> None:
    """调用方须已持有 _lock。"""
    started = float(job.get("started_at") or job.get("created_at") or time.time())
    pages_done = int(job.get("pages_done") or 0)
    pages_total = int(job.get("total") or 0)
    workers = int(job.get("workers") or 1)
    remain = _estimate_remaining_sec(
        pages_done=pages_done,
        pages_total=pages_total,
        page_seconds=list(job.get("page_seconds") or []),
        workers=workers,
        started_at=started,
    )
    elapsed = max(0.0, time.time() - started)
    job["elapsed_sec"] = int(round(elapsed))
    job["eta_remaining_sec"] = int(round(remain))
    job["eta_label"] = (
        f"预计还需约 {_format_duration(remain)}（已用 {_format_duration(elapsed)}）"
        if pages_total > 0
        else ""
    )


def _build_lesson_prog(
    lesson_totals: dict[str, int],
    lesson_done: dict[str, int],
    lesson_status: dict[str, str],
) -> dict[str, dict[str, Any]]:
    return {
        u: {
            "done": int(lesson_done.get(u, 0)),
            "total": int(lesson_totals.get(u, 0)),
            "status": lesson_status.get(u, "pending"),
        }
        for u in lesson_totals
    }


def _build_math_lesson_blocks(app, old_lesson_uid: str, new_lesson_uid: str) -> None:
    """数学课级 pipeline 完成后：两侧各做语义建块（复用教材库 build_library_lesson_blocks）。"""
    from ..textbook_library.lesson_work import build_library_lesson_blocks

    with app.app_context():
        for side, uid in (("旧侧", old_lesson_uid), ("新侧", new_lesson_uid)):
            try:
                _log.info("数学语义建块 %s %s", side, uid)
                build_library_lesson_blocks(lesson_uid=uid, replace_existing=True)
            except Exception:
                _log.exception("数学%s语义建块失败 %s", side, uid)
            finally:
                try:
                    db.session.remove()
                except Exception:
                    pass


def _run_job(
    *,
    app,
    key: str,
    old_code: str,
    new_code: str,
    steps: list[dict[str, Any]],
    scope: Scope,
    workers: int,
) -> None:
    with app.app_context():
        total = len(steps)
        confirmed_uids: set[str] = set()
        confirmed_lock = threading.Lock()
        errors: list[str] = []
        # 数学学科：课级 pipeline 完成后追加语义建块（对齐教材库）
        try:
            _nv = get_diff_volume_by_code(new_code)
            do_math_blocks = (_nv.subject or "").strip() == "数学"
        except Exception:
            do_math_blocks = False
        error_uids: set[str] = set()
        lesson_totals: dict[str, int] = {}
        for step in steps:
            u = str(step.get("new_lesson_uid") or "")
            if u:
                lesson_totals[u] = lesson_totals.get(u, 0) + 1
        lesson_done: dict[str, int] = {u: 0 for u in lesson_totals}
        lesson_status: dict[str, str] = {u: "pending" for u in lesson_totals}
        page_seconds: list[float] = []
        active_labels: dict[int, str] = {}
        active_uids: dict[int, str] = {}
        pages_done = 0
        started_at = time.time()
        w = max(1, min(int(workers or 1), 8))

        _update_job(
            key,
            lessons=_build_lesson_prog(lesson_totals, lesson_done, lesson_status),
            workers=w,
            pages_done=0,
            page_seconds=[],
            started_at=started_at,
            active_labels=[],
            active_lesson_uids=[],
            eta_remaining_sec=int(round(120.0 * total / w)),
            eta_label=f"预计还需约 {_format_duration(120.0 * total / w)}（冷跑粗估）",
            message=f"已启动：共 {total} 页，{w} 路并行…",
        )

        def _cancelled() -> bool:
            with _lock:
                job = _jobs.get(key)
                return bool(job and job.get("cancel"))

        def _run_one(step_index: int, step: dict[str, Any]) -> dict[str, Any]:
            label = step.get("label") or f"p{step['old_page']}↔p{step['new_page']}"
            uid = str(step.get("new_lesson_uid") or "")
            old_uid = str(step.get("old_lesson_uid") or "")
            page_i = int(step.get("page_index") or 0)
            page_n = int(step.get("page_count") or 0)
            t0 = time.time()
            with _lock:
                active_labels[step_index] = label
                if uid:
                    active_uids[step_index] = uid
                    if lesson_status.get(uid) != "error":
                        lesson_status[uid] = "running"
                job = _jobs.get(key)
                if job:
                    job["active_labels"] = list(active_labels.values())[:6]
                    job["active_lesson_uids"] = sorted(set(active_uids.values()))
                    job["current_label"] = label
                    job["current_new_lesson_uid"] = uid or None
                    job["lesson_page_index"] = page_i
                    job["lesson_page_count"] = page_n
                    job["page_phase"] = ""
                    job["page_phase_label"] = "排队/启动"
                    job["lessons"] = _build_lesson_prog(lesson_totals, lesson_done, lesson_status)
                    _refresh_eta_locked(job)
                    eta = job.get("eta_label") or ""
                    job["message"] = (
                        f"并行 {len(active_labels)}/{w} · 已完成 {pages_done}/{total}"
                        + (f" · {eta}" if eta else "")
                    )
                    job["updated_at"] = time.time()

            def _on_phase(code: str, phase_label: str) -> None:
                with _lock:
                    job = _jobs.get(key)
                    if not job:
                        return
                    job["page_phase"] = code
                    job["page_phase_label"] = phase_label
                    job["current_label"] = label
                    _refresh_eta_locked(job)
                    eta = job.get("eta_label") or ""
                    job["message"] = (
                        f"{label} · {phase_label} · 已完成 {pages_done}/{total}"
                        + (f" · {eta}" if eta else "")
                    )
                    job["updated_at"] = time.time()

            try:
                with app.app_context():
                    old_vol = _resolve_pipeline_old_vol(
                        get_diff_volume_by_code(old_code)
                    )
                    new_vol = get_diff_volume_by_code(new_code)
                    result = run_page_one_click(
                        old_vol=old_vol,
                        new_vol=new_vol,
                        old_page=int(step["old_page"]),
                        new_page=int(step["new_page"]),
                        on_phase=_on_phase,
                    )
                    if uid:
                        with confirmed_lock:
                            if uid not in confirmed_uids:
                                try:
                                    # 课对状态可写库；Test 的 OCR/比对内容仍只落本地 JSON
                                    mark_pair_ready_for_review(
                                        old_code=old_code,
                                        new_code=new_code,
                                        new_lesson_uid=uid,
                                    )
                                    confirmed_uids.add(uid)
                                except ValueError:
                                    pass
                    _log.info(
                        "pipeline page ok %s steps=%s (%.1fs)",
                        label,
                        "→".join(result.get("steps") or []),
                        time.time() - t0,
                    )
                    return {
                        "ok": True,
                        "label": label,
                        "uid": uid,
                        "old_uid": old_uid,
                        "page_i": page_i,
                        "page_n": page_n,
                        "elapsed": time.time() - t0,
                        "error": None,
                    }
            except Exception as exc:
                _log.exception("pipeline page failed %s", label)
                return {
                    "ok": False,
                    "label": label,
                    "uid": uid,
                    "old_uid": old_uid,
                    "page_i": page_i,
                    "page_n": page_n,
                    "elapsed": time.time() - t0,
                    "error": f"{label}：{exc}",
                }
            finally:
                try:
                    db.session.remove()
                except Exception:
                    pass

        try:
            with ThreadPoolExecutor(max_workers=w, thread_name_prefix="diff-pipe-page") as pool:
                futures = {
                    pool.submit(_run_one, i, step): i
                    for i, step in enumerate(steps)
                }
                for fut in as_completed(futures):
                    if _cancelled():
                        for f in futures:
                            f.cancel()
                        _update_job(key, status="cancelled", message="已取消")
                        return
                    step_index = futures[fut]
                    try:
                        outcome = fut.result()
                    except Exception as exc:
                        outcome = {
                            "ok": False,
                            "label": f"step#{step_index}",
                            "uid": "",
                            "page_i": 0,
                            "page_n": 0,
                            "elapsed": 0.0,
                            "error": str(exc),
                        }

                    uid = str(outcome.get("uid") or "")
                    old_uid = str(outcome.get("old_uid") or "")
                    lesson_just_done = False
                    with _lock:
                        active_labels.pop(step_index, None)
                        active_uids.pop(step_index, None)
                        pages_done += 1
                        elapsed_page = float(outcome.get("elapsed") or 0)
                        if elapsed_page > 0:
                            page_seconds.append(elapsed_page)
                        if uid:
                            lesson_done[uid] = int(lesson_done.get(uid, 0)) + 1
                            if not outcome.get("ok"):
                                error_uids.add(uid)
                            if lesson_done[uid] >= lesson_totals.get(uid, 0):
                                lesson_status[uid] = (
                                    "error" if uid in error_uids else "done"
                                )
                                if uid not in error_uids:
                                    lesson_just_done = True
                            else:
                                lesson_status[uid] = "running"
                        if not outcome.get("ok") and outcome.get("error"):
                            errors.append(str(outcome["error"]))
                        job = _jobs.get(key)
                        if job:
                            job["pages_done"] = pages_done
                            job["current_index"] = pages_done
                            job["page_seconds"] = list(page_seconds[-40:])
                            job["active_labels"] = list(active_labels.values())[:6]
                            job["active_lesson_uids"] = sorted(set(active_uids.values()))
                            job["lessons"] = _build_lesson_prog(
                                lesson_totals, lesson_done, lesson_status
                            )
                            if not outcome.get("ok"):
                                job["last_error"] = str(outcome.get("error") or "")
                            job["page_phase"] = ""
                            job["page_phase_label"] = ""
                            if active_labels:
                                cur_i = next(iter(active_labels))
                                job["current_label"] = active_labels[cur_i]
                                job["current_new_lesson_uid"] = active_uids.get(cur_i)
                            else:
                                job["current_label"] = ""
                                job["current_new_lesson_uid"] = None
                            _refresh_eta_locked(job)
                            eta = job.get("eta_label") or ""
                            job["message"] = (
                                f"已完成 {pages_done}/{total} 页"
                                + (f" · 并行中 {len(active_labels)}" if active_labels else "")
                                + (f" · {eta}" if eta else "")
                            )
                            job["updated_at"] = time.time()
                    # 数学：课级全部页跑完后，后台追加语义建块（对齐教材库）
                    if lesson_just_done and do_math_blocks and old_uid and uid:
                        threading.Thread(
                            target=_build_math_lesson_blocks,
                            args=(app, old_uid, uid),
                            daemon=True,
                            name=f"math-blocks-{uid}",
                        ).start()

            final_lessons = {}
            for u, n in lesson_totals.items():
                done = int(lesson_done.get(u, 0))
                if u in error_uids and done >= n:
                    st = "error"
                elif done >= n:
                    st = "done"
                else:
                    st = lesson_status.get(u, "pending")
                final_lessons[u] = {"done": done, "total": n, "status": st}

            elapsed_total = time.time() - started_at
            elapsed_sec = int(round(elapsed_total))
            elapsed_label = _format_duration(elapsed_total)
            ok_pages = total - len(errors)
            _log.info(
                "pipeline finished scope=%s pages=%s ok=%s errors=%s workers=%s elapsed_sec=%s (%s)",
                scope,
                total,
                ok_pages,
                len(errors),
                w,
                elapsed_sec,
                elapsed_label,
            )
            if errors:
                _update_job(
                    key,
                    status="done_with_errors",
                    message=(
                        f"完成：成功 {ok_pages}/{total} 页，失败 {len(errors)}"
                        f" · 实际耗时 {elapsed_label}（{w} 路并行）"
                    ),
                    errors=errors[:20],
                    current_label="",
                    current_new_lesson_uid=None,
                    phase="",
                    lessons=final_lessons,
                    pages_done=pages_done,
                    eta_remaining_sec=0,
                    eta_label="",
                    elapsed_sec=elapsed_sec,
                    elapsed_label=elapsed_label,
                    finished_at=time.time(),
                    active_labels=[],
                    active_lesson_uids=[],
                )
            else:
                _update_job(
                    key,
                    status="done",
                    message=(
                        f"全部完成：{total} 页 · 实际耗时 {elapsed_label}"
                        f"（{scope}，{w} 路并行）"
                    ),
                    errors=[],
                    current_label="",
                    current_new_lesson_uid=None,
                    phase="",
                    lessons=final_lessons,
                    pages_done=pages_done,
                    eta_remaining_sec=0,
                    eta_label="",
                    elapsed_sec=elapsed_sec,
                    elapsed_label=elapsed_label,
                    finished_at=time.time(),
                    active_labels=[],
                    active_lesson_uids=[],
                )
        except Exception as exc:
            _log.exception("pipeline job failed")
            _update_job(key, status="error", message=str(exc))


def cancel_pipeline(
    *,
    old_code: str,
    new_code: str,
    scope: Scope | None = None,
    new_lesson_uid: str | None = None,
) -> dict[str, Any]:
    """请求取消本册正在跑的一键任务（页级 worker 会在下一页边界停下）。"""
    old_code = (old_code or "").strip()
    new_code = (new_code or "").strip()
    if not old_code or not new_code:
        raise ValueError("缺少 old_code / new_code")
    prefix = _pair_prefix(old_code, new_code) + "::"
    cancelled: list[dict[str, Any]] = []
    with _lock:
        for k, j in list(_jobs.items()):
            if not k.startswith(prefix):
                continue
            if (j.get("status") or "") != "running":
                continue
            if scope == "volume" and j.get("scope") != "volume":
                continue
            if scope == "lesson":
                if j.get("scope") != "lesson":
                    continue
                uid = (new_lesson_uid or "").strip()
                if uid and str(j.get("new_lesson_uid") or "") != uid:
                    continue
            j["cancel"] = True
            j["message"] = "正在取消…"
            j["updated_at"] = time.time()
            cancelled.append(dict(j))
    # 小科取消（非小科无此模块，跳过）
    try:
        from .xiaoke_ocr import cancel_xiaoke_volume_full

        xk = cancel_xiaoke_volume_full(old_code=old_code, new_code=new_code)
        if xk.get("cancelled"):
            cancelled.append(xk.get("job") or {"kind": "xiaoke-volume-full"})
    except ImportError:
        pass
    if not cancelled:
        return {"ok": True, "cancelled": 0, "jobs": [], "message": "没有进行中的一键任务"}
    return {
        "ok": True,
        "cancelled": len(cancelled),
        "jobs": cancelled,
        "message": f"已请求取消 {len(cancelled)} 个任务",
    }


def start_pipeline(
    *,
    old_code: str,
    new_code: str,
    scope: Scope,
    new_lesson_uid: str | None = None,
    only_new_lesson_uids: list[str] | None = None,
) -> dict[str, Any]:
    old_code = (old_code or "").strip()
    new_code = (new_code or "").strip()
    if not old_code or not new_code:
        raise ValueError("缺少 old_code / new_code")
    if scope not in ("lesson", "volume"):
        raise ValueError("scope 须为 lesson 或 volume")

    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    only_uids = [
        str(u).strip() for u in (only_new_lesson_uids or []) if str(u).strip()
    ] or None

    from .xiaoke_view import is_xiaoke_volume

    if is_xiaoke_volume(old_vol) or is_xiaoke_volume(new_vol):
        if scope == "volume":
            from .xiaoke_ocr import start_xiaoke_volume_full

            return start_xiaoke_volume_full(
                old_code=old_code,
                new_code=new_code,
                skip_cached=True,
                new_lesson_uids=only_uids,
            )
        uid = (new_lesson_uid or "").strip()
        if not uid:
            raise ValueError("本课时一键需要 new_lesson_uid")
        from .xiaoke_ocr import start_xiaoke_lesson_full

        return start_xiaoke_lesson_full(
            old_code=old_code,
            new_code=new_code,
            new_lesson_uid=uid,
            skip_cached=True,
        )

    if scope == "lesson":
        uid = (new_lesson_uid or "").strip()
        if not uid:
            raise ValueError("本课时一键需要 new_lesson_uid")
        steps = list_lesson_page_steps(
            old_vol=old_vol, new_vol=new_vol, new_lesson_uid=uid
        )
    else:
        steps = list_volume_page_steps(
            old_vol=old_vol,
            new_vol=new_vol,
            only_new_lesson_uids=only_uids,
        )

    workers = _pipeline_page_workers()
    # 启动文案带上页数，便于确认「只跑待对比」已生效
    start_msg_pages = len(steps)
    key = _job_key(
        old_code,
        new_code,
        scope=scope,
        new_lesson_uid=new_lesson_uid if scope == "lesson" else None,
    )
    prefix = _pair_prefix(old_code, new_code) + "::"
    with _lock:
        if scope == "volume":
            for k, j in _jobs.items():
                if k.startswith(prefix) and (j.get("status") or "") == "running":
                    raise ValueError("已有本课时/整册一键在跑，请结束后再开整册")
        else:
            vol_job = _jobs.get(_job_key(old_code, new_code, scope="volume"))
            if vol_job and (vol_job.get("status") or "") == "running":
                raise ValueError("整册一键进行中，请稍候再开本课时")
            existing = _jobs.get(key)
            if existing and (existing.get("status") or "") == "running":
                raise ValueError("该课时一键已在跑")
        job_id = uuid.uuid4().hex[:12]
        started_at = time.time()
        rough_eta = 120.0 * len(steps) / max(1, workers)
        job = {
            "job_id": job_id,
            "old_code": old_code,
            "new_code": new_code,
            "scope": scope,
            "new_lesson_uid": (new_lesson_uid or "").strip() or None,
            "status": "running",
            "message": (
                (
                    f"已启动失败重启…（{len(only_uids or [])} 课 / {start_msg_pages} 页，"
                    f"{workers} 路并行，预计约 {_format_duration(rough_eta)}）"
                    if only_uids
                    else (
                        f"已启动…（仅待对比 {start_msg_pages} 页，{workers} 路并行，"
                        f"预计约 {_format_duration(rough_eta)}）"
                    )
                )
                if scope == "volume"
                else f"已启动…（{workers} 路并行，预计约 {_format_duration(rough_eta)}）"
            ),
            "retry_lesson_uids": list(only_uids or []),
            "current_index": 0,
            "total": len(steps),
            "pages_done": 0,
            "workers": workers,
            "page_seconds": [],
            "started_at": started_at,
            "elapsed_sec": 0,
            "eta_remaining_sec": int(round(rough_eta)),
            "eta_label": f"预计还需约 {_format_duration(rough_eta)}（冷跑粗估）",
            "elapsed_label": "",
            "finished_at": None,
            "current_label": "",
            "current_new_lesson_uid": None,
            "lesson_page_index": 0,
            "lesson_page_count": 0,
            "page_phase": "",
            "page_phase_label": "",
            "active_labels": [],
            "active_lesson_uids": [],
            "lessons": {},
            "errors": [],
            "last_error": "",
            "cancel": False,
            "created_at": started_at,
            "updated_at": started_at,
        }
        _jobs[key] = job

    app = current_app._get_current_object()
    threading.Thread(
        target=_run_job,
        kwargs={
            "app": app,
            "key": key,
            "old_code": old_code,
            "new_code": new_code,
            "steps": steps,
            "scope": scope,
            "workers": workers,
        },
        name=f"diff-pipe-{job_id}",
        daemon=True,
    ).start()

    with _lock:
        return dict(_jobs[key])
