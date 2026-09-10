"""旧库：本版多册 PDF 解析后台任务（离开工作台页不中断）。"""
from __future__ import annotations

import copy
import threading
from datetime import datetime
from typing import Any

from ....extensions import db
from ....models import Lesson, Volume
from ..edition_registry import get_edition, grade_term_pairs_for_edition
from ..volume_codes import make_volume_code, normalize_term
from .parse import parse_volume_pdf

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def get_edition_parse_job(edition_id: str) -> dict | None:
    eid = (edition_id or "").strip()
    if not eid:
        return None
    with _lock:
        job = _jobs.get(eid)
        return copy.deepcopy(job) if job else None


def cancel_edition_parse_job(edition_id: str) -> bool:
    """请求取消。当前册仍会跑完；立即把 status 标为 cancelling，便于前端解锁。"""
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


def clear_edition_parse_job(edition_id: str) -> bool:
    """丢弃内存中的本版解析任务（用于卡死任务解锁；不杀线程）。"""
    eid = (edition_id or "").strip()
    with _lock:
        if eid not in _jobs:
            return False
        del _jobs[eid]
        return True


def _edition_volume_targets(edition_id: str, *, only_pending: bool) -> list[dict[str, Any]]:
    edition = get_edition(edition_id)
    rows: list[dict[str, Any]] = []
    for grade, term in grade_term_pairs_for_edition(edition):
        term_key = normalize_term(term)
        code = make_volume_code(edition, grade=grade, term=term_key, book_type="old")
        vol = Volume.query.filter_by(volume_code=code, book_type="old").first()
        if not vol or not vol.blob_id:
            continue
        lesson_count = Lesson.query.filter_by(volume_id=vol.id).count()
        if lesson_count <= 0:
            continue
        if only_pending and vol.parse_status == "done":
            continue
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


def _run_edition_parse(edition_id: str, volume_codes: list[str], app) -> None:
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
                label=f"解析中…（第 {i + 1}/{len(volume_codes)} 册）",
            )
            try:
                result = parse_volume_pdf(volume_code=code)
                if result.get("ok"):
                    ok_count += 1
                    _patch_volume(
                        edition_id,
                        code,
                        state="done",
                        label="已完成",
                        parse_status="done",
                    )
                else:
                    err_count += 1
                    err = result.get("parse_error") or result.get("error") or "解析失败"
                    _patch_volume(
                        edition_id,
                        code,
                        state="error",
                        label=err,
                        parse_status=result.get("parse_status") or "failed",
                        error=err,
                    )
            except Exception as exc:
                db.session.rollback()
                err_count += 1
                _patch_volume(
                    edition_id,
                    code,
                    state="error",
                    label=str(exc),
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
            job["parsed_count"] = ok_count
            job["error_count"] = err_count
            job["current_volume"] = None


def start_edition_parse_all(
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
                "message": "没有待解析的册（需已上传 PDF，且尚未拆分完成）",
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
        "parsed_count": 0,
        "error_count": 0,
        "message": f"后台解析 {len(targets)} 册…",
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
        target=_run_edition_parse,
        args=(eid, [t["volume_code"] for t in targets], app),
        daemon=True,
    ).start()
    return {"ok": True, "already_running": False, "job": copy.deepcopy(job)}
