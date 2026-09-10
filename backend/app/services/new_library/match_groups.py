"""有序多锚定共用逻辑（建块锚定 + 新旧对比）。"""
from __future__ import annotations

from typing import Any


def block_sort_order(block: Any) -> int:
    if isinstance(block, dict):
        return int(block.get("sort_order") or 0)
    return int(getattr(block, "sort_order", None) or 0)


def block_code(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("block_id") or block.get("block_code") or "").strip()
    return str(getattr(block, "block_code", "") or "").strip()


def old_codes_contiguous(
    codes: list[str],
    *,
    blocks_by_code: dict[str, Any],
) -> bool:
    """旧块 code 在 sort_order 上连续（允许步长 1）。"""
    if len(codes) <= 1:
        return True
    indices: list[int] = []
    for code in codes:
        ob = blocks_by_code.get(code)
        if not ob:
            return False
        indices.append(block_sort_order(ob))
    indices.sort()
    for i in range(1, len(indices)):
        if indices[i] - indices[i - 1] > 1:
            return False
    return True


def group_matches_by_new_block(
    matches: list[dict],
) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for m in matches:
        nb = str(m.get("new_block_id") or "").strip()
        if not nb:
            continue
        groups.setdefault(nb, []).append(m)
    return groups


def ordered_old_codes_for_group(
    group: list[dict],
    *,
    old_blocks_by_code: dict[str, Any],
) -> list[str]:
    codes = [
        str(m.get("old_block_id") or "").strip()
        for m in group
        if m.get("old_block_id")
    ]
    return sorted(set(codes), key=lambda c: block_sort_order(old_blocks_by_code.get(c, {})))


def anchor_ref_with_role(ref: dict[str, Any], *, role: str) -> dict[str, Any]:
    out = dict(ref)
    out["match_role"] = role
    return out
