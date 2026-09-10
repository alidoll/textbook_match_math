"""新库：本版多册整册预处理后台任务（目录→划分→页图→粗分）。"""
from __future__ import annotations

import copy
import logging
import threading
from datetime import datetime
from typing import Any

from ....extensions import db
from ....models import Lesson, Volume
from ...course_match.service import run_course_match_for_volume
from ...old_library.edition_registry import get_edition, grade_term_pairs_for_edition
from ...old_library.volume_codes import make_volume_code, normalize_term
from ..catalog_from_pdf import bootstrap_catalog_from_volume_pdf
from ..pdf.parse import parse_volume_pdf
from ..pdf.rebuild_pages import rebuild_lesson_pages_for_volume

_log = logging.getLogger(__name__)

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def get_edition_preprocess_job(edition_id: str) -> dict | None:
    eid = (edition_id or "").strip()
    if not eid:
        return None
    with _lock:
        job = _jobs.get(eid)
        return copy.deepcopy(job) if job else None


def cancel_edition_preprocess_job(edition_id: str) -> bool:
    eid = (edition_id or "").strip()
    with _lock:
        job = _jobs.get(eid)
        if not job or job.get("status") not in ("running", "cancelling"):
            return False
        job["cancel_requested"] = True
        if job.get("status") == "running":
            job["status"] = "cancelling"
            job["message"] = (
                f"正在取消…（当前册 {job.get('current_volume') or ''} 跑完后停止）"
            )
        return True


def clear_edition_preprocess_job(edition_id: str) -> bool:
    eid = (edition_id or "").strip()
    with _lock:
        if eid not in _jobs:
            return False
        del _jobs[eid]
        return True


def _volume_needs_preprocess(vol: Volume) -> bool:
    if not vol.blob_id:
        return False
    from ....models import LessonMatch, LessonPage

    lessons = Lesson.query.filter_by(volume_id=vol.id).all()
    if not lessons:
        return True
    if vol.parse_status != "done":
        return True
    if not all(l.page_start and l.page_end for l in lessons):
        return True
    if any(LessonPage.query.filter_by(lesson_id=l.id).count() <= 0 for l in lessons):
        return True
    matched = LessonMatch.query.filter(
        LessonMatch.new_lesson_id.in_([l.id for l in lessons])
    ).count()
    return matched < len(lessons)


def _edition_volume_targets(edition_id: str, *, only_pending: bool) -> list[dict[str, Any]]:
    edition = get_edition(edition_id)
    rows: list[dict[str, Any]] = []
    for grade, term in grade_term_pairs_for_edition(edition):
        term_key = normalize_term(term)
        code = make_volume_code(edition, grade=grade, term=term_key, book_type="new")
        vol = Volume.query.filter_by(volume_code=code, book_type="new").first()
        if not vol or not vol.blob_id:
            continue
        if only_pending and not _volume_needs_preprocess(vol):
            continue
        lesson_count = Lesson.query.filter_by(volume_id=vol.id).count()
        rows.append(
            {
                "volume_code": code,
                "grade": grade,
                "term": term_key,
                "lesson_count": lesson_count,
                "parse_status": vol.parse_status,
            }
        )
    return rows


def _patch_volume(edition_id: str, volume_code: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(edition_id)
        if not job:
            return
        row = (job.get("volumes") or {}).get(volume_code)
        if not row:
            return
        row.update(fields)


def _preprocess_one_volume(volume_code: str) -> dict[str, Any]:
    from ....models import LessonPage

    vol = Volume.query.filter_by(volume_code=volume_code, book_type="new").first()
    if not vol or not vol.blob_id:
        raise ValueError("册次无 PDF")

    steps_done: list[str] = []
    lessons = Lesson.query.filter_by(volume_id=vol.id).all()
    if not lessons:
        bootstrap_catalog_from_volume_pdf(volume_code=volume_code, replace=False)
        steps_done.append("catalog")
        lessons = Lesson.query.filter_by(volume_id=vol.id).all()
    if not lessons:
        raise ValueError("目录识别后仍无课时")

    vol = Volume.query.filter_by(volume_code=volume_code, book_type="new").first()
    need_parse = (vol.parse_status or "") != "done" or not all(
        l.page_start and l.page_end for l in lessons
    )
    if need_parse:
        result = parse_volume_pdf(
            volume_code=volume_code,
            skip_body_text=False,
            replace_lessons=False,
            write_lesson_pages=True,
            force_recalibrate=True,
        )
        if not result.get("ok"):
            raise ValueError(result.get("error") or "页码划分失败")
        steps_done.append("parse")
        lessons = Lesson.query.filter_by(volume_id=vol.id).all()

    need_pages = any(
        LessonPage.query.filter_by(lesson_id=l.id).count() <= 0 for l in lessons
    )
    if need_pages:
        rebuild_lesson_pages_for_volume(volume_code=volume_code)
        steps_done.append("pages")

    try:
        cm = run_course_match_for_volume(volume_code=volume_code)
        if cm.get("ok") is False:
            raise ValueError(cm.get("error") or "粗分失败")
        steps_done.append("course_match")
    except ValueError as exc:
        msg = str(exc)
        if "旧库" in msg:
            steps_done.append("course_match_skipped")
            return {
                "ok": True,
                "steps": steps_done,
                "volume_code": volume_code,
                "warning": msg,
            }
        raise
    return {"ok": True, "steps": steps_done, "volume_code": volume_code}


def _run_edition_preprocess(edition_id: str, volume_codes: list[str], app) -> None:
    ok_count = 0
    err_count = 0
    with app.app_context():
        for i, code in enumerate(volume_codes):
            with _lock:
                job = _jobs.get(edition_id)
                if not job:
                    return
                if job.get("cancel_requested"):
                    job["status"] = "cancelled"
                    job["finished_at"] = datetime.utcnow().isoformat()
                    job["message"] = "已取消"
                    return
                job["current_index"] = i + 1
                job["current_volume"] = code

            _patch_volume(
                edition_id,
                code,
                state="running",
                label=f"预处理中…（第 {i + 1}/{len(volume_codes)} 册）",
            )
            try:
                _preprocess_one_volume(code)
                ok_count += 1
                _patch_volume(
                    edition_id,
                    code,
                    state="done",
                    label="预处理完成",
                )
            except Exception as exc:
                err_count += 1
                db.session.rollback()
                _log.exception("新库本版预处理失败 %s", code)
                _patch_volume(
                    edition_id,
                    code,
                    state="error",
                    label=f"失败：{exc}",
                    error=str(exc),
                )

        with _lock:
            job = _jobs.get(edition_id)
            if not job:
                return
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
                job["message"] = (
                    f"已取消：成功 {ok_count} · 失败 {err_count} · "
                    f"未跑完 {max(0, len(volume_codes) - ok_count - err_count)}"
                )
            else:
                job["status"] = "done"
                job["message"] = f"完成：成功 {ok_count} · 失败 {err_count}"
            job["finished_at"] = datetime.utcnow().isoformat()
            job["done_count"] = ok_count
            job["error_count"] = err_count
            job["current_volume"] = None


def start_edition_preprocess_all(
    *,
    edition_id: str,
    app,
    only_pending: bool = True,
) -> dict[str, Any]:
    eid = (edition_id or "").strip()
    if not eid:
        raise ValueError("缺少版本 id")
    edition = get_edition(eid)
    eid = edition.edition_id

    with _lock:
        existing = _jobs.get(eid)
        if existing and existing.get("status") in ("running", "cancelling"):
            return {"ok": True, "already_running": True, "job": copy.deepcopy(existing)}

    targets = _edition_volume_targets(eid, only_pending=only_pending)
    if not targets:
        return {
            "ok": True,
            "edition_id": eid,
            "edition_label": edition.label,
            "job": {
                "status": "idle",
                "volume_count": 0,
                "message": "没有待预处理的册（需已上传 PDF，且尚未完成目录/划分/页图/粗分）",
            },
        }

    job = {
        "ok": True,
        "edition_id": eid,
        "edition_label": edition.label,
        "status": "running",
        "cancel_requested": False,
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "volume_count": len(targets),
        "current_index": 0,
        "current_volume": None,
        "done_count": 0,
        "error_count": 0,
        "message": f"后台预处理 {len(targets)} 册…",
        "volumes": {
            row["volume_code"]: {
                "volume_code": row["volume_code"],
                "grade": row["grade"],
                "term": row["term"],
                "lesson_count": row["lesson_count"],
                "state": "queued",
                "label": "排队中…",
            }
            for row in targets
        },
    }
    with _lock:
        _jobs[eid] = job

    threading.Thread(
        target=_run_edition_preprocess,
        args=(eid, [t["volume_code"] for t in targets], app),
        daemon=True,
        name=f"new-ed-preprocess-{eid}",
    ).start()
    return {"ok": True, "job": copy.deepcopy(job)}
