"""粗分混合模式：规则预筛 + 大模型语义判定。"""
from __future__ import annotations

import logging

from ..llm.config import llm_course_match_enabled
from ..llm.course_match_suggest import suggest_course_match_with_llm
from .engine import (
    LessonCandidate,
    MatchHit,
    collect_similar_hits,
    match_new_lesson,
    old_hint_for_candidate,
)

logger = logging.getLogger(__name__)

LLM_CANDIDATE_LIMIT = 15


def _rule_primary_hint(hits: list[MatchHit]) -> tuple[str | None, str | None]:
    primary = hits[0] if hits else None
    if not primary or primary.tier == "none":
        trace = next((h for h in hits if h.tier == "traceability" and h.old), None)
        if trace and trace.old:
            return "traceability", old_hint_for_candidate(trace.old)
        return "none", None
    if primary.old:
        return primary.tier, old_hint_for_candidate(primary.old)
    return primary.tier, None


def _hits_from_llm_decision(
    decision,
    candidates: list,
    *,
    rule_hits: list[MatchHit],
) -> list[MatchHit]:
    hits: list[MatchHit] = []

    def _pick(idx: int | None):
        if idx is None or idx < 0 or idx >= len(candidates):
            return None
        return candidates[idx]

    if decision.primary_tier == "high_similarity":
        item = _pick(decision.primary_index)
        if item:
            hits.append(
                MatchHit(
                    tier="high_similarity",
                    similarity_score=item.hybrid,
                    old=item.old,
                    rank=1,
                )
            )
            trace_item = _pick(decision.traceability_index)
            if trace_item and trace_item.old.lesson_id != item.old.lesson_id:
                hits.append(
                    MatchHit(
                        tier="traceability",
                        similarity_score=trace_item.hybrid,
                        old=trace_item.old,
                        rank=2,
                    )
                )
            return hits or rule_hits

    hits.append(MatchHit(tier="none", similarity_score=None, old=None, rank=1))
    trace_item = _pick(decision.traceability_index)
    if trace_item:
        hits.append(
            MatchHit(
                tier="traceability",
                similarity_score=trace_item.hybrid,
                old=trace_item.old,
                rank=2,
            )
        )
    elif len(rule_hits) > 1 and rule_hits[1].tier == "traceability" and rule_hits[1].old:
        hits.append(
            MatchHit(
                tier="traceability",
                similarity_score=rule_hits[1].similarity_score,
                old=rule_hits[1].old,
                rank=2,
            )
        )
    return hits


def match_new_lesson_hybrid(
    new: LessonCandidate,
    pool: list[LessonCandidate],
    exact_index: dict[str, list[LessonCandidate]],
) -> tuple[list[MatchHit], str, str | None]:
    """
    返回 (hits, source, llm_reason)，source 为 rules | llm | llm_fallback。
    完全同名仍走规则；其余在 LLM 可用时走混合判定。
    """
    from ...parsers.lesson_reuse_match import MAX_DYNAMIC_OLD_HITS_CAP
    from .engine import _sort_exact_hits

    if not new.reuse_key:
        return [MatchHit(tier="none", similarity_score=None, old=None, rank=1)], "rules", None

    exact_hits = _sort_exact_hits(new, exact_index.get(new.reuse_key, []))[
        :MAX_DYNAMIC_OLD_HITS_CAP
    ]
    if exact_hits:
        return [
            MatchHit(tier="exact", similarity_score=1.0, old=old, rank=rank)
            for rank, old in enumerate(exact_hits, start=1)
        ], "rules", None

    rule_hits = match_new_lesson(new, pool, exact_index)

    if not llm_course_match_enabled():
        return rule_hits, "rules", None

    candidates = collect_similar_hits(new, pool)[:LLM_CANDIDATE_LIMIT]
    if not candidates:
        return rule_hits, "rules", None

    rule_tier, rule_hint = _rule_primary_hint(rule_hits)
    try:
        decision = suggest_course_match_with_llm(
            new,
            candidates,
            rule_primary_tier=rule_tier,
            rule_primary_hint=rule_hint,
        )
        hits = _hits_from_llm_decision(decision, candidates, rule_hits=rule_hits)
        reason = (decision.reason or "").strip() or None
        return hits, "llm", reason
    except Exception as exc:
        logger.warning("course match LLM failed, fallback to rules: %s", exc)
        return rule_hits, "llm_fallback", None
