"""粗分匹配引擎：新课时 ↔ 旧库课时池。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from ...parsers.lesson_reuse_match import (
    MAX_DYNAMIC_OLD_HITS_CAP,
    REF_CTX_FLOOR,
    REF_SIM_HARD_JUNK_CTX,
    REF_SIM_HARD_JUNK_LES,
    REF_SIM_HARD_JUNK_UNIT,
    REF_SIM_STRONG_LESSON,
    REF_SIM_THRESHOLD,
    REF_UNIT_RESCUE_FLOOR,
    body_text_similarity_score,
    format_old_lesson_hint,
    lesson_ref_similarity_score,
    lesson_reuse_match_key,
    looks_like_unit_lesson_title_swap,
    ref_similarity_matter_trilogy_bonus,
    ref_similarity_multi_paths,
    unit_lesson_ref_context_score,
    unit_ref_similarity_score,
)
from .theme_aliases import (
    THEME_ALIAS_LES_FLOOR,
    preferred_old_title_needles,
    qualifies_theme_alias_core,
    theme_alias_boost,
)

MatchTier = Literal["exact", "high_similarity", "traceability", "none"]

TIER_LABELS = {
    "exact": "完全同名",
    "high_similarity": "高相似",
    "traceability": "基础溯源",
    "none": "无匹配",
}

# 标题 + 正文混合粗分
BODY_WEIGHT_NORMAL = 0.35
BODY_WEIGHT_BOOST = 0.55
TITLE_TOP_CLOSE_DELTA = 0.08

# 核心对照：跨年级需更高单元/语境一致；禁止低关联跨学段强行配对
CORE_MAX_GRADE_GAP = 2
CORE_CROSS_GRADE_MIN_UNIT = 0.38
CORE_CROSS_GRADE_MIN_CTX = 0.42
CORE_SAME_UNIT_MIN_LES = 0.68
CORE_PARTIAL_LES_TRAP_LO = 0.36
TRACEABILITY_MIN_UNIT = 0.28
TRACEABILITY_MIN_LES = 0.48
TRACEABILITY_MIN_CTX = 0.36


@dataclass(frozen=True)
class LessonCandidate:
    lesson_id: str
    edition: str
    grade: int
    semester: str
    unit_title: str
    lesson_text: str
    lesson_no: str
    lesson_name: str
    old_course_id: str | None
    page_count: int | None
    reuse_key: str
    body_text: str | None = None


@dataclass(frozen=True)
class MatchHit:
    tier: MatchTier
    similarity_score: float | None
    old: LessonCandidate | None
    rank: int


@dataclass(frozen=True)
class _ScoredOld:
    hybrid: float
    old: LessonCandidate
    sc_les: float
    sc_unit: float
    sc_ctx: float


def lesson_display_text(*, lesson_no: str, lesson_name: str) -> str:
    no = (lesson_no or "").strip()
    name = (lesson_name or "").strip()
    if no and no != "0":
        return f"{no} {name}".strip()
    return name


def build_candidate(
    *,
    lesson_id: str,
    edition: str,
    grade: int,
    semester: str,
    unit_title: str,
    lesson_no: str,
    lesson_name: str,
    old_course_id: str | None,
    page_count: int | None,
    body_text: str | None = None,
) -> LessonCandidate | None:
    lesson_text = lesson_display_text(lesson_no=lesson_no, lesson_name=lesson_name)
    if not lesson_text or "单元小结" in lesson_text:
        return None
    reuse_key = lesson_reuse_match_key(lesson_text)
    if not reuse_key:
        return None
    return LessonCandidate(
        lesson_id=lesson_id,
        edition=edition,
        grade=grade,
        semester=semester,
        unit_title=(unit_title or "").strip() or "未命名单元",
        lesson_text=lesson_text,
        lesson_no=lesson_no,
        lesson_name=lesson_name,
        old_course_id=old_course_id,
        page_count=page_count,
        reuse_key=reuse_key,
        body_text=(body_text or "").strip() or None,
    )


def build_exact_index(pool: list[LessonCandidate]) -> dict[str, list[LessonCandidate]]:
    idx: dict[str, list[LessonCandidate]] = defaultdict(list)
    for item in pool:
        if item.reuse_key:
            idx[item.reuse_key].append(item)
    return idx


def _grade_gap(new: LessonCandidate, old: LessonCandidate) -> int:
    return abs(new.grade - old.grade)


def _location_rank(new: LessonCandidate, old: LessonCandidate) -> tuple[int, int]:
    """越小越优先：同册 > 同年级 > 邻近年级 > 远年级。"""
    gap = _grade_gap(new, old)
    same_grade = gap == 0
    same_sem = new.semester == old.semester
    if same_grade and same_sem:
        return (0, 0)
    if same_grade:
        return (1, 0)
    if gap == 1:
        return (2, gap)
    if gap == 2:
        return (3, gap)
    return (4, gap)


def _sort_exact_hits(new: LessonCandidate, hits: list[LessonCandidate]) -> list[LessonCandidate]:
    return sorted(hits, key=lambda old: (_location_rank(new, old), old.lesson_text))


def _body_weight_for_pair(
    new: LessonCandidate,
    old: LessonCandidate,
    *,
    title_top_close: bool,
) -> float:
    if not new.body_text or not old.body_text:
        return 0.0
    if title_top_close or looks_like_unit_lesson_title_swap(
        new.unit_title,
        new.lesson_text,
        old.unit_title,
        old.lesson_text,
    ):
        return BODY_WEIGHT_BOOST
    return BODY_WEIGHT_NORMAL


def _hybrid_similarity_score(
    new: LessonCandidate,
    old: LessonCandidate,
    title_score: float,
    *,
    title_top_close: bool,
) -> float:
    body_w = _body_weight_for_pair(new, old, title_top_close=title_top_close)
    if body_w <= 0:
        return title_score
    body_score = body_text_similarity_score(new.body_text, old.body_text)
    if body_score is None:
        return title_score
    return float(title_score * (1.0 - body_w) + body_score * body_w)


def _same_unit_theme(new: LessonCandidate, old: LessonCandidate, sc_unit: float) -> bool:
    return sc_unit >= REF_UNIT_RESCUE_FLOOR


def _qualifies_core_match(new: LessonCandidate, item: _ScoredOld) -> bool:
    gap = _grade_gap(new, item.old)
    # 教研主题别名（如月相/地月系/探月）：允许作核心对照
    if qualifies_theme_alias_core(
        new.lesson_text, item.old.lesson_text, grade_gap=gap
    ) and item.hybrid >= REF_SIM_THRESHOLD:
        return True
    if item.hybrid < REF_SIM_THRESHOLD:
        return False
    if gap >= 3:
        return False
    if gap >= 2 and item.sc_unit < CORE_CROSS_GRADE_MIN_UNIT:
        return False
    if gap >= 1 and item.sc_unit < CORE_CROSS_GRADE_MIN_UNIT and item.sc_ctx < CORE_CROSS_GRADE_MIN_CTX:
        return False
    if (
        _same_unit_theme(new, item.old, item.sc_unit)
        and CORE_PARTIAL_LES_TRAP_LO <= item.sc_les < CORE_SAME_UNIT_MIN_LES
    ):
        return False
    if (
        gap == 0
        and item.sc_unit >= REF_UNIT_RESCUE_FLOOR
        and item.sc_ctx >= CORE_CROSS_GRADE_MIN_CTX
        and item.sc_les < CORE_PARTIAL_LES_TRAP_LO
    ):
        return True
    if _same_unit_theme(new, item.old, item.sc_unit) and item.sc_les < CORE_SAME_UNIT_MIN_LES:
        return False
    if item.sc_unit < 0.32 and item.sc_les < REF_SIM_STRONG_LESSON and item.sc_ctx < REF_CTX_FLOOR:
        return False
    return item.sc_les >= REF_SIM_STRONG_LESSON or item.sc_ctx >= CORE_CROSS_GRADE_MIN_CTX


def _qualifies_traceability(new: LessonCandidate, item: _ScoredOld) -> bool:
    if _qualifies_core_match(new, item):
        return False
    gap = _grade_gap(new, item.old)
    if gap < 1:
        return False
    if item.hybrid < REF_SIM_THRESHOLD:
        return False
    if item.sc_unit >= TRACEABILITY_MIN_UNIT:
        return True
    if item.sc_les >= TRACEABILITY_MIN_LES and item.sc_ctx >= TRACEABILITY_MIN_CTX:
        return True
    return False


def _qualifies_runner_up(
    new: LessonCandidate,
    item: _ScoredOld,
    core: _ScoredOld | None,
) -> bool:
    """同年级名称/单元备选（rank 2+），供 Step 0 换主课与预扫描。"""
    if core and item.old.lesson_id == core.old.lesson_id:
        return False
    if _grade_gap(new, item.old) > 0:
        return False
    if item.hybrid < REF_SIM_THRESHOLD:
        return False
    if _qualifies_core_match(new, item):
        return True
    if _same_unit_theme(new, item.old, item.sc_unit) and item.sc_les >= CORE_PARTIAL_LES_TRAP_LO:
        return True
    if core and (core.hybrid - item.hybrid) < TITLE_TOP_CLOSE_DELTA:
        return True
    return False


def _seen_old_ids(hits: list[MatchHit]) -> set[str]:
    out: set[str] = set()
    for h in hits:
        if h.old:
            out.add(h.old.lesson_id)
    return out


def _append_secondary_hits(
    hits: list[MatchHit],
    *,
    new: LessonCandidate,
    similar: list[_ScoredOld],
    core: _ScoredOld | None,
    start_rank: int,
    max_rank: int = 4,
) -> None:
    """rank 2+：先同年级名称备选，再跨年级 traceability。"""
    rank = start_rank
    seen = _seen_old_ids(hits)

    for item in similar:
        if rank > max_rank:
            break
        oid = item.old.lesson_id
        if oid in seen:
            continue
        if not _qualifies_runner_up(new, item, core):
            continue
        hits.append(
            MatchHit(
                tier="high_similarity",
                similarity_score=item.hybrid,
                old=item.old,
                rank=rank,
            )
        )
        seen.add(oid)
        rank += 1

    for item in similar:
        if rank > max_rank:
            break
        oid = item.old.lesson_id
        if oid in seen:
            continue
        if not _qualifies_traceability(new, item):
            continue
        hits.append(
            MatchHit(
                tier="traceability",
                similarity_score=item.hybrid,
                old=item.old,
                rank=rank,
            )
        )
        seen.add(oid)
        rank += 1


def collect_similar_hits(
    new: LessonCandidate,
    pool: list[LessonCandidate],
) -> list[_ScoredOld]:
    if not new.reuse_key or not new.lesson_text:
        return []
    title_scored: list[tuple[float, float, float, float, LessonCandidate]] = []
    seen_ids: set[str] = set()
    for old in pool:
        if old.reuse_key == new.reuse_key:
            continue
        sc_final, sc_les, sc_unit, sc_ctx = ref_similarity_multi_paths(
            new.unit_title,
            new.lesson_text,
            old.unit_title,
            old.lesson_text,
        )
        boost = theme_alias_boost(new.lesson_text, old.lesson_text)
        if boost > 0:
            sc_final = min(1.0, sc_final + boost)
            sc_les = min(1.0, max(sc_les, THEME_ALIAS_LES_FLOOR) + boost * 0.5)
        alias_hit = boost > 0
        if (
            not alias_hit
            and sc_les < REF_SIM_HARD_JUNK_LES
            and sc_unit < REF_SIM_HARD_JUNK_UNIT
            and sc_ctx < REF_SIM_HARD_JUNK_CTX
        ):
            continue
        if (
            not alias_hit
            and sc_ctx < REF_CTX_FLOOR
            and sc_les < REF_SIM_STRONG_LESSON
            and sc_unit < REF_UNIT_RESCUE_FLOOR
        ):
            continue
        if sc_final < REF_SIM_THRESHOLD and not alias_hit:
            continue
        if sc_final < REF_SIM_THRESHOLD and alias_hit:
            sc_final = REF_SIM_THRESHOLD
        title_scored.append((sc_final, sc_les, sc_unit, sc_ctx, old))
        seen_ids.add(old.lesson_id)

    # 主题 preferred 旧课：字符串分不够时仍注入候选
    pref_needles = preferred_old_title_needles(new.lesson_text)
    if pref_needles:
        from ...parsers.lesson_reuse_match import lesson_reuse_match_key as _rk

        pref_keys = {_rk(t) for t in pref_needles if _rk(t)}
        for old in pool:
            if old.lesson_id in seen_ids or old.reuse_key == new.reuse_key:
                continue
            ok = old.reuse_key or _rk(old.lesson_text)
            if not ok or not any(pk == ok or pk in ok or ok in pk for pk in pref_keys):
                continue
            sc_final, sc_les, sc_unit, sc_ctx = ref_similarity_multi_paths(
                new.unit_title,
                new.lesson_text,
                old.unit_title,
                old.lesson_text,
            )
            boost = theme_alias_boost(new.lesson_text, old.lesson_text)
            sc_final = min(1.0, max(sc_final, REF_SIM_THRESHOLD) + boost)
            sc_les = min(1.0, max(sc_les, THEME_ALIAS_LES_FLOOR))
            title_scored.append((sc_final, sc_les, sc_unit, sc_ctx, old))
            seen_ids.add(old.lesson_id)

    if not title_scored:
        return []

    title_scored.sort(key=lambda x: -x[0])
    title_top_close = False
    if len(title_scored) >= 2:
        title_top_close = (title_scored[0][0] - title_scored[1][0]) < TITLE_TOP_CLOSE_DELTA

    hybrid_scored: list[_ScoredOld] = []
    for title_score, sc_les, sc_unit, sc_ctx, old in title_scored:
        hybrid = _hybrid_similarity_score(
            new,
            old,
            title_score,
            title_top_close=title_top_close,
        )
        boost = theme_alias_boost(new.lesson_text, old.lesson_text)
        if boost > 0:
            hybrid = min(1.0, hybrid + boost * 0.35)
        hybrid_scored.append(
            _ScoredOld(hybrid=hybrid, old=old, sc_les=sc_les, sc_unit=sc_unit, sc_ctx=sc_ctx)
        )

    hybrid_scored.sort(
        key=lambda x: (
            0 if qualifies_theme_alias_core(
                new.lesson_text, x.old.lesson_text, grade_gap=_grade_gap(new, x.old)
            )
            and theme_alias_boost(new.lesson_text, x.old.lesson_text) > 0
            else 1,
            _location_rank(new, x.old),
            -x.hybrid,
            -ref_similarity_matter_trilogy_bonus(new.unit_title, x.old.unit_title),
            -x.sc_unit,
            -x.sc_les,
            -x.sc_ctx,
            x.old.lesson_text,
        )
    )
    return hybrid_scored


def match_new_lesson(
    new: LessonCandidate,
    pool: list[LessonCandidate],
    exact_index: dict[str, list[LessonCandidate]],
) -> list[MatchHit]:
    if not new.reuse_key:
        return [MatchHit(tier="none", similarity_score=None, old=None, rank=1)]

    exact_hits = _sort_exact_hits(new, exact_index.get(new.reuse_key, []))[
        :MAX_DYNAMIC_OLD_HITS_CAP
    ]
    if exact_hits:
        return [
            MatchHit(tier="exact", similarity_score=1.0, old=old, rank=rank)
            for rank, old in enumerate(exact_hits, start=1)
        ]

    similar = collect_similar_hits(new, pool)
    if not similar:
        return [MatchHit(tier="none", similarity_score=None, old=None, rank=1)]

    hits: list[MatchHit] = []
    core = next((s for s in similar if _qualifies_core_match(new, s)), None)
    if core:
        hits.append(
            MatchHit(
                tier="high_similarity",
                similarity_score=core.hybrid,
                old=core.old,
                rank=1,
            )
        )
        _append_secondary_hits(
            hits,
            new=new,
            similar=similar,
            core=core,
            start_rank=2,
        )
        return hits

    trace = next((s for s in similar if _qualifies_traceability(new, s)), None)
    if trace:
        hits.append(MatchHit(tier="none", similarity_score=None, old=None, rank=1))
        hits.append(
            MatchHit(
                tier="traceability",
                similarity_score=trace.hybrid,
                old=trace.old,
                rank=2,
            )
        )
        _append_secondary_hits(
            hits,
            new=new,
            similar=similar,
            core=None,
            start_rank=3,
        )
        return hits

    return [MatchHit(tier="none", similarity_score=None, old=None, rank=1)]


def similarity_score_for_pair(
    new: LessonCandidate,
    old: LessonCandidate,
) -> float:
    """规则混合相似度（单元+课时+语境，有正文时混入 OCR），供展示与 LLM 对照参考。"""
    sc_final, _, _, _ = ref_similarity_multi_paths(
        new.unit_title,
        new.lesson_text,
        old.unit_title,
        old.lesson_text,
    )
    hybrid = _hybrid_similarity_score(new, old, sc_final, title_top_close=False)
    return round(float(hybrid), 4)


def score_components_for_pair(
    new: LessonCandidate,
    old: LessonCandidate,
) -> dict[str, float | None]:
    """分项得分，写入 lesson_match_signals。"""
    sc_final, sc_les, sc_unit, sc_ctx = ref_similarity_multi_paths(
        new.unit_title,
        new.lesson_text,
        old.unit_title,
        old.lesson_text,
    )
    hybrid = _hybrid_similarity_score(new, old, sc_final, title_top_close=False)
    body_score = body_text_similarity_score(new.body_text, old.body_text)
    page_delta = None
    if new.page_count is not None and old.page_count is not None:
        page_delta = int(new.page_count) - int(old.page_count)
    return {
        "title_score": round(float(sc_final), 4),
        "body_score": round(float(body_score), 4) if body_score is not None else None,
        "unit_score": round(float(sc_unit), 4),
        "context_score": round(float(sc_ctx), 4),
        "hybrid_score": round(float(hybrid), 4),
        "page_count_delta": page_delta,
        "content_verified": bool(new.body_text and old.body_text),
    }


def old_hint_for_candidate(old: LessonCandidate) -> str:
    grade_label = f"{old.grade}年级{old.semester}"
    base = format_old_lesson_hint(old.unit_title, old.lesson_text)
    return f"{grade_label}｜{base}"
