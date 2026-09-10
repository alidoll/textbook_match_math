"""课时前置统计（M1）：有效区块、最长连续可复用组、核心实验保留。"""
from __future__ import annotations

from .block_profile import is_experiment_block, profile_block
from .lesson_grade import effective_reuse_action
from .rules_config import MIN_CONTINUOUS_REUSABLE_LENGTH

_REUSABLE_FOR_CONTINUITY = frozenset({"reuse_as_is", "optimize"})
_EXPERIMENT_RETAINED = frozenset({"reuse_as_is", "optimize"})


def _sorted_new_blocks(new_blocks: list[dict]) -> list[dict]:
    return sorted(
        new_blocks,
        key=lambda b: (b.get("sort_order") or 0, b.get("block_id") or ""),
    )


def _match_by_new_id(matches: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in matches:
        nb = m.get("new_block_id")
        if nb:
            out[nb] = m
    return out


def _is_reusable_for_continuity(match: dict | None) -> bool:
    if not match:
        return False
    return effective_reuse_action(match) in _REUSABLE_FOR_CONTINUITY


def longest_continuous_reusable_run(
    *,
    new_blocks: list[dict],
    matches: list[dict],
    effective_ids: set[str] | None = None,
) -> dict:
    """按新区块顺序找最长连续「直接沿用+优化调整」组（长度≥2 才算）。"""
    match_by_new = _match_by_new_id(matches)
    ordered = _sorted_new_blocks(new_blocks)

    best_len = 0
    best_ids: list[str] = []
    cur_len = 0
    cur_ids: list[str] = []

    def flush() -> None:
        nonlocal best_len, best_ids, cur_len, cur_ids
        if cur_len > best_len:
            best_len = cur_len
            best_ids = list(cur_ids)
        cur_len = 0
        cur_ids = []

    for block in ordered:
        bid = block.get("block_id") or ""
        if effective_ids is not None and bid not in effective_ids:
            flush()
            continue
        if _is_reusable_for_continuity(match_by_new.get(bid)):
            cur_len += 1
            cur_ids.append(bid)
        else:
            flush()

    flush()

    qualified = best_len >= MIN_CONTINUOUS_REUSABLE_LENGTH
    return {
        "length": best_len if qualified else 0,
        "raw_length": best_len,
        "block_ids": best_ids if qualified else [],
        "qualified": qualified,
    }


def core_experiment_status(
    *,
    new_blocks: list[dict],
    matches: list[dict],
    profiles: list[dict],
) -> dict:
    match_by_new = _match_by_new_id(matches)
    profile_by_id = {p["block_id"]: p for p in profiles}

    experiment_blocks: list[dict] = []
    for block in new_blocks:
        bid = block.get("block_id") or ""
        prof = profile_by_id.get(bid) or {}
        if not (prof.get("is_experiment") or is_experiment_block(block)):
            continue
        if not prof.get("is_effective", True):
            continue
        m = match_by_new.get(bid)
        action = effective_reuse_action(m) if m else ""
        retained = action in _EXPERIMENT_RETAINED
        experiment_blocks.append(
            {
                "block_id": bid,
                "block_name": block.get("block_name") or "",
                "action": action or "pending",
                "retained": retained,
                "has_match": bool(m),
            }
        )

    if not experiment_blocks:
        return {
            "has_experiment": False,
            "experiment_count": 0,
            "retained": None,
            "all_retained": None,
            "blocks": [],
        }

    all_retained = all(b["retained"] for b in experiment_blocks if b["action"] != "pending")
    any_pending = any(b["action"] in ("", "pending") for b in experiment_blocks)
    return {
        "has_experiment": True,
        "experiment_count": len(experiment_blocks),
        "retained": all_retained if not any_pending else None,
        "all_retained": all_retained if not any_pending else None,
        "blocks": experiment_blocks,
    }


def build_atoms_index(float_atoms: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for a in float_atoms or []:
        code = a.get("atom_code") or a.get("atom_id")
        side = a.get("book_side") or "new"
        if code:
            index[f"{side}:{code}"] = a
            index[str(code)] = a
    return index


def build_pre_stats(
    *,
    new_blocks: list[dict],
    matches: list[dict],
    float_atoms: list[dict] | None = None,
) -> dict:
    atoms_by_code = build_atoms_index(float_atoms or [])
    profiles = [
        profile_block(b, side="new", atoms_by_code=atoms_by_code)
        for b in new_blocks
    ]
    effective = [p for p in profiles if p.get("is_effective")]
    effective_ids = {p["block_id"] for p in effective}
    fragment_count = len(profiles) - len(effective)

    continuous = longest_continuous_reusable_run(
        new_blocks=new_blocks,
        matches=matches,
        effective_ids=effective_ids,
    )
    effective_count = len(effective) or 1
    continuous_ratio = round(continuous["length"] / effective_count, 3)

    experiment = core_experiment_status(
        new_blocks=new_blocks,
        matches=matches,
        profiles=profiles,
    )

    return {
        "total_new_blocks": len(new_blocks),
        "effective_block_count": len(effective),
        "fragment_block_count": fragment_count,
        "continuous_reusable": {
            **continuous,
            "ratio": continuous_ratio,
            "min_length_required": MIN_CONTINUOUS_REUSABLE_LENGTH,
        },
        "core_experiment": experiment,
        "block_profiles": profiles,
        "summary_lines": _summary_lines(
            effective_count=len(effective),
            fragment_count=fragment_count,
            continuous=continuous,
            continuous_ratio=continuous_ratio,
            experiment=experiment,
        ),
    }


def _summary_lines(
    *,
    effective_count: int,
    fragment_count: int,
    continuous: dict,
    continuous_ratio: float,
    experiment: dict,
) -> list[str]:
    lines = [
        f"有效区块 {effective_count} 个"
        + (f"（排除碎片 {fragment_count} 个）" if fragment_count else ""),
    ]
    if continuous.get("qualified"):
        ids = continuous.get("block_ids") or []
        lines.append(
            f"最长连续可复用 {continuous['length']} 块（{', '.join(ids)}），"
            f"占有效区块 {int(continuous_ratio * 100)}%"
        )
    else:
        raw = continuous.get("raw_length") or 0
        if raw == 1:
            lines.append("连续可复用仅 1 块，不计入连续组（规则要求≥2）")
        else:
            lines.append("尚无长度≥2 的连续可复用组")

    if experiment.get("has_experiment"):
        if experiment.get("retained") is True:
            lines.append(f"核心实验 {experiment['experiment_count']} 块均已保留")
        elif experiment.get("retained") is False:
            lines.append("核心实验未完整保留，需人工确认")
        else:
            lines.append("核心实验块尚有待判定项")
    return lines
