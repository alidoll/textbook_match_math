"""区块级判定引擎（M2）：动作 + 依据 + 修改要点 + 置信度。"""
from __future__ import annotations

from .block_profile import is_experiment_block
from .rules_config import (
    MATCH_SCORE_GAP_LOW_CONFIDENCE,
    OPTIMIZE_TEXT_ONLY_MAX,
    TEXT_CHANGE_DIRECT_REUSE_MAX,
    TEXT_CHANGE_NEW_BUILD_MIN,
    TEXT_CHANGE_OPTIMIZE_MAX,
    TEXT_CHANGE_REFERENCE_MIN,
)
from .text_diff import compute_text_change_ratio, diff_snippets

_BUCKET_TO_ACTION = {
    "identical": "reuse_as_is",
    "direct_reuse": "reuse_as_is",
    "optimize": "optimize",
    "reference": "reference",
    "new_build": "new_build",
}

_LITERACY_NEW_KWS = ("反思", "评价", "量规", "素养", "信箱", "填空", "记录单")
_ACTIVITY_KWS = ("实验", "探究", "活动", "操作", "演示")


def _match_scores(new_block: dict | None) -> tuple[float | None, float | None]:
    if not new_block:
        return None, None
    meta = new_block.get("metadata") or {}
    try:
        s1 = float(meta["match_score"]) if meta.get("match_score") is not None else None
    except (TypeError, ValueError):
        s1 = None
    try:
        s2 = float(meta["match_score_top2"]) if meta.get("match_score_top2") is not None else None
    except (TypeError, ValueError):
        s2 = None
    return s1, s2


def _confidence(
    *,
    action: str,
    change_ratio: float,
    match_score: float | None,
    match_score_top2: float | None,
    has_both_blocks: bool,
) -> str:
    if not has_both_blocks:
        return "high"
    if match_score is not None and match_score_top2 is not None:
        if (match_score - match_score_top2) < MATCH_SCORE_GAP_LOW_CONFIDENCE:
            return "low"
    boundaries = (
        TEXT_CHANGE_DIRECT_REUSE_MAX,
        TEXT_CHANGE_OPTIMIZE_MAX,
        TEXT_CHANGE_REFERENCE_MIN,
        TEXT_CHANGE_NEW_BUILD_MIN,
    )
    for b in boundaries:
        if abs(change_ratio - b) <= 0.03:
            return "low"
    if match_score is not None and match_score < 0.35:
        return "low"
    if match_score is not None and match_score >= 0.6:
        return "high"
    return "medium"


def _optimize_subtype(change_ratio: float, combined: str) -> str | None:
    if change_ratio <= OPTIMIZE_TEXT_ONLY_MAX:
        return "text_only"
    if any(kw in combined for kw in ("旁白", "讲解", "台词", "配音", "引导语")):
        return "re_voice"
    if change_ratio > OPTIMIZE_TEXT_ONLY_MAX:
        return "re_voice"
    return "text_only"


def _build_change_points(
    *,
    old_block: dict | None,
    new_block: dict | None,
    old_text: str,
    new_text: str,
    action: str,
    change_ratio: float,
    change_type: str,
) -> list[str]:
    points: list[str] = []
    snippets = diff_snippets(old_text, new_text)

    if old_block and new_block:
        on = (old_block.get("block_name") or "").strip()
        nn = (new_block.get("block_name") or "").strip()
        if on and nn and on != nn:
            points.append(f"区块名称由「{on}」改为「{nn}」")

    combined = f"{old_text} {new_text} {(old_block or {}).get('block_name', '')} {(new_block or {}).get('block_name', '')}"

    if action == "reuse_as_is":
        points.append("核心图文一致，可直接沿用旧课件")
    elif action == "optimize":
        if change_ratio <= OPTIMIZE_TEXT_ONLY_MAX:
            points.append("微调文字/案例，视频画面无需改动")
        else:
            points.append("引导话术改动较多，可能需要重录配音")
        if snippets["old_excerpt"] != snippets["new_excerpt"] and snippets["old_excerpt"]:
            points.append(f"文字：{snippets['old_excerpt']} → {snippets['new_excerpt']}")
    elif action == "reference":
        points.append("保留版式思路，实验/核心内容需重制")
        if any(kw in combined for kw in _ACTIVITY_KWS):
            points.append("探究/实验流程有更换，视频需重拍")
    elif action == "new_build":
        points.append("新旧差异大或无旧对应，需从零制作")
    elif action == "remove":
        points.append("新教材已删除该模块，旧课件内容剔除")

    if change_type == "L001":
        points.append("新增素养/评价类栏目要素")
    if any(kw in combined for kw in _LITERACY_NEW_KWS) and action == "optimize":
        points.append("检查是否新增填空框/评价量表")

    return points[:5]


def _build_rationale(
    *,
    action: str,
    change_ratio: float,
    diff_bucket: str,
    is_experiment: bool,
    match_score: float | None,
    base_note: str,
) -> list[str]:
    lines = [f"文本改动量约 {int(change_ratio * 100)}%，落入「{diff_bucket}」区间"]
    if match_score is not None:
        lines.append(f"锚定匹配度 {int(match_score * 100)}%")
    if is_experiment:
        lines.append("本块含实验/探究要素，按整块判定")
    action_labels = {
        "reuse_as_is": "核心一致，判定直接沿用",
        "optimize": "核心保留、非核心微调，判定优化调整",
        "reference": "原理相近但实验或结构大改，判定参考素材重制",
        "new_build": "无复用基础或全新模块，判定全新制作",
        "remove": "新教材删除旧内容，判定完全删除",
    }
    if action in action_labels:
        lines.append(action_labels[action])
    if base_note:
        lines.append(base_note[:80] + ("…" if len(base_note) > 80 else ""))
    return lines[:4]


def _refine_action_from_diff(
    base_action: str,
    *,
    change_ratio: float,
    diff_bucket: str,
    is_experiment: bool,
    old_block: dict | None,
    new_block: dict | None,
) -> str:
    if not old_block or not new_block:
        return base_action

    diff_action = _BUCKET_TO_ACTION.get(diff_bucket, base_action)

    if base_action in ("remove", "new_build"):
        return base_action

    if is_experiment and diff_action in ("optimize", "reference"):
        if change_ratio >= TEXT_CHANGE_REFERENCE_MIN:
            return "reference" if change_ratio < TEXT_CHANGE_NEW_BUILD_MIN else "new_build"
        if change_ratio <= TEXT_CHANGE_DIRECT_REUSE_MAX:
            return "reuse_as_is"
        return "reference"

    if diff_action == "new_build" and change_ratio >= TEXT_CHANGE_NEW_BUILD_MIN:
        return "new_build"
    if diff_action == "reference" and change_ratio >= TEXT_CHANGE_REFERENCE_MIN:
        if base_action in ("reuse_as_is", "optimize"):
            return "reference"
    if diff_action == "optimize" and base_action == "reuse_as_is":
        if change_ratio >= TEXT_CHANGE_OPTIMIZE_MIN:
            return "optimize"
    if diff_action == "direct_reuse" and base_action == "optimize":
        if change_ratio <= TEXT_CHANGE_DIRECT_REUSE_MAX:
            return "reuse_as_is"

    return base_action


def judge_block_pair(
    old_block: dict | None,
    new_block: dict | None,
    *,
    old_text: str,
    new_text: str,
    reuse_action: str,
    change_type: str,
    teacher_note: str,
) -> dict:
    """
    在规则引擎基础动作上附加 M2 输出 schema，并按文本 diff 微调动作。
    """
    diff = compute_text_change_ratio(old_text, new_text)
    change_ratio = diff["change_ratio"]
    is_experiment = is_experiment_block(old_block or {}) or is_experiment_block(new_block or {})
    match_score, match_score_top2 = _match_scores(new_block)

    action = _refine_action_from_diff(
        reuse_action,
        change_ratio=change_ratio,
        diff_bucket=diff["bucket"],
        is_experiment=is_experiment,
        old_block=old_block,
        new_block=new_block,
    )

    combined = f"{old_text} {new_text}"
    optimize_subtype = _optimize_subtype(change_ratio, combined) if action == "optimize" else None

    confidence = _confidence(
        action=action,
        change_ratio=change_ratio,
        match_score=match_score,
        match_score_top2=match_score_top2,
        has_both_blocks=bool(old_block and new_block),
    )

    rationale = _build_rationale(
        action=action,
        change_ratio=change_ratio,
        diff_bucket=diff["bucket"],
        is_experiment=is_experiment,
        match_score=match_score,
        base_note=teacher_note,
    )
    change_points = _build_change_points(
        old_block=old_block,
        new_block=new_block,
        old_text=old_text,
        new_text=new_text,
        action=action,
        change_ratio=change_ratio,
        change_type=change_type,
    )

    return {
        "reuse_action": action,
        "change_type": change_type,
        "teacher_note": teacher_note,
        "rationale": rationale,
        "change_points": change_points,
        "confidence": confidence,
        "optimize_subtype": optimize_subtype,
        "text_change": diff,
        "matched_old_block_id": (old_block or {}).get("block_id"),
        "match_score": match_score,
        "match_score_top2": match_score_top2,
        "is_experiment_block": is_experiment,
    }
