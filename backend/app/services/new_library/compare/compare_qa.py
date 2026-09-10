"""对比阶段质检：有序多锚定、顺序、匹配置信度。"""
from __future__ import annotations

from ..match_groups import (
    group_matches_by_new_block,
    old_codes_contiguous,
    ordered_old_codes_for_group,
)
from .rules_config import MATCH_SCORE_GAP_LOW_CONFIDENCE


def _block_sort_index(blocks: list[dict], block_id: str) -> int:
    order = {
        b.get("block_id") or "": i
        for i, b in enumerate(
            sorted(blocks, key=lambda x: (x.get("sort_order") or 0, x.get("block_id") or ""))
        )
    }
    return order.get(block_id, 9999)


def _match_score_from_new_block(block: dict | None) -> tuple[float | None, float | None]:
    if not block:
        return None, None
    meta = block.get("metadata") or {}
    top1 = meta.get("match_score")
    top2 = meta.get("match_score_top2")
    try:
        s1 = float(top1) if top1 is not None else None
    except (TypeError, ValueError):
        s1 = None
    try:
        s2 = float(top2) if top2 is not None else None
    except (TypeError, ValueError):
        s2 = None
    return s1, s2


def run_compare_qa(
    *,
    matches: list[dict],
    new_blocks: list[dict],
    old_blocks: list[dict],
) -> list[dict]:
    flags: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(
        *,
        flag_type: str,
        severity: str,
        target: str,
        message: str,
        match_id: str | None = None,
    ) -> None:
        key = (flag_type, target, message)
        if key in seen:
            return
        seen.add(key)
        flags.append(
            {
                "type": flag_type,
                "severity": severity,
                "target": target,
                "message": message,
                "match_id": match_id,
            }
        )

    new_by_id = {b.get("block_id"): b for b in new_blocks}
    old_by_id = {b.get("block_id"): b for b in old_blocks}

    # 旧块被多个「不同」新区块认领 → 异常
    old_to_new: dict[str, set[str]] = {}
    for m in matches:
        ob = str(m.get("old_block_id") or "").strip()
        nb = str(m.get("new_block_id") or "").strip()
        if ob and nb:
            old_to_new.setdefault(ob, set()).add(nb)

    for ob, new_ids in old_to_new.items():
        if len(new_ids) > 1:
            add(
                flag_type="duplicate_anchor",
                severity="high",
                target=ob,
                message=f"旧区块 {ob} 被多个新区块配对（{', '.join(sorted(new_ids))}）",
            )

    groups = group_matches_by_new_block(
        [m for m in matches if m.get("old_block_id") and m.get("new_block_id")]
    )

    for nb, group in groups.items():
        old_codes = ordered_old_codes_for_group(group, old_blocks_by_code=old_by_id)
        if len(old_codes) > 1:
            if old_codes_contiguous(old_codes, blocks_by_code=old_by_id):
                continue
            add(
                flag_type="non_contiguous_multi_anchor",
                severity="high",
                target=nb,
                message=(
                    f"新区块 {nb} 匹配多个旧区块（{', '.join(old_codes)}），"
                    "旧课顺序不连续，请核对"
                ),
                match_id=group[0].get("match_id"),
            )
        elif len(group) > 1:
            # 同一新块多条配对但 old_id 重复（数据异常）
            add(
                flag_type="duplicate_pair",
                severity="medium",
                target=nb,
                message=f"新区块 {nb} 存在重复配对行",
                match_id=group[0].get("match_id"),
            )

    for m in matches:
        mid = m.get("match_id")
        nb = m.get("new_block_id") or ""
        nb_block = new_by_id.get(nb)
        top1, top2 = _match_score_from_new_block(nb_block)
        if top1 is not None and top2 is not None and (top1 - top2) < MATCH_SCORE_GAP_LOW_CONFIDENCE:
            add(
                flag_type="low_confidence",
                severity="medium",
                target=nb or m.get("old_block_id") or "",
                message=(
                    f"匹配度分差 {(top1 - top2) * 100:.1f}% < "
                    f"{MATCH_SCORE_GAP_LOW_CONFIDENCE * 100:.0f}%，建议优先人工审核"
                ),
                match_id=mid,
            )

    # 跨新区块：按组最大旧序号单调递增
    sorted_groups = sorted(
        groups.items(),
        key=lambda item: _block_sort_index(new_blocks, item[0]),
    )
    last_old_idx = -1
    for nb, group in sorted_groups:
        old_codes = ordered_old_codes_for_group(group, old_blocks_by_code=old_by_id)
        indices = [_block_sort_index(old_blocks, c) for c in old_codes]
        indices = [i for i in indices if i >= 0]
        if not indices:
            continue
        group_min = min(indices)
        if group_min < last_old_idx:
            add(
                flag_type="order_inversion",
                severity="high",
                target=nb,
                message=f"新区块 {nb} 锚定旧块顺序疑似倒退",
                match_id=group[0].get("match_id"),
            )
        last_old_idx = max(last_old_idx, max(indices))

    return flags
