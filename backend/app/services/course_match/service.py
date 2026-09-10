"""粗分任务：写入 match_jobs / lesson_matches。"""
from __future__ import annotations

import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from flask import current_app
from sqlalchemy import desc, insert, text

from ...extensions import db
from ...models import Lesson, LessonMatch, MatchJob, Volume
from ...query.lesson_order import order_lessons_query
from ..lesson_filters import (
    filter_master_class_lessons,
    is_master_class_lesson,
    is_unit_summary_label,
)
from ..llm.config import (
    llm_course_match_enabled,
    llm_course_match_mode,
    llm_course_match_workers,
)
from ..new_library.volumes import get_new_volume_by_code
from .batch import match_volume_batch
from .body_text_prep import ensure_lessons_body_text_for_coarse_match
from .hybrid import match_new_lesson_hybrid
from .engine import (
    TIER_LABELS,
    MatchHit,
    build_candidate,
    build_exact_index,
    old_hint_for_candidate,
    similarity_score_for_pair,
)
from .match_signals import insert_match_signal


def _load_old_pool(edition: str) -> list:
    from .engine import LessonCandidate

    pool: list[LessonCandidate] = []
    volumes = Volume.query.filter_by(edition=edition, book_type="old").all()
    for vol in volumes:
        lessons = order_lessons_query(
            Lesson.query.filter_by(volume_id=vol.id)
        ).all()
        for les in lessons:
            if is_unit_summary_label(les.lesson_name):
                continue
            cand = build_candidate(
                lesson_id=les.id,
                edition=vol.edition,
                grade=vol.grade,
                semester=vol.semester,
                unit_title=les.unit_title,
                lesson_no=les.lesson_no,
                lesson_name=les.lesson_name,
                old_course_id=les.old_course_id,
                page_count=les.page_count,
                body_text=les.body_text,
            )
            if cand:
                pool.append(cand)
    return pool


def _load_new_lessons(volume: Volume) -> list[Lesson]:
    lessons = order_lessons_query(
        Lesson.query.filter_by(volume_id=volume.id)
    ).all()
    return filter_master_class_lessons(lessons)


def _tier_stats(hits: list[MatchHit]) -> str | None:
    """每节取 rank=1 的主结果统计 tier。"""
    primary = hits[0] if hits else None
    return primary.tier if primary else None


def _lesson_sort_key(les: Lesson) -> tuple:
    no = str(les.lesson_no or "")
    return (int(no) if no.isdigit() else 9999, les.lesson_name or "")


def _match_one_lesson(
    app,
    les: Lesson,
    *,
    edition: str,
    grade: str,
    semester: str,
    old_pool: list,
    exact_index: dict,
) -> dict | None:
    with app.app_context():
        if is_master_class_lesson(les) or is_unit_summary_label(les.lesson_name):
            return None
        new_cand = build_candidate(
            lesson_id=les.id,
            edition=edition,
            grade=grade,
            semester=semester,
            unit_title=les.unit_title,
            lesson_no=les.lesson_no,
            lesson_name=les.lesson_name,
            old_course_id=None,
            page_count=None,
            body_text=les.body_text,
        )
        if not new_cand:
            return None
        hits, match_source, llm_reason = match_new_lesson_hybrid(
            new_cand, old_pool, exact_index
        )
        return {
            "lesson": les,
            "hits": hits,
            "match_source": match_source,
            "llm_reason": llm_reason,
        }


def _build_new_items(
    *,
    volume: Volume,
    new_lessons: list[Lesson],
) -> list[tuple[str, object]]:
    """(lesson_id, LessonCandidate) 按课序。"""
    from .engine import LessonCandidate

    items: list[tuple[str, LessonCandidate]] = []
    tasks = [
        les
        for les in new_lessons
        if not is_master_class_lesson(les)
        and not is_unit_summary_label(les.lesson_name)
    ]
    tasks.sort(key=_lesson_sort_key)
    for les in tasks:
        new_cand = build_candidate(
            lesson_id=les.id,
            edition=volume.edition,
            grade=volume.grade,
            semester=volume.semester,
            unit_title=les.unit_title,
            lesson_no=les.lesson_no,
            lesson_name=les.lesson_name,
            old_course_id=None,
            page_count=None,
            body_text=les.body_text,
        )
        if new_cand:
            items.append((les.id, new_cand))
    return items, {les.id: les for les in tasks}


def _collect_match_results_batch(
    *,
    volume: Volume,
    new_lessons: list[Lesson],
    old_pool: list,
    exact_index: dict,
) -> list[dict]:
    new_items, lessons_by_id = _build_new_items(volume=volume, new_lessons=new_lessons)
    id_pairs = [(lid, cand) for lid, cand in new_items]
    matched = match_volume_batch(
        edition=volume.edition,
        grade=volume.grade,
        semester=volume.semester or "上",
        new_items=id_pairs,
        old_pool=old_pool,
        exact_index=exact_index,
    )
    rows: list[dict] = []
    for lesson_id, (hits, match_source, llm_reason) in matched.items():
        les = lessons_by_id.get(lesson_id)
        if not les:
            continue
        rows.append(
            {
                "lesson": les,
                "hits": hits,
                "match_source": match_source,
                "llm_reason": llm_reason,
            }
        )
    rows.sort(key=lambda row: _lesson_sort_key(row["lesson"]))
    return rows


def _collect_match_results_hybrid(
    *,
    volume: Volume,
    new_lessons: list[Lesson],
    old_pool: list,
    exact_index: dict,
) -> list[dict]:
    tasks = [
        les
        for les in new_lessons
        if not is_master_class_lesson(les)
        and not is_unit_summary_label(les.lesson_name)
    ]
    tasks.sort(key=_lesson_sort_key)

    workers = llm_course_match_workers() if llm_course_match_enabled() else 1
    if workers <= 1 or len(tasks) <= 1:
        app = current_app._get_current_object()
        return [
            row
            for les in tasks
            if (
                row := _match_one_lesson(
                    app,
                    les,
                    edition=volume.edition,
                    grade=volume.grade,
                    semester=volume.semester,
                    old_pool=old_pool,
                    exact_index=exact_index,
                )
            )
        ]

    app = current_app._get_current_object()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _match_one_lesson,
                app,
                les,
                edition=volume.edition,
                grade=volume.grade,
                semester=volume.semester,
                old_pool=old_pool,
                exact_index=exact_index,
            ): les
            for les in tasks
        }
        for future in as_completed(futures):
            row = future.result()
            if row:
                results.append(row)
    results.sort(key=lambda row: _lesson_sort_key(row["lesson"]))
    return results


def _collect_match_results(
    *,
    volume: Volume,
    new_lessons: list[Lesson],
    old_pool: list,
    exact_index: dict,
) -> list[dict]:
    mode = llm_course_match_mode()
    if mode == "batch" and llm_course_match_enabled():
        return _collect_match_results_batch(
            volume=volume,
            new_lessons=new_lessons,
            old_pool=old_pool,
            exact_index=exact_index,
        )
    return _collect_match_results_hybrid(
        volume=volume,
        new_lessons=new_lessons,
        old_pool=old_pool,
        exact_index=exact_index,
    )


def _match_engine_label(*, llm_used: int, llm_fallback: int) -> str:
    mode = llm_course_match_mode()
    if mode == "batch" and (llm_used or llm_fallback):
        return "batch"
    if llm_used or llm_fallback:
        return "hybrid"
    return "rules"


def _old_lessons_and_volumes(edition: str) -> tuple[dict[str, Lesson], dict[str, Volume]]:
    volumes = Volume.query.filter_by(edition=edition, book_type="old").all()
    vol_by_id = {v.id: v for v in volumes}
    if not vol_by_id:
        return {}, {}
    lessons = Lesson.query.filter(Lesson.volume_id.in_(vol_by_id)).all()
    return {les.id: les for les in lessons}, vol_by_id


def _volume_match_lock_name(volume_id: str) -> str:
    return f"textbook_match:course_match:{volume_id}"


def _is_sqlite() -> bool:
    return db.session.get_bind().dialect.name == "sqlite"


def _acquire_volume_match_lock(volume_id: str, *, timeout: int = 60) -> None:
    # SQLite 无 named lock，单进程写本就串行（WAL + busy_timeout 兜底）
    if _is_sqlite():
        return
    got = db.session.execute(
        text("SELECT GET_LOCK(:name, :timeout)"),
        {"name": _volume_match_lock_name(volume_id), "timeout": timeout},
    ).scalar()
    if got != 1:
        raise ValueError("粗分写入正在进行中或等待库锁超时，请稍后再试")


def _release_volume_match_lock(volume_id: str) -> None:
    if _is_sqlite():
        return
    db.session.execute(
        text("SELECT RELEASE_LOCK(:name)"),
        {"name": _volume_match_lock_name(volume_id)},
    )


def _raise_if_lock_timeout(exc: BaseException) -> None:
    msg = str(exc)
    if "1205" in msg or "Lock wait timeout" in msg:
        raise ValueError(
            "数据库锁等待超时：请确认无其他粗分/预扫描任务进行中，稍后再试。"
        ) from exc


def _persist_lesson_match_batch(rows: list[dict], *, max_attempts: int = 5) -> None:
    if not rows:
        return
    if not _is_sqlite():
        db.session.execute(text("SET SESSION innodb_lock_wait_timeout = 120"))
    for attempt in range(max_attempts):
        try:
            with db.session.no_autoflush:
                db.session.execute(insert(LessonMatch), rows)
            db.session.commit()
            return
        except Exception as exc:
            db.session.rollback()
            msg = str(exc)
            if ("1205" not in msg and "Lock wait timeout" not in msg) or attempt + 1 >= max_attempts:
                _raise_if_lock_timeout(exc)
                raise
            time.sleep(1.5 * (attempt + 1))


def _lesson_match_row(
    *,
    match_id: str,
    job_id: str,
    les: Lesson,
    hit: MatchHit,
    hint: str | None,
) -> dict:
    old = hit.old
    return {
        "id": match_id,
        "job_id": job_id,
        "new_lesson_id": les.id,
        "old_lesson_id": old.lesson_id if old else None,
        "match_tier": hit.tier,
        "similarity_score": hit.similarity_score,
        "old_lesson_hint": hint,
        "subject": None,
        "old_courseware_id": old.old_course_id if old else None,
        "old_page_count": old.page_count if old else None,
        "rank": hit.rank,
        "annotate_primary": (
            hit.rank == 1
            and hit.tier in ("exact", "high_similarity")
            and old is not None
        ),
        "compare_confirmed_at": None,
        "compare_confirmed_by": None,
        "pair_review_status": None,
        "pair_reviewed_at": None,
    }


def _fail_open_running_jobs(volume_id: str) -> None:
    """清理上次异常退出留下的 running 任务，避免占用连接/干扰重试。"""
    rows = MatchJob.query.filter_by(new_volume_id=volume_id, status="running").all()
    if not rows:
        return
    now = datetime.utcnow()
    for job in rows:
        job.status = "failed"
        job.summary_json = {"error": "被新粗分任务取代或上次异常退出"}
        job.finished_at = now
    db.session.commit()


def _mark_job_failed(job_id: str, error: str) -> None:
    db.session.rollback()
    job = db.session.get(MatchJob, job_id)
    if not job:
        return
    job.status = "failed"
    job.summary_json = {"error": error[:500]}
    job.finished_at = datetime.utcnow()
    db.session.commit()


def run_course_match_for_volume(*, volume_code: str) -> dict:
    volume = get_new_volume_by_code(volume_code)
    new_lessons = _load_new_lessons(volume)
    if not new_lessons:
        raise ValueError("尚无课时，请先上传 PDF 并解析划分课时")

    old_pool = _load_old_pool(volume.edition)
    if not old_pool:
        raise ValueError(
            f"旧库无课时（edition={volume.edition}）。请先在旧库工作台载入基准目录。"
        )

    exact_index = build_exact_index(old_pool)
    old_lessons_by_id, old_volumes_by_id = _old_lessons_and_volumes(volume.edition)

    _fail_open_running_jobs(volume.id)

    job = MatchJob(new_volume_id=volume.id, status="running")
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    exact_count = 0
    high_count = 0
    traceability_count = 0
    none_count = 0
    rows_written = 0
    llm_used = 0
    llm_fallback = 0

    try:
        prep = ensure_lessons_body_text_for_coarse_match(volume, new_lessons)
        if int(prep.get("ocr_lessons") or 0):
            current_app.logger.info(
                "粗分前 OCR 正文 %s：%d 课写入 body_text，跳过 %d，空 %d",
                volume_code,
                prep.get("ocr_lessons"),
                prep.get("skipped"),
                prep.get("empty"),
            )

        # LLM/规则匹配在事务外执行，避免长时间占锁
        match_rows = _collect_match_results(
            volume=volume,
            new_lessons=new_lessons,
            old_pool=old_pool,
            exact_index=exact_index,
        )

        _acquire_volume_match_lock(volume.id)
        try:
            for row in match_rows:
                les = row["lesson"]
                hits = row["hits"]
                match_source = row["match_source"]
                llm_reason = row["llm_reason"]

                if match_source == "llm":
                    llm_used += 1
                elif match_source == "llm_fallback":
                    llm_fallback += 1
                primary_tier = _tier_stats(hits)
                if primary_tier == "exact":
                    exact_count += 1
                elif primary_tier == "high_similarity":
                    high_count += 1
                else:
                    none_count += 1
                if any(h.tier == "traceability" for h in hits):
                    traceability_count += 1

                batch: list[dict] = []
                signal_specs: list[dict] = []
                for hit in hits:
                    old = hit.old
                    hint = old_hint_for_candidate(old) if old else None
                    if hit.rank == 1 and llm_reason and match_source == "llm":
                        hint = (
                            f"{hint}（AI：{llm_reason}）" if hint else f"AI：{llm_reason}"
                        )
                    match_id = str(uuid.uuid4())
                    batch.append(
                        _lesson_match_row(
                            match_id=match_id,
                            job_id=job_id,
                            les=les,
                            hit=hit,
                            hint=hint,
                        )
                    )
                    if old:
                        old_les = old_lessons_by_id.get(old.lesson_id)
                        old_vol = (
                            old_volumes_by_id.get(old_les.volume_id)
                            if old_les
                            else None
                        )
                        if old_les and old_vol:
                            signal_specs.append(
                                {
                                    "match_id": match_id,
                                    "new_lesson": les,
                                    "old_lesson": old_les,
                                    "old_volume": old_vol,
                                    "signal_source": match_source,
                                    "llm_reason": llm_reason if hit.rank == 1 else None,
                                }
                            )
                    rows_written += 1

                _persist_lesson_match_batch(batch)
                with db.session.no_autoflush:
                    for spec in signal_specs:
                        insert_match_signal(
                            lesson_match_id=spec["match_id"],
                            new_lesson=spec["new_lesson"],
                            old_lesson=spec["old_lesson"],
                            new_volume=volume,
                            old_volume=spec["old_volume"],
                            signal_source=spec["signal_source"],
                            llm_reason=spec["llm_reason"],
                        )
                db.session.commit()
        finally:
            _release_volume_match_lock(volume.id)

        job = db.session.get(MatchJob, job_id)
        if not job:
            raise RuntimeError("粗分任务丢失")
        job.status = "done"
        job.summary_json = {
            "lesson_count": exact_count + high_count + traceability_count + none_count,
            "exact_match": exact_count,
            "high_similarity": high_count,
            "traceability": traceability_count,
            "no_match": none_count,
            "match_rows": rows_written,
            "old_pool_size": len(old_pool),
            "body_text_prep": prep,
            "match_engine": _match_engine_label(
                llm_used=llm_used, llm_fallback=llm_fallback
            ),
            "llm_decisions": llm_used,
            "llm_fallbacks": llm_fallback,
        }
        job.finished_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        _mark_job_failed(job_id, str(exc))
        raise

    return get_course_match_result(volume_code=volume_code, job_id=job_id)


def _latest_job(volume_id: str) -> MatchJob | None:
    return (
        MatchJob.query.filter_by(new_volume_id=volume_id)
        .order_by(desc(MatchJob.created_at))
        .first()
    )


_TIER_PRIORITY = {"exact": 0, "high_similarity": 1}


def _lesson_no_sort_key(lesson_no: str | None) -> int:
    no = str(lesson_no or "")
    return int(no) if no.isdigit() else 9999


def _primary_candidate(row: dict) -> dict | None:
    cands = row.get("candidates") or []
    if not cands:
        return None
    return min(cands, key=lambda c: c.get("rank") or 999)


def _primary_old_lesson_id(row: dict) -> str | None:
    primary = _primary_candidate(row)
    if not primary:
        return None
    if primary.get("match_tier") not in ("exact", "high_similarity"):
        return None
    return primary.get("old_lesson_id")


def _primary_owner_sort_key(row: dict) -> tuple:
    tier = row.get("match_tier") or "none"
    score = row.get("similarity_score")
    score_f = float(score) if score is not None else 0.0
    return (
        _TIER_PRIORITY.get(tier, 9),
        -score_f,
        _lesson_no_sort_key(row.get("new_lesson_no")),
        row.get("new_lesson_uid") or "",
    )


def _build_primary_old_owners(by_lesson: list[dict]) -> dict[str, str]:
    """旧课 id → 本册唯一核心对照新课 uid（完全同名优先于高相似，再比分数与课序）。"""
    owners: dict[str, str] = {}
    owner_keys: dict[str, tuple] = {}
    for row in by_lesson:
        old_id = _primary_old_lesson_id(row)
        if not old_id:
            continue
        key = _primary_owner_sort_key(row)
        prev = owner_keys.get(old_id)
        if prev is None or key < prev:
            owner_keys[old_id] = key
            owners[old_id] = row["new_lesson_uid"]
    return owners


def _merge_stored_ai_suffix(stored_hint: str | None, live_hint: str) -> str:
    """保留粗分写入的 AI 说明，单元/课时名用旧库最新数据。"""
    if not stored_hint or not live_hint:
        return live_hint or stored_hint or ""
    ai = re.search(r"（AI：[\s\S]+）$", stored_hint)
    if ai:
        return f"{live_hint}{ai.group(0)}"
    return live_hint


def _live_old_match_fields(
    old_les: Lesson | None,
    volumes_by_id: dict[str, Volume],
    *,
    stored_hint: str | None,
    stored_courseware_id: str | None,
    stored_page_count: int | None,
) -> dict:
    """展示用旧课信息：优先读旧库 lessons 表最新单元/课时名。"""
    if not old_les:
        return {
            "old_lesson_hint": stored_hint,
            "old_courseware_id": stored_courseware_id,
            "old_page_count": stored_page_count,
        }
    old_vol = volumes_by_id.get(old_les.volume_id)
    courseware_id = old_les.old_course_id or stored_courseware_id
    page_count = old_les.page_count if old_les.page_count is not None else stored_page_count
    if not old_vol:
        return {
            "old_lesson_hint": stored_hint,
            "old_courseware_id": courseware_id,
            "old_page_count": page_count,
        }
    cand = build_candidate(
        lesson_id=old_les.id,
        edition=old_vol.edition,
        grade=old_vol.grade,
        semester=old_vol.semester,
        unit_title=old_les.unit_title or "",
        lesson_no=old_les.lesson_no or "",
        lesson_name=old_les.lesson_name or "",
        old_course_id=old_les.old_course_id,
        page_count=old_les.page_count,
    )
    live_hint = old_hint_for_candidate(cand) if cand else (stored_hint or "")
    return {
        "old_lesson_hint": _merge_stored_ai_suffix(stored_hint, live_hint),
        "old_courseware_id": courseware_id,
        "old_page_count": page_count,
    }


def _blocked_old_from_candidate(cand: dict | None) -> dict | None:
    if not cand or not cand.get("old_lesson_id"):
        return None
    hint = (cand.get("old_lesson_hint") or "").strip()
    if not hint:
        return None
    return {
        "old_lesson_id": cand.get("old_lesson_id"),
        "old_lesson_uid": cand.get("old_lesson_uid"),
        "old_lesson_hint": hint,
        "old_courseware_id": cand.get("old_courseware_id"),
        "old_page_count": cand.get("old_page_count"),
    }


def _blocked_old_from_row_primary(row: dict) -> dict | None:
    hint = (row.get("old_lesson_hint") or "").strip()
    if not hint:
        return None
    primary = _primary_candidate(row) or {}
    return {
        "old_lesson_id": primary.get("old_lesson_id"),
        "old_lesson_uid": primary.get("old_lesson_uid"),
        "old_lesson_hint": hint,
        "old_courseware_id": row.get("old_courseware_id"),
        "old_page_count": row.get("old_page_count"),
    }


def _clear_build_hint(row: dict) -> None:
    row["build_hint"] = None
    row["build_hint_label"] = None
    row["build_hint_reason"] = None
    row["build_hint_blocked_old"] = None


def _set_fully_new(
    row: dict,
    *,
    reason: str,
    blocked_old: dict | None,
) -> None:
    row["build_hint"] = "fully_new"
    row["build_hint_label"] = "完全新制"
    row["build_hint_reason"] = reason
    row["build_hint_blocked_old"] = blocked_old


def apply_build_hints(by_lesson: list[dict]) -> None:
    """对照旧课已被他课占用 → 建议完全新制（含高相似撞车、无匹配仅溯源）。"""
    owners = _build_primary_old_owners(by_lesson)
    owned_old_ids = set(owners)

    for row in by_lesson:
        uid = row.get("new_lesson_uid") or ""
        primary_old_id = _primary_old_lesson_id(row)
        if (
            row.get("match_tier") in ("exact", "high_similarity")
            and primary_old_id
            and owners.get(primary_old_id) != uid
        ):
            _set_fully_new(
                row,
                reason="primary_collision",
                blocked_old=_blocked_old_from_row_primary(row),
            )
            continue

        if row.get("match_tier") != "none":
            _clear_build_hint(row)
            continue

        trace = next(
            (
                c
                for c in row.get("candidates") or []
                if c.get("match_tier") == "traceability"
            ),
            None,
        )
        trace_old_id = trace.get("old_lesson_id") if trace else None
        if trace_old_id and trace_old_id in owned_old_ids:
            _set_fully_new(
                row,
                reason="traceability",
                blocked_old=_blocked_old_from_candidate(trace),
            )
        else:
            _clear_build_hint(row)


def _lesson_candidate_from_row(
    les: Lesson,
    vol: Volume | None,
    *,
    include_body: bool = True,
) -> object | None:
    if not vol:
        return None
    return build_candidate(
        lesson_id=les.id,
        edition=vol.edition,
        grade=vol.grade,
        semester=vol.semester,
        unit_title=les.unit_title or "",
        lesson_no=les.lesson_no or "",
        lesson_name=les.lesson_name or "",
        old_course_id=les.old_course_id,
        page_count=les.page_count,
        body_text=les.body_text if include_body else None,
    )


def _resolve_similarity_score(
    new_les: Lesson,
    old_les: Lesson | None,
    volumes_by_id: dict[str, Volume],
    *,
    match_tier: str,
    stored: float | None,
) -> float | None:
    if stored is not None:
        return stored
    if not old_les or match_tier in ("none",):
        return None
    if match_tier == "exact":
        return 1.0
    new_c = _lesson_candidate_from_row(new_les, volumes_by_id.get(new_les.volume_id))
    old_c = _lesson_candidate_from_row(old_les, volumes_by_id.get(old_les.volume_id))
    if not new_c or not old_c:
        return None
    return similarity_score_for_pair(new_c, old_c)


def get_course_match_result(
    *,
    volume_code: str,
    job_id: str | None = None,
) -> dict:
    volume = get_new_volume_by_code(volume_code)
    if job_id:
        job = MatchJob.query.filter_by(id=job_id, new_volume_id=volume.id).first()
    else:
        job = _latest_job(volume.id)
    if not job:
        return {
            "ok": True,
            "volume_code": volume.volume_code,
            "job_id": None,
            "status": None,
            "summary": None,
            "matches_by_lesson": [],
        }

    matches = (
        LessonMatch.query.filter_by(job_id=job.id)
        .order_by(LessonMatch.new_lesson_id, LessonMatch.match_rank)
        .all()
    )
    lesson_ids = {m.new_lesson_id for m in matches} | {
        m.old_lesson_id for m in matches if m.old_lesson_id
    }
    lessons_by_id = {
        les.id: les
        for les in Lesson.query.filter(Lesson.id.in_(lesson_ids)).all()
    } if lesson_ids else {}
    vol_ids = {les.volume_id for les in lessons_by_id.values()}
    volumes_by_id = {
        v.id: v for v in Volume.query.filter(Volume.id.in_(vol_ids)).all()
    } if vol_ids else {}

    grouped: dict[str, list] = {}
    for m in matches:
        grouped.setdefault(m.new_lesson_id, []).append(m)

    by_lesson = []
    for new_id, rows in grouped.items():
        new_les = lessons_by_id.get(new_id)
        if not new_les:
            continue
        primary = rows[0]
        candidates = []
        for row in rows:
            old_les = lessons_by_id.get(row.old_lesson_id) if row.old_lesson_id else None
            live = _live_old_match_fields(
                old_les,
                volumes_by_id,
                stored_hint=row.old_lesson_hint,
                stored_courseware_id=row.old_courseware_id,
                stored_page_count=row.old_page_count,
            )
            score = _resolve_similarity_score(
                new_les,
                old_les,
                volumes_by_id,
                match_tier=row.match_tier,
                stored=row.similarity_score,
            )
            candidates.append(
                {
                    "rank": row.match_rank,
                    "match_tier": row.match_tier,
                    "match_label": TIER_LABELS.get(row.match_tier, row.match_tier),
                    "similarity_score": score,
                    "old_lesson_hint": live["old_lesson_hint"],
                    "old_courseware_id": live["old_courseware_id"],
                    "old_page_count": live["old_page_count"],
                    "old_lesson_id": row.old_lesson_id,
                    "old_lesson_uid": old_les.lesson_uid if old_les else None,
                    "old_volume_code": (
                        volumes_by_id[old_les.volume_id].volume_code
                        if old_les and old_les.volume_id in volumes_by_id
                        else None
                    ),
                }
            )
        primary_old = (
            lessons_by_id.get(primary.old_lesson_id) if primary.old_lesson_id else None
        )
        primary_live = _live_old_match_fields(
            primary_old,
            volumes_by_id,
            stored_hint=primary.old_lesson_hint,
            stored_courseware_id=primary.old_courseware_id,
            stored_page_count=primary.old_page_count,
        )
        primary_score = _resolve_similarity_score(
            new_les,
            primary_old,
            volumes_by_id,
            match_tier=primary.match_tier,
            stored=primary.similarity_score,
        )
        by_lesson.append(
            {
                "new_lesson_uid": new_les.lesson_uid,
                "new_unit_title": new_les.unit_title,
                "new_lesson_no": new_les.lesson_no,
                "new_lesson_name": new_les.lesson_name,
                "match_tier": primary.match_tier,
                "match_label": TIER_LABELS.get(primary.match_tier, primary.match_tier),
                "similarity_score": primary_score,
                "old_lesson_hint": primary_live["old_lesson_hint"],
                "old_courseware_id": primary_live["old_courseware_id"],
                "old_page_count": primary_live["old_page_count"],
                "candidates": candidates,
            }
        )

    by_lesson.sort(
        key=lambda row: (
            int(row["new_lesson_no"]) if str(row["new_lesson_no"]).isdigit() else 9999,
            row["new_lesson_name"],
        )
    )

    apply_build_hints(by_lesson)

    return {
        "ok": True,
        "volume_code": volume.volume_code,
        "job_id": job.id,
        "status": job.status,
        "summary": job.summary_json,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "matches_by_lesson": by_lesson,
    }


def build_hint_for_lesson(*, volume_code: str, lesson_uid: str) -> str | None:
    """单课粗分 build_hint（fully_new 等），供建块预扫描模式判定。"""
    result = get_course_match_result(volume_code=volume_code)
    for row in result.get("matches_by_lesson") or []:
        if row.get("new_lesson_uid") == lesson_uid:
            return row.get("build_hint")
    return None
