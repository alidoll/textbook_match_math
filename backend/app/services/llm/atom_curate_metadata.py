"""AI 整理原子：结构化备注（对话组、插图分类、板块层级）。"""
from __future__ import annotations

from typing import Any

from ...models import TextbookAtom

DIALOGUE_ROLES = frozenset({"dialogue_bubble", "dialogue_fragment"})
ILLUSTRATION_ROLES = frozenset({"illustration", "scene_label", "scene_inset"})

CURATE_META_KEYS = (
    "role",
    "bar_id",
    "bubble_id",
    "ill_id",
    "table_id",
    "context_id",
    "record_id",
    "dialogue_group_id",
    "dialogue_side",
    "group_slot",
    "image_category",
    "section_parent",
    "section_module",
    "group_label",
)


def build_atom_metadata_from_role(raw: dict[str, Any]) -> dict[str, Any] | None:
    curate: dict[str, Any] = {}
    role = str(raw.get("role") or "").strip()
    if role:
        curate["role"] = role
    for key in CURATE_META_KEYS:
        if key == "role":
            continue
        val = raw.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            if key == "group_slot" and int(val) <= 0:
                continue
            curate[key] = int(val) if key == "group_slot" else val
            continue
        s = str(val).strip()
        if s:
            curate[key] = s
    if not curate:
        return None
    return {"curate": curate}


def combine_atom_metadata(*metas: dict[str, Any] | None) -> dict[str, Any] | None:
    curate: dict[str, Any] = {}
    for meta in metas:
        if not meta:
            continue
        block = meta.get("curate") if isinstance(meta, dict) else None
        if not isinstance(block, dict):
            continue
        for key, val in block.items():
            if val is None or val == "":
                continue
            if key not in curate:
                curate[key] = val
    return {"curate": curate} if curate else None


def metadata_map_from_roles(atom_roles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in atom_roles:
        code = str(raw.get("atom_code") or "").strip()
        if not code:
            continue
        meta = build_atom_metadata_from_role(raw)
        if meta:
            out[code] = meta
    return out


def remap_metadata_map(
    meta_by_code: dict[str, dict[str, Any]],
    mapping: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """合并编号映射后的 metadata（如 save_page_atoms 重编号）。"""
    out: dict[str, dict[str, Any]] = {}
    for old_code, meta in meta_by_code.items():
        new_code = mapping.get(old_code, old_code)
        out[new_code] = combine_atom_metadata(out.get(new_code), meta) or meta
    return out


def record_merge_metadata(
    meta_by_code: dict[str, dict[str, Any]],
    merged_from: list[str],
    merged_code: str,
) -> None:
    metas = [meta_by_code.get(c) for c in merged_from if c in meta_by_code]
    combined = combine_atom_metadata(*metas)
    for c in merged_from:
        meta_by_code.pop(c, None)
    if combined:
        meta_by_code[merged_code] = combine_atom_metadata(
            meta_by_code.get(merged_code), combined
        ) or combined


def enrich_curate_atom_roles(
    atom_roles: list[dict[str, Any]],
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """规则补全：对话左右配对、插图/板块缺省字段。"""
    if not atom_roles:
        return atom_roles

    by_code = {a["atom_code"]: a for a in atoms}
    out = [dict(r) for r in atom_roles]
    role_index = {str(r.get("atom_code") or "").strip(): r for r in out}

    dialogue_rows: list[dict[str, Any]] = []
    for raw in out:
        code = str(raw.get("atom_code") or "").strip()
        role = str(raw.get("role") or "").strip()
        if role in DIALOGUE_ROLES and code in by_code:
            dialogue_rows.append(raw)

    if dialogue_rows:
        groups: dict[str, list[dict[str, Any]]] = {}
        ungrouped: list[dict[str, Any]] = []
        for raw in dialogue_rows:
            gid = str(raw.get("dialogue_group_id") or "").strip()
            if gid:
                groups.setdefault(gid, []).append(raw)
            else:
                ungrouped.append(raw)

        pair_id = 0
        used: set[str] = set()
        for raw in sorted(
            ungrouped,
            key=lambda r: (
                float((by_code.get(r["atom_code"], {}).get("bbox") or {}).get("y_start", 0)),
                float((by_code.get(r["atom_code"], {}).get("bbox") or {}).get("x_start", 0)),
            ),
        ):
            code = raw["atom_code"]
            if code in used:
                continue
            bbox = by_code[code].get("bbox") or {}
            ys, ye = float(bbox.get("y_start", 0)), float(bbox.get("y_end", 1))
            mates = []
            for other in ungrouped:
                oc = other["atom_code"]
                if oc == code or oc in used:
                    continue
                ob = by_code[oc].get("bbox") or {}
                oys, oye = float(ob.get("y_start", 0)), float(ob.get("y_end", 1))
                y_overlap = min(ye, oye) - max(ys, oys)
                if y_overlap >= 0.04:
                    mates.append(other)
            if len(mates) == 1:
                pair_id += 1
                gid = f"dlg_auto_{pair_id}"
                for i, row in enumerate(sorted([raw] + mates, key=lambda r: float(
                    (by_code.get(r["atom_code"], {}).get("bbox") or {}).get("x_start", 0)
                ))):
                    row["dialogue_group_id"] = gid
                    row["group_slot"] = i + 1
                    row["dialogue_side"] = "left" if i == 0 else "right"
                    used.add(row["atom_code"])
            elif code not in used:
                used.add(code)

        for gid, rows in groups.items():
            rows.sort(
                key=lambda r: float(
                    (by_code.get(r["atom_code"], {}).get("bbox") or {}).get("x_start", 0)
                )
            )
            for i, row in enumerate(rows):
                if not str(row.get("group_slot") or "").strip():
                    row["group_slot"] = i + 1
                if not str(row.get("dialogue_side") or "").strip():
                    row["dialogue_side"] = "left" if i == 0 else "right"

    for raw in out:
        code = str(raw.get("atom_code") or "").strip()
        role = str(raw.get("role") or "").strip()
        atom = by_code.get(code) or {}
        bbox = atom.get("bbox") or {}
        xs = float(bbox.get("x_start", 0))
        if role in DIALOGUE_ROLES and not str(raw.get("dialogue_side") or "").strip():
            raw["dialogue_side"] = "left" if xs < 0.45 else "right"

        if role in ILLUSTRATION_ROLES and not str(raw.get("image_category") or "").strip():
            text = (atom.get("content") or atom.get("ocr_text") or "").lower()
            if any(k in text for k in ("工序", "步骤", "流程")):
                raw["image_category"] = "工序图"
            elif any(k in text for k in ("作品", "完成")):
                raw["image_category"] = "作品展示照"

    return out


def apply_metadata_to_page_atoms(
    *,
    lesson_id: str,
    page_index: int,
    meta_by_code: dict[str, dict[str, Any]],
) -> int:
    if not meta_by_code:
        return 0
    from ...extensions import db

    updated = 0
    rows = TextbookAtom.query.filter_by(
        lesson_id=lesson_id, page_index=page_index
    ).all()
    for atom in rows:
        meta = meta_by_code.get(atom.atom_code)
        if not meta:
            continue
        atom.metadata_json = meta
        updated += 1
    if updated:
        db.session.commit()
    return updated
