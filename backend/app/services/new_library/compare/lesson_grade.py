"""课时级五档定级（M3 规则引擎）。"""
from __future__ import annotations

from ....services.dictionary import list_dictionary_entries
from .block_profile import is_experiment_block
from .rules_config import (
    CONTINUOUS_REUSABLE_RATIO_VIDEO_CLIP,
    OPTIMIZE_RATIO_MATERIAL_RERECORD_MAX,
    OPTIMIZE_TEXT_ONLY_MAX,
    REFERENCE_RATIO_MATERIAL_RERECORD,
)

LESSON_GRADE_OPTIONS: list[dict[str, str | int]] = [
    {"code": "direct_reuse", "label": "直接复用", "order": 1},
    {"code": "video_clip", "label": "视频剪辑复用", "order": 2},
    {"code": "partial_rerecord", "label": "部分补录", "order": 3},
    {"code": "cw_material_video_rerecord", "label": "课件部分素材，视频重录", "order": 4},
    {"code": "new_production", "label": "新制", "order": 5},
]

_PAGE_LEVEL_OPTIONS: list[dict[str, str]] = [
    {"code": "mostly_reuse", "label": "基本沿用"},
    {"code": "partial_adjust", "label": "部分调整"},
    {"code": "major_rebuild", "label": "大幅重做"},
]

_ALLOWED_BY_GRADE: dict[str, frozenset[str]] = {
    "direct_reuse": frozenset({"reuse_as_is", "remove"}),
    "video_clip": frozenset({"reuse_as_is", "optimize", "remove"}),
    "partial_rerecord": frozenset({"reuse_as_is", "optimize", "remove"}),
    "cw_material_video_rerecord": frozenset({"reference", "optimize", "remove"}),
    "new_production": frozenset({"new_build", "remove"}),
}

_REUSABLE_CODES = frozenset({"reuse_as_is", "optimize"})


def _reuse_label_map() -> dict[str, str]:
    return {
        r["code"]: r["label"]
        for r in list_dictionary_entries(category="reuse_action")
    }


def effective_reuse_action(match: dict) -> str:
    code = (match.get("reuse_action") or "").strip()
    if code:
        return code
    return (match.get("ai_reuse_action") or "").strip()


def effective_optimize_subtype(match: dict) -> str | None:
    if effective_reuse_action(match) != "optimize":
        return None
    sub = (match.get("optimize_subtype") or match.get("ai_optimize_subtype") or "").strip()
    if sub in ("text_only", "re_voice"):
        return sub
    ratio = (match.get("ai_text_change") or {}).get("change_ratio")
    if ratio is not None:
        return "text_only" if float(ratio) <= OPTIMIZE_TEXT_ONLY_MAX else "re_voice"
    return None


def compute_action_stats(matches: list[dict]) -> dict:
    reuse_labels = _reuse_label_map()
    counts: dict[str, int] = {
        "reuse_as_is": 0,
        "optimize": 0,
        "reference": 0,
        "new_build": 0,
        "remove": 0,
        "pending": 0,
    }
    for m in matches:
        code = effective_reuse_action(m)
        if code in counts:
            counts[code] += 1
        elif code:
            counts["pending"] += 1
        else:
            counts["pending"] += 1

    total = sum(counts.values()) or 1
    breakdown = []
    for code, n in counts.items():
        if n <= 0:
            continue
        label = reuse_labels.get(code, "待判定" if code == "pending" else code)
        breakdown.append(
            {
                "code": code,
                "label": label,
                "count": n,
                "ratio": round(n / total, 3),
            }
        )
    return {
        "total": total,
        "counts": counts,
        "breakdown": breakdown,
    }


def _effective_block_ids(pre_stats: dict | None) -> set[str] | None:
    if not pre_stats:
        return None
    profiles = pre_stats.get("block_profiles") or []
    ids = {p["block_id"] for p in profiles if p.get("is_effective") and p.get("block_id")}
    return ids or None


def _counts_for_grade(matches: list[dict], *, effective_ids: set[str] | None) -> dict[str, int]:
    counts: dict[str, int] = {
        "reuse_as_is": 0,
        "optimize": 0,
        "reference": 0,
        "new_build": 0,
        "remove": 0,
        "pending": 0,
        "optimize_text_only": 0,
        "optimize_re_voice": 0,
    }
    for m in matches:
        nb = m.get("new_block_id")
        if effective_ids is not None and nb and nb not in effective_ids:
            continue
        action = effective_reuse_action(m)
        if not action:
            counts["pending"] += 1
            continue
        if action in counts:
            counts[action] += 1
        if action == "optimize":
            sub = effective_optimize_subtype(m)
            if sub == "re_voice":
                counts["optimize_re_voice"] += 1
            elif sub == "text_only":
                counts["optimize_text_only"] += 1
    return counts


def _estimate_modification_pct(counts: dict[str, int], total: int) -> int:
    if total <= 0:
        return 0
    weighted = (
        counts.get("reuse_as_is", 0) * 2
        + counts.get("optimize", 0) * 15
        + counts.get("reference", 0) * 50
        + counts.get("new_build", 0) * 90
        + counts.get("remove", 0) * 5
        + counts.get("pending", 0) * 40
    )
    return min(100, max(0, round(weighted / total)))


def _lesson_confidence(
    *,
    pending: int,
    continuous_ratio: float,
    continuous_qualified: bool,
    code: str,
) -> str:
    if pending:
        return "low"
    if code in ("direct_reuse", "new_production"):
        return "high"
    if continuous_qualified and abs(continuous_ratio - CONTINUOUS_REUSABLE_RATIO_VIDEO_CLIP) <= 0.05:
        return "low"
    if continuous_qualified and continuous_ratio >= CONTINUOUS_REUSABLE_RATIO_VIDEO_CLIP:
        return "high"
    return "medium"


def _pick_grade(
    *,
    counts: dict[str, int],
    effective_count: int,
    continuous_qualified: bool,
    continuous_ratio: float,
    experiment_retained: bool | None,
    pending: int,
) -> tuple[str, list[str]]:
    """按规则库 4.3 顺序判定，返回 (code, rationale_lines)。"""
    reasons: list[str] = []
    eff = effective_count or 1

    ref_ratio = counts.get("reference", 0) / eff
    new_ratio = counts.get("new_build", 0) / eff
    opt_re_voice = counts.get("optimize_re_voice", 0)
    has_reference = counts.get("reference", 0) > 0
    has_new_build = counts.get("new_build", 0) > 0
    has_optimize = counts.get("optimize", 0) > 0
    continuous_ok = continuous_qualified and continuous_ratio >= CONTINUOUS_REUSABLE_RATIO_VIDEO_CLIP

    only_as_is_and_remove = (
        counts.get("optimize", 0) == 0
        and counts.get("reference", 0) == 0
        and counts.get("new_build", 0) == 0
        and counts.get("reuse_as_is", 0) > 0
    )

    if pending:
        reasons.append(f"尚有 {pending} 个区块待判定")

    # 1. 直接复用
    if pending == 0 and only_as_is_and_remove:
        reasons.append("全部保留区块均为直接沿用，仅含删除项")
        return "direct_reuse", reasons

    # 5. 新制（先判极端情况）
    if new_ratio >= 0.45 or counts.get("new_build", 0) >= max(2, eff * 0.35):
        reasons.append(f"全新制作区块占有效模块 {int(new_ratio * 100)}%")
        return "new_production", reasons
    if not continuous_ok and continuous_ratio < CONTINUOUS_REUSABLE_RATIO_VIDEO_CLIP and has_new_build:
        reasons.append("连续可复用不足 1/3 且存在全新制作块")
        return "new_production", reasons

    # 4. 课件部分素材，视频重录（任一触发）
    material_triggers: list[str] = []
    if experiment_retained is False:
        material_triggers.append("核心实验未完整保留")
    if ref_ratio > REFERENCE_RATIO_MATERIAL_RERECORD or has_reference:
        material_triggers.append(
            f"参考素材重制区块 {counts.get('reference', 0)} 个（{int(ref_ratio * 100)}%）"
        )
    if not continuous_ok:
        material_triggers.append(
            f"连续可复用组未达 1/3（当前 {int(continuous_ratio * 100)}%）"
        )
    if has_new_build and has_reference:
        material_triggers.append("同时存在全新制作与参考重制")

    strict_material = (
        has_reference
        and counts.get("reuse_as_is", 0) == 0
        and counts.get("optimize", 0) <= max(1, int(eff * OPTIMIZE_RATIO_MATERIAL_RERECORD_MAX))
    )
    if strict_material:
        material_triggers.append("以参考重制为主，几乎无直接沿用")

    if material_triggers:
        reasons.extend(material_triggers)
        return "cw_material_video_rerecord", reasons

    # 2. 视频剪辑复用（全部满足）
    experiment_ok = experiment_retained is not False
    if (
        pending == 0
        and experiment_ok
        and continuous_ok
        and not has_reference
        and not has_new_build
        and opt_re_voice == 0
    ):
        if has_optimize:
            reasons.append("核心实验保留，连续可复用≥1/3，优化块无需重配音")
        else:
            reasons.append("核心实验保留，连续可复用≥1/3，均为直接沿用")
        return "video_clip", reasons

    # 3. 部分补录
    if (
        pending == 0
        and experiment_ok
        and continuous_ok
        and not has_reference
        and not has_new_build
        and opt_re_voice > 0
    ):
        reasons.append(f"连续可复用达标，{opt_re_voice} 个优化块需重录配音")
        return "partial_rerecord", reasons

    if continuous_ok and has_optimize and not has_reference and not has_new_build:
        reasons.append("连续可复用达标，存在优化调整")
        return "partial_rerecord", reasons

    if reusable := counts.get("reuse_as_is", 0) + counts.get("optimize", 0):
        reasons.append(f"可复用动作 {reusable} 块，但未完全满足剪辑/补录条件")
    else:
        reasons.append("有效可复用内容较少")

    return "new_production", reasons


def infer_lesson_grade(
    *,
    matches: list[dict],
    new_blocks: list[dict],
    old_blocks: list[dict] | None = None,
    pre_stats: dict | None = None,
) -> dict:
    stats = compute_action_stats(matches)
    effective_ids = _effective_block_ids(pre_stats)
    counts = _counts_for_grade(matches, effective_ids=effective_ids)
    pending = counts.get("pending", 0)

    effective_count = (pre_stats or {}).get("effective_block_count") or len(new_blocks) or stats["total"]
    eff = effective_count or 1

    continuous = (pre_stats or {}).get("continuous_reusable") or {}
    continuous_ratio = continuous.get("ratio") or 0.0
    continuous_qualified = bool(continuous.get("qualified"))

    experiment = (pre_stats or {}).get("core_experiment") or {}
    experiment_retained = experiment.get("retained")

    reusable_blocks = counts.get("reuse_as_is", 0) + counts.get("optimize", 0)
    reusable_ratio = round(reusable_blocks / eff, 3)

    if continuous_qualified:
        pass  # rationale filled in _pick_grade caller
    elif (pre_stats or {}).get("effective_block_count"):
        pass

    code, reason_lines = _pick_grade(
        counts=counts,
        effective_count=eff,
        continuous_qualified=continuous_qualified,
        continuous_ratio=continuous_ratio,
        experiment_retained=experiment_retained,
        pending=pending,
    )

    if continuous_qualified:
        reason_lines.insert(
            0,
            f"最长连续可复用 {continuous.get('length', 0)} 块，占有效区块 {int(continuous_ratio * 100)}%",
        )
    elif pre_stats:
        reason_lines.append("尚无长度≥2 的连续可复用组")

    if experiment.get("has_experiment"):
        if experiment_retained is True:
            reason_lines.append("核心实验完整保留")
        elif experiment_retained is False:
            reason_lines.append("核心实验未完整保留")

    grade = next((g for g in LESSON_GRADE_OPTIONS if g["code"] == code), LESSON_GRADE_OPTIONS[4])
    mod_pct = _estimate_modification_pct(counts, stats["total"])
    confidence = _lesson_confidence(
        pending=pending,
        continuous_ratio=continuous_ratio,
        continuous_qualified=continuous_qualified,
        code=code,
    )

    violations: list[dict] = []
    allowed = _ALLOWED_BY_GRADE.get(code, frozenset())
    reuse_labels = _reuse_label_map()
    old_by_id = {b["block_id"]: b for b in (old_blocks or [])}
    new_by_id = {b["block_id"]: b for b in new_blocks}

    for m in matches:
        action = effective_reuse_action(m)
        if not action or action == "pending":
            continue
        if action not in allowed:
            violations.append(
                {
                    "match_id": m.get("match_id"),
                    "block_id": m.get("new_block_id") or m.get("old_block_id"),
                    "action": action,
                    "action_label": reuse_labels.get(action, action),
                    "message": f"课时定级「{grade['label']}」通常不允许「{reuse_labels.get(action, action)}」",
                }
            )
        old_block = old_by_id.get(m.get("old_block_id") or "")
        new_block = new_by_id.get(m.get("new_block_id") or "")
        if is_experiment_block(old_block) or is_experiment_block(new_block):
            if action in ("optimize", "reference"):
                violations.append(
                    {
                        "match_id": m.get("match_id"),
                        "block_id": m.get("new_block_id") or m.get("old_block_id"),
                        "action": action,
                        "action_label": reuse_labels.get(action, action),
                        "message": "实验类区块建议整块沿用或整块重制",
                    }
                )

    grade_stats = {
        "effective_block_count": effective_count,
        "action_counts": {k: v for k, v in counts.items() if not k.startswith("optimize_")},
        "optimize_text_only_count": counts.get("optimize_text_only", 0),
        "optimize_re_voice_count": counts.get("optimize_re_voice", 0),
        "reference_ratio": round(counts.get("reference", 0) / eff, 3),
        "new_build_ratio": round(counts.get("new_build", 0) / eff, 3),
        "continuous_reusable_length": continuous.get("length", 0),
        "continuous_reusable_ratio": continuous_ratio,
        "continuous_reusable_qualified": continuous_qualified,
        "core_experiment_retained": experiment_retained,
    }

    return {
        "code": grade["code"],
        "label": grade["label"],
        "confidence": confidence,
        "reusable_block_ratio": reusable_ratio,
        "continuous_reusable_ratio": continuous_ratio,
        "continuous_reusable_qualified": continuous_qualified,
        "core_experiment_retained": experiment_retained,
        "modification_estimate_pct": mod_pct,
        "rationale": "；".join(reason_lines) if reason_lines else "依据规则库 4.3 自动推断",
        "rationale_lines": reason_lines,
        "pending_count": pending,
        "constraint_violations": violations,
        "allowed_actions": sorted(_ALLOWED_BY_GRADE.get(code, frozenset())),
        "grade_stats": grade_stats,
    }


def compute_page_levels(
    *,
    matches: list[dict],
    new_blocks: list[dict],
) -> list[dict]:
    """页面级快速浏览：每页可复用区块占比。"""
    by_page: dict[int, list[str]] = {}
    for b in new_blocks:
        for pg in b.get("new_tb_pgs") or []:
            by_page.setdefault(int(pg), []).append(b["block_id"])

    match_by_new: dict[str, str] = {}
    for m in matches:
        nb = m.get("new_block_id")
        if nb:
            match_by_new[nb] = effective_reuse_action(m)

    rows: list[dict] = []
    for pg in sorted(by_page.keys()):
        block_ids = by_page[pg]
        total = len(block_ids) or 1
        reusable = sum(
            1 for bid in block_ids if match_by_new.get(bid) in _REUSABLE_CODES
        )
        ratio = round(reusable / total, 3)
        if ratio >= 0.8:
            level = "mostly_reuse"
        elif ratio >= 0.5:
            level = "partial_adjust"
        else:
            level = "major_rebuild"
        label = next(x["label"] for x in _PAGE_LEVEL_OPTIONS if x["code"] == level)
        rows.append(
            {
                "page_index": pg,
                "reusable_ratio": ratio,
                "level_code": level,
                "level_label": label,
                "block_count": total,
            }
        )
    return rows


def build_lesson_insights(
    *,
    matches: list[dict],
    new_blocks: list[dict],
    old_blocks: list[dict] | None = None,
    float_atoms: list[dict] | None = None,
) -> dict:
    from .compare_qa import run_compare_qa
    from .pre_stats import build_pre_stats

    pre_stats = build_pre_stats(
        new_blocks=new_blocks,
        matches=matches,
        float_atoms=float_atoms,
    )
    qa_flags = run_compare_qa(
        matches=matches,
        new_blocks=new_blocks,
        old_blocks=old_blocks or [],
    )
    return {
        "pre_stats": pre_stats,
        "qa_flags": qa_flags,
        "lesson_grade": infer_lesson_grade(
            matches=matches,
            new_blocks=new_blocks,
            old_blocks=old_blocks,
            pre_stats=pre_stats,
        ),
        "action_stats": compute_action_stats(matches),
        "page_levels": compute_page_levels(matches=matches, new_blocks=new_blocks),
        "lesson_grade_options": LESSON_GRADE_OPTIONS,
    }
