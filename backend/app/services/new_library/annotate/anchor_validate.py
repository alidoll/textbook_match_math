"""建块锚定质检：有序多锚定 / 顺序 / 分差（P0 规则）。"""
from __future__ import annotations

from ....models import Block

from .anchor_multi import old_refs_are_contiguous


def _block_old_refs(block: Block) -> list[dict]:
    meta = block.metadata_json or {}
    refs = meta.get("anchor_old_refs") or []
    if not isinstance(refs, list):
        return []
    return [r for r in refs if isinstance(r, dict) and r.get("old_block_code")]


def _primary_old_ref(block: Block) -> tuple[str, float, float | None]:
    meta = block.metadata_json or {}
    refs = _block_old_refs(block)
    if not refs:
        return "", float(meta.get("match_score") or 0), meta.get("match_score_top2")
    ref = refs[0]
    code = str(ref.get("old_block_code") or "").strip()
    score = float(meta.get("match_score") or ref.get("match_score") or 0)
    top2 = meta.get("match_score_top2")
    return code, score, float(top2) if top2 is not None else None


def _old_block_sort_index(old_blocks: list[Block], old_block_code: str) -> int:
    order = {b.block_code: i for i, b in enumerate(old_blocks)}
    return order.get(old_block_code, 9999)


def run_anchor_validation(
    new_blocks: list[Block],
    *,
    old_blocks: list[Block] | None = None,
) -> list[dict]:
    """返回存疑项：block_code, reason, severity。"""
    suspicious: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(code: str, reason: str, severity: str) -> None:
        key = (code, reason)
        if not code or key in seen:
            return
        seen.add(key)
        suspicious.append(
            {"block_code": code, "reason": reason, "severity": severity},
        )

    anchored = [b for b in new_blocks if _block_old_refs(b)]
    if not anchored:
        return suspicious

    old_blocks_by_code = {b.block_code: b for b in (old_blocks or [])}

    # 同一旧块被多个新区块锚定（含主/辅）→ duplicate
    old_code_to_blocks: dict[str, list[str]] = {}
    for block in anchored:
        for ref in _block_old_refs(block):
            code = str(ref.get("old_block_code") or "").strip()
            if not code:
                continue
            old_code_to_blocks.setdefault(code, []).append(block.block_code)

    for block in anchored:
        for ref in _block_old_refs(block):
            code = str(ref.get("old_block_code") or "").strip()
            claimants = old_code_to_blocks.get(code) or []
            if len(set(claimants)) > 1:
                add(block.block_code, "duplicate_anchor", "medium")
                break

    if old_blocks:
        old_order = sorted(old_blocks, key=lambda b: (b.sort_order or 0, b.block_code))
        old_index = {b.block_code: i for i, b in enumerate(old_order)}
        last_idx = -1
        for block in sorted(anchored, key=lambda b: (b.sort_order or 0, b.block_code)):
            refs = _block_old_refs(block)
            indices = [
                old_index.get(str(r.get("old_block_code") or "").strip(), -1)
                for r in refs
            ]
            indices = [i for i in indices if i >= 0]
            if not indices:
                continue
            block_min = min(indices)
            if block_min < last_idx:
                add(block.block_code, "order_jump", "high")
            last_idx = max(last_idx, max(indices))

    for block in anchored:
        refs = _block_old_refs(block)
        if len(refs) > 1 and old_blocks_by_code:
            if not old_refs_are_contiguous(
                refs, old_blocks_by_code=old_blocks_by_code
            ):
                add(block.block_code, "non_contiguous_multi_anchor", "high")

    for block in anchored:
        _, top1, top2 = _primary_old_ref(block)
        if top2 is not None and top1 - top2 < 0.10:
            add(block.block_code, "low_gap", "medium")

    return suspicious


def mark_suspicious_blocks(
    blocks: list[Block],
    suspicious_items: list[dict],
) -> None:
    by_code = {item["block_code"]: item for item in suspicious_items if item.get("block_code")}
    for block in blocks:
        item = by_code.get(block.block_code)
        if not item:
            continue
        meta = dict(block.metadata_json or {})
        meta["suspicious"] = True
        meta["suspicious_reason"] = item.get("reason") or ""
        block.metadata_json = meta
