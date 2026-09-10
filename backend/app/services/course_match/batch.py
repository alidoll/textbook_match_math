"""粗分整册 batch：完全同名走规则，其余一次 LLM 对照全目录。"""
from __future__ import annotations

import logging

from ...parsers.lesson_reuse_match import MAX_DYNAMIC_OLD_HITS_CAP
from ..llm.config import llm_course_match_enabled
from ..llm.course_match_batch import BatchMatchRow, suggest_course_match_batch
from .engine import (
    LessonCandidate,
    MatchHit,
    match_new_lesson,
    similarity_score_for_pair,
    _sort_exact_hits,
)
from .theme_aliases import is_preferred_old_for_new, preferred_old_title_needles

logger = logging.getLogger(__name__)


def _pick_old(pool: list[LessonCandidate], idx: int | None) -> LessonCandidate | None:
    if idx is None or idx < 0 or idx >= len(pool):
        return None
    return pool[idx]


def _hits_from_batch_row(
    row: BatchMatchRow,
    new: LessonCandidate,
    old_pool: list[LessonCandidate],
) -> tuple[list[MatchHit], str | None]:
    hits: list[MatchHit] = []
    reason = (row.reason or "").strip() or None

    def _score(old: LessonCandidate | None) -> float | None:
        if not old:
            return None
        return similarity_score_for_pair(new, old)

    if row.primary_tier == "high_similarity":
        primary = _pick_old(old_pool, row.primary_old_index)
        if primary:
            hits.append(
                MatchHit(
                    tier="high_similarity",
                    similarity_score=_score(primary),
                    old=primary,
                    rank=1,
                )
            )
            trace = _pick_old(old_pool, row.traceability_old_index)
            if trace and trace.lesson_id != primary.lesson_id:
                hits.append(
                    MatchHit(
                        tier="traceability",
                        similarity_score=_score(trace),
                        old=trace,
                        rank=2,
                    )
                )
            return hits, reason

    hits.append(MatchHit(tier="none", similarity_score=None, old=None, rank=1))
    trace = _pick_old(old_pool, row.traceability_old_index)
    if trace:
        hits.append(
            MatchHit(
                tier="traceability",
                similarity_score=_score(trace),
                old=trace,
                rank=2,
            )
        )
    return hits, reason


def _apply_theme_alias_override(
    new: LessonCandidate,
    old_pool: list[LessonCandidate],
    exact_index: dict[str, list[LessonCandidate]],
    llm_hits: list[MatchHit],
) -> list[MatchHit] | None:
    """教研主题别名：LLM 未命中 preferred 旧课时，改用规则引擎结果。"""
    if not preferred_old_title_needles(new.lesson_text):
        return None
    llm_primary = next((h for h in llm_hits if h.rank == 1 and h.old), None)
    if llm_primary and is_preferred_old_for_new(
        new.lesson_text, llm_primary.old.lesson_text
    ):
        return None
    rule_hits = match_new_lesson(new, old_pool, exact_index)
    rule_primary = next(
        (h for h in rule_hits if h.rank == 1 and h.old and h.tier != "none"),
        None,
    )
    if not rule_primary:
        return None
    if not is_preferred_old_for_new(new.lesson_text, rule_primary.old.lesson_text):
        return None
    return rule_hits


def _exact_hits(
    new: LessonCandidate,
    exact_index: dict[str, list[LessonCandidate]],
) -> list[MatchHit] | None:
    if not new.reuse_key:
        return None
    exact = _sort_exact_hits(new, exact_index.get(new.reuse_key, []))[
        :MAX_DYNAMIC_OLD_HITS_CAP
    ]
    if not exact:
        return None
    return [
        MatchHit(tier="exact", similarity_score=1.0, old=old, rank=rank)
        for rank, old in enumerate(exact, start=1)
    ]


def match_volume_batch(
    *,
    edition: str,
    grade: int,
    semester: str,
    new_items: list[tuple[str, LessonCandidate]],
    old_pool: list[LessonCandidate],
    exact_index: dict[str, list[LessonCandidate]],
) -> dict[str, tuple[list[MatchHit], str, str | None]]:
    """
    new_items: (lesson_id, candidate)，已按课序排列。
    返回 lesson_id -> (hits, source, llm_reason)。
    """
    out: dict[str, tuple[list[MatchHit], str, str | None]] = {}
    need_llm: list[tuple[str, LessonCandidate, int]] = []
    batch_new: list[LessonCandidate] = []

    for lesson_id, new_cand in new_items:
        exact = _exact_hits(new_cand, exact_index)
        if exact:
            out[lesson_id] = (exact, "rules", None)
            continue
        batch_idx = len(batch_new)
        batch_new.append(new_cand)
        need_llm.append((lesson_id, new_cand, batch_idx))

    if not need_llm:
        return out

    if not llm_course_match_enabled():
        for lesson_id, new_cand, _ in need_llm:
            out[lesson_id] = (match_new_lesson(new_cand, old_pool, exact_index), "rules", None)
        return out

    try:
        rows = suggest_course_match_batch(
            edition=edition,
            grade=grade,
            semester=semester,
            new_lessons=batch_new,
            old_pool=old_pool,
        )
        by_new_index = {row.new_index: row for row in rows}
        for lesson_id, new_cand, batch_idx in need_llm:
            row = by_new_index.get(batch_idx)
            if row:
                hits, reason = _hits_from_batch_row(row, new_cand, old_pool)
                overridden = _apply_theme_alias_override(
                    new_cand, old_pool, exact_index, hits
                )
                if overridden is not None:
                    out[lesson_id] = (overridden, "rules", reason)
                else:
                    out[lesson_id] = (hits, "llm", reason)
            else:
                logger.warning("batch missing new_index=%s, fallback rules", batch_idx)
                out[lesson_id] = (
                    match_new_lesson(new_cand, old_pool, exact_index),
                    "llm_fallback",
                    None,
                )
    except Exception as exc:
        logger.warning("course match batch failed, fallback to rules: %s", exc)
        for lesson_id, new_cand, _ in need_llm:
            out[lesson_id] = (
                match_new_lesson(new_cand, old_pool, exact_index),
                "llm_fallback",
                None,
            )
    return out
