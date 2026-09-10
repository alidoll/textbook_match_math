"""建块流水线：7 步状态机 + 双路径判断（与 analysis_pipeline 同构）。"""
from __future__ import annotations

from typing import Any

BLOCK_PIPELINE_STEPS: list[dict[str, Any]] = [
    {"key": "column_detect", "name": "认栏目头", "weight": 0.10},
    {"key": "topic_cluster", "name": "原子分组", "weight": 0.20},
    {"key": "balance", "name": "合并碎块", "weight": 0.05},
    {"key": "attributes", "name": "对照旧块建块", "weight": 0.15},
    {"key": "anchor", "name": "挂旧块锚", "weight": 0.25},
    {"key": "validate", "name": "检查锚定", "weight": 0.15},
    {"key": "edit_ready", "name": "待核对", "weight": 0.10},
]

BLOCK_SEGMENT_KEYS = tuple(s["key"] for s in BLOCK_PIPELINE_STEPS)

COVERAGE_THRESHOLD = 0.70
AVG_TOP1_THRESHOLD = 0.60
ATOM_COVERAGE_MIN = 0.50
ATOM_ASSIGNMENT_MIN = 0.45

SUSPICIOUS_REASON_LABELS = {
    "duplicate_anchor": "同一旧块被多个新区块锚定",
    "order_jump": "锚定顺序与旧课块顺序不一致",
    "non_contiguous_multi_anchor": "多锚定旧块在旧课中不连续",
    "low_gap": "匹配备选分差过小，建议人工确认",
}


def empty_block_segments() -> dict[str, str]:
    return {key: "pending" for key in BLOCK_SEGMENT_KEYS}


def compute_block_progress(step_key: str, sub_progress: float = 1.0) -> float:
    total = 0.0
    for step in BLOCK_PIPELINE_STEPS:
        if step["key"] == step_key:
            total += float(step["weight"]) * max(0.0, min(1.0, sub_progress))
            break
        total += float(step["weight"])
    return min(1.0, total)


def steps_done_up_to(step_key: str) -> list[str]:
    keys: list[str] = []
    for step in BLOCK_PIPELINE_STEPS:
        keys.append(step["key"])
        if step["key"] == step_key:
            break
    return keys


def derive_match_type(score: float | None) -> str:
    s = float(score or 0)
    if s >= 0.8:
        return "full"
    if s >= 0.5:
        return "partial"
    return "new"


def apply_block_pipeline_metadata(
    meta: dict | None,
    *,
    source_path: str,
    ai_step: str,
    match_score: float | None = None,
    match_score_top2: float | None = None,
    suspicious: bool = False,
    suspicious_reason: str = "",
    stage_ref: str = "",
) -> dict:
    existing = dict(meta or {})
    existing["block_pipeline"] = {
        "source_path": source_path,
        "ai_step": ai_step,
        "steps_done": steps_done_up_to(ai_step),
    }
    score = float(match_score or 0)
    existing["match_type"] = derive_match_type(score)
    existing["match_score"] = round(score, 4) if match_score is not None else None
    if match_score_top2 is not None:
        existing["match_score_top2"] = round(float(match_score_top2), 4)
    existing["suspicious"] = bool(suspicious)
    existing["suspicious_reason"] = suspicious_reason or ""
    existing["category"] = (stage_ref or existing.get("category") or "").strip()
    existing.setdefault("tags", [])
    existing.setdefault("teaching_phase", "")
    return existing


def build_blocking_summary(
    blocks: list[dict],
    suspicious_items: list[dict],
    *,
    source_path: str = "",
    duration_seconds: float | None = None,
    path_reason: str = "",
) -> dict:
    summary: dict[str, Any] = {
        "total_blocks": len(blocks),
        "match_distribution": {"full": 0, "partial": 0, "new": 0},
        "suspicious_count": len(suspicious_items),
        "suspicious_blocks": [],
        "source_path": source_path,
        "path_reason": path_reason,
    }
    if duration_seconds is not None:
        summary["duration_seconds"] = round(duration_seconds, 1)

    by_code = {b.get("block_code"): b for b in blocks if b.get("block_code")}

    for b in blocks:
        meta = b.get("metadata_json") or {}
        mtype = meta.get("match_type") or derive_match_type(meta.get("match_score"))
        if mtype not in summary["match_distribution"]:
            mtype = "new"
        summary["match_distribution"][mtype] += 1

    for item in suspicious_items:
        code = item.get("block_code") or item.get("block_id")
        block = by_code.get(code, {})
        meta = block.get("metadata_json") or {}
        reason_key = item.get("reason") or meta.get("suspicious_reason") or ""
        summary["suspicious_blocks"].append(
            {
                "block_code": code,
                "block_name": block.get("block_name") or "",
                "reason": reason_key,
                "reason_label": SUSPICIOUS_REASON_LABELS.get(reason_key, reason_key),
                "severity": item.get("severity", "medium"),
            }
        )

    return summary


def should_use_old_mirror_path(*, new_lesson_id: str) -> dict[str, Any]:
    """判断走 old_mirror 还是 new_cluster。"""
    from .annotate.pair_review import (
        allows_in_lesson_auto_pairing,
        get_primary_lesson_match,
        pair_review_status,
    )
    from .annotate.seed_from_old_page import compute_old_mirror_preflight_metrics

    result: dict[str, Any] = {
        "path": "new_cluster",
        "reason": "",
        "checks": {
            "pair_confirmed": False,
            "old_blocks_exist": False,
            "coverage_rate": 0.0,
            "avg_top1_score": 0.0,
            "atom_assignment_rate": 0.0,
        },
    }

    match = get_primary_lesson_match(new_lesson_id)
    status = pair_review_status(match)
    pair_confirmed = allows_in_lesson_auto_pairing(status)
    result["checks"]["pair_confirmed"] = pair_confirmed
    if not pair_confirmed:
        result["reason"] = "pair_review 未确认（需 confirmed 才走旧块镜像）"
        return result

    if not match or not match.old_lesson_id:
        result["reason"] = "无粗分主参照旧课"
        return result

    metrics = compute_old_mirror_preflight_metrics(
        new_lesson_id=new_lesson_id,
        old_lesson_id=match.old_lesson_id,
    )
    result["checks"]["old_blocks_exist"] = metrics["old_blocks_exist"]
    result["checks"]["coverage_rate"] = metrics["coverage_rate"]
    result["checks"]["avg_top1_score"] = metrics["avg_top1_score"]
    result["checks"]["atom_assignment_rate"] = metrics.get("atom_assignment_rate", 0.0)

    if not metrics["old_blocks_exist"]:
        result["reason"] = metrics.get("detail") or "旧侧无区块或新侧无 OCR 原子"
        return result

    if metrics["coverage_rate"] < COVERAGE_THRESHOLD:
        pct = int(metrics["coverage_rate"] * 100)
        result["reason"] = f"旧块覆盖率 {pct}%，低于 {int(COVERAGE_THRESHOLD * 100)}% 阈值"
        return result

    if metrics["avg_top1_score"] < AVG_TOP1_THRESHOLD:
        pct = int(metrics["avg_top1_score"] * 100)
        result["reason"] = f"平均 Top1 匹配度 {pct}%，低于 {int(AVG_TOP1_THRESHOLD * 100)}% 阈值"
        return result

    atom_rate = float(metrics.get("atom_assignment_rate") or 0.0)
    if atom_rate < ATOM_ASSIGNMENT_MIN:
        pct = int(atom_rate * 100)
        result["reason"] = (
            f"预估原子可分配率 {pct}%，低于 {int(ATOM_ASSIGNMENT_MIN * 100)}% 阈值"
        )
        return result

    result["path"] = "old_mirror"
    result["reason"] = "全部条件通过，使用旧块镜像路径（①–③ 栏目分析 + ④⑤ 豆包对照）"
    return result
