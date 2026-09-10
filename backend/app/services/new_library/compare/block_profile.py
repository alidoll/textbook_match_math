"""区块画像：类型、是否有效、是否完整闭环（M1 前置统计）。"""
from __future__ import annotations

from .rules_config import (
    CLOSURE_ACTIVITY_KEYWORDS,
    CLOSURE_INTRO_KEYWORDS,
    CLOSURE_SUMMARY_KEYWORDS,
    EXPERIMENT_BLOCK_KINDS,
    EXPERIMENT_NAME_KEYWORDS,
    FRAGMENT_MAX_ATOMS,
    FRAGMENT_MIN_TEXT_CHARS,
)

_BLOCK_KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cover": ("课件名称", "课头封面", "封面", "名称页"),
    "intro": ("导入", "情境", "问题情境", "激趣", "引入"),
    "explain": ("知识点", "讲解", "新知", "概念", "讲授"),
    "activity": ("实验", "活动", "探究", "操作", "研究"),
    "practice": ("练习", "典例", "习题", "课堂练习"),
    "summary": ("小结", "归纳", "知识小结", "总结"),
    "end": ("尾页", "结束页", "练习提醒", "结束"),
    "cw_fixed": ("课件例题", "课件尾页"),
}


def infer_block_kind(block: dict) -> str:
    meta = block.get("metadata") or {}
    explicit = str(meta.get("block_kind") or "").strip()
    if explicit:
        return explicit
    name = (block.get("block_name") or "").strip()
    for kind, keywords in _BLOCK_KIND_KEYWORDS.items():
        if any(kw in name for kw in keywords):
            return kind
    return "other"


def is_experiment_block(block: dict | None) -> bool:
    if not block:
        return False
    meta = block.get("metadata") or {}
    if meta.get("is_experiment") is True:
        return True
    if meta.get("is_experiment") is False:
        return False
    kind = infer_block_kind(block)
    if kind in EXPERIMENT_BLOCK_KINDS:
        return True
    name = (block.get("block_name") or "").strip()
    return any(kw in name for kw in EXPERIMENT_NAME_KEYWORDS)


def _atom_text(atom: dict | None) -> str:
    if not atom:
        return ""
    return (atom.get("text") or atom.get("content") or atom.get("ocr_text") or "").strip()


def _atoms_for_block(
    block: dict,
    *,
    side: str,
    atoms_by_code: dict[str, dict],
) -> list[dict]:
    codes = block.get("atom_codes") or []
    rows: list[dict] = []
    for code in codes:
        key = f"{side}:{code}"
        atom = atoms_by_code.get(key) or atoms_by_code.get(str(code))
        if atom:
            rows.append(atom)
    return rows


def infer_closure_complete(
    block: dict,
    *,
    side: str = "new",
    atoms_by_code: dict[str, dict] | None = None,
) -> bool:
    meta = block.get("metadata") or {}
    if meta.get("closure_complete") is True:
        return True
    if meta.get("closure_complete") is False:
        return False

    name = (block.get("block_name") or "").strip()
    combined = name
    atoms = _atoms_for_block(block, side=side, atoms_by_code=atoms_by_code or {})
    for a in atoms:
        combined += " " + _atom_text(a)

    has_intro = any(kw in combined for kw in CLOSURE_INTRO_KEYWORDS)
    has_activity = any(kw in combined for kw in CLOSURE_ACTIVITY_KEYWORDS)
    has_summary = any(kw in combined for kw in CLOSURE_SUMMARY_KEYWORDS)

    if has_intro and has_activity and has_summary:
        return True
    if is_experiment_block(block) and len(atoms) >= 3:
        return True
    if len(atoms) >= 5:
        return True
    return False


def is_fragment_block(
    block: dict,
    *,
    side: str = "new",
    atoms_by_code: dict[str, dict] | None = None,
) -> bool:
    """碎片化块：单图/单句/无教学闭环，不计入有效区块。"""
    meta = block.get("metadata") or {}
    if meta.get("is_fragment") is True:
        return True
    if meta.get("is_fragment") is False:
        return False
    if infer_closure_complete(block, side=side, atoms_by_code=atoms_by_code):
        return False

    kind = infer_block_kind(block)
    if kind in ("intro", "activity", "explain", "practice", "summary", "cw_fixed"):
        return False
    if is_experiment_block(block):
        return False

    codes = block.get("atom_codes") or []
    cw_pgs = block.get("cw_pgs") or []
    if cw_pgs and not codes:
        return False

    if len(codes) > FRAGMENT_MAX_ATOMS:
        return False

    atoms = _atoms_for_block(block, side=side, atoms_by_code=atoms_by_code or {})
    if not codes and not cw_pgs:
        return True

    if len(codes) == 1:
        text = _atom_text(atoms[0] if atoms else None)
        atom_type = (atoms[0].get("atom_type") if atoms else "") or ""
        if atom_type == "image" and len(text) < FRAGMENT_MIN_TEXT_CHARS:
            return True
        if len(text) < FRAGMENT_MIN_TEXT_CHARS:
            return True

    name = (block.get("block_name") or "").strip()
    if len(codes) == 0 and not name:
        return True
    return False


def profile_block(
    block: dict,
    *,
    side: str = "new",
    atoms_by_code: dict[str, dict] | None = None,
) -> dict:
    fragment = is_fragment_block(block, side=side, atoms_by_code=atoms_by_code)
    return {
        "block_id": block.get("block_id") or block.get("block_code"),
        "block_kind": infer_block_kind(block),
        "is_experiment": is_experiment_block(block),
        "is_fragment": fragment,
        "is_effective": not fragment,
        "closure_complete": infer_closure_complete(
            block, side=side, atoms_by_code=atoms_by_code
        ),
        "atom_count": len(block.get("atom_codes") or []),
    }
