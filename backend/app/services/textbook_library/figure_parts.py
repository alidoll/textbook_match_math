# -*- coding: utf-8 -*-
"""教材库：整图（父）与图内散件（子）几何绑定。"""
from __future__ import annotations

import math
import re
from typing import Any

from ..llm.atom_text_role_classify import (
    BINDABLE_TEXT_ROLES,
    atoms_have_text_roles,
    classify_atom_text_roles,
)

_PAGE_CHROME_RE = re.compile(
    r"^\(\s*第\s*\d+\s*题\s*\)$|^\d{1,3}$"
)
_PART_MAX_CHARS = 24
_BINDING_KEYS = (
    "figure_role",
    "figure_kind",
    "part_atom_codes",
    "parent_figure_code",
    "part_kind",
    "page_chrome",
    "text_role",
)


def _atom_id(atom: dict[str, Any]) -> str:
    return str(atom.get("atom_id") or atom.get("atom_code") or "").strip()


def _bbox_center(atom: dict[str, Any]) -> tuple[float, float]:
    xs = float(atom.get("x_start", 0))
    ys = float(atom.get("y_start", 0))
    xe = float(atom.get("x_end", 1))
    ye = float(atom.get("y_end", 1))
    return (xs + xe) / 2.0, (ys + ye) / 2.0


def _bbox_area(atom: dict[str, Any]) -> float:
    xs = float(atom.get("x_start", 0))
    ys = float(atom.get("y_start", 0))
    xe = float(atom.get("x_end", 1))
    ye = float(atom.get("y_end", 1))
    return max(0.0, xe - xs) * max(0.0, ye - ys)


def _expand_bbox(atom: dict[str, Any], margin: float) -> dict[str, float]:
    xs = float(atom.get("x_start", 0))
    ys = float(atom.get("y_start", 0))
    xe = float(atom.get("x_end", 1))
    ye = float(atom.get("y_end", 1))
    m = max(0.0, float(margin))
    return {
        "x_start": max(0.0, xs - m),
        "y_start": max(0.0, ys - m),
        "x_end": min(1.0, xe + m),
        "y_end": min(1.0, ye + m),
    }


def _center_in_bbox(cx: float, cy: float, box: dict[str, float]) -> bool:
    return (
        box["x_start"] <= cx <= box["x_end"]
        and box["y_start"] <= cy <= box["y_end"]
    )


def _center_distance(
    cx: float, cy: float, atom: dict[str, Any]
) -> float:
    px, py = _bbox_center(atom)
    return math.hypot(cx - px, cy - py)


def _is_page_chrome(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_PAGE_CHROME_RE.match(t))


def _is_parent_figure(atom: dict[str, Any]) -> bool:
    return str(atom.get("atom_type") or "").strip() in ("image", "figure")


def _plain_text(atom: dict[str, Any]) -> str:
    for key in ("content", "ocr_text", "display_text", "text"):
        s = str(atom.get(key) or "").strip()
        if s:
            return s
    return ""


def _is_text_like(atom: dict[str, Any]) -> bool:
    return str(atom.get("atom_type") or "").strip() in ("text", "title")


def _is_candidate_part(atom: dict[str, Any]) -> bool:
    if not _is_text_like(atom):
        return False
    text = _plain_text(atom)
    if not text or _is_page_chrome(text):
        return False
    return len(text) <= _PART_MAX_CHARS


def _bind_as_part(
    atom: dict[str, Any],
    parent: dict[str, Any],
    parent_parts: dict[str, list[str]],
    *,
    part_kind: str,
) -> None:
    aid = _atom_id(atom)
    pid = _atom_id(parent)
    if not aid or not pid:
        return
    atom["figure_role"] = "part"
    atom["parent_figure_code"] = pid
    atom["part_kind"] = part_kind
    parent_parts.setdefault(pid, [])
    if aid not in parent_parts[pid]:
        parent_parts[pid].append(aid)


def _bbox_dict(atom: dict[str, Any]) -> dict[str, float]:
    return {
        "x_start": float(atom.get("x_start", 0)),
        "y_start": float(atom.get("y_start", 0)),
        "x_end": float(atom.get("x_end", 1)),
        "y_end": float(atom.get("y_end", 1)),
    }


def _intersection_area(a: dict[str, float], b: dict[str, float]) -> float:
    x0 = max(a["x_start"], b["x_start"])
    y0 = max(a["y_start"], b["y_start"])
    x1 = min(a["x_end"], b["x_end"])
    y1 = min(a["y_end"], b["y_end"])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def _part_overlap_in_parent(part: dict[str, Any], parent: dict[str, Any]) -> float:
    """散件 bbox 有多大比例落在父图内（过程图长 text 比中心点判定更稳）。"""
    part_box = _bbox_dict(part)
    part_area = _bbox_area(part)
    if part_area <= 0:
        return 0.0
    parent_box = _expand_bbox(parent, 0.02)
    return _intersection_area(part_box, parent_box) / part_area


def _pick_parent_for_part(
    part: dict[str, Any],
    parents: list[dict[str, Any]],
    *,
    margin: float,
) -> dict[str, Any] | None:
    if not parents:
        return None
    cx, cy = _bbox_center(part)
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for parent in parents:
        box = _expand_bbox(parent, margin)
        if _center_in_bbox(cx, cy, box):
            candidates.append((_bbox_area(parent), _center_distance(cx, cy, parent), parent))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    overlap_candidates: list[tuple[float, float, dict[str, Any]]] = []
    for parent in parents:
        overlap = _part_overlap_in_parent(part, parent)
        if overlap >= 0.45:
            overlap_candidates.append(
                (-overlap, _bbox_area(parent), parent)
            )
    if overlap_candidates:
        overlap_candidates.sort()
        return overlap_candidates[0][2]
    return None


def _clear_figure_binding_fields(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(a) for a in atoms]
    for atom in out:
        for key in _BINDING_KEYS:
            atom.pop(key, None)
    return out


def _mark_page_chrome_only(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(a) for a in atoms]
    for atom in out:
        text = _plain_text(atom)
        if _is_page_chrome(text):
            atom["page_chrome"] = True
            atom["figure_role"] = "page_chrome"
    return out


def _text_role(atom: dict[str, Any]) -> str:
    return str(atom.get("text_role") or "").strip()


def _needs_figure_part_binding_heuristic(
    atoms: list[dict[str, Any]],
    *,
    margin: float = 0.015,
) -> bool:
    """无 text_role 时的几何兜底。"""
    parents = [a for a in atoms if _is_parent_figure(a)]
    if not parents:
        return False
    for atom in atoms:
        if not _is_text_like(atom):
            continue
        text = _plain_text(atom)
        if not text or _is_page_chrome(text):
            continue
        parent = _pick_parent_for_part(atom, parents, margin=margin)
        if not parent:
            continue
        if len(text) <= _PART_MAX_CHARS:
            return True
        if _part_overlap_in_parent(atom, parent) >= 0.45:
            return True
    return False


def needs_figure_part_binding(
    atoms: list[dict[str, Any]],
    *,
    margin: float = 0.015,
) -> bool:
    """页内是否需要父子绑定。"""
    if not any(_is_parent_figure(a) for a in atoms):
        return False
    if atoms_have_text_roles(atoms):
        return any(
            _text_role(a) in BINDABLE_TEXT_ROLES
            for a in atoms
            if _is_text_like(a)
        )
    return _needs_figure_part_binding_heuristic(atoms, margin=margin)


def _is_bindable_text_atom(
    atom: dict[str, Any],
    parents: list[dict[str, Any]],
    *,
    margin: float,
) -> bool:
    role = _text_role(atom)
    if role:
        if role in ("page_chrome",):
            return False
        return role in BINDABLE_TEXT_ROLES
    text = _plain_text(atom)
    if not text or _is_page_chrome(text):
        return False
    parent = _pick_parent_for_part(atom, parents, margin=margin)
    if not parent:
        return False
    if len(text) <= _PART_MAX_CHARS:
        return True
    return _part_overlap_in_parent(atom, parent) >= 0.45


def _part_kind_for_atom(atom: dict[str, Any]) -> str:
    role = _text_role(atom)
    if role == "figure_interior_body":
        return "interior"
    if role == "figure_interior_label":
        return "other"
    text = _plain_text(atom)
    if len(text) > _PART_MAX_CHARS:
        return "interior"
    return "other"


def prepare_figure_parts_for_page(
    atoms: list[dict[str, Any]],
    *,
    volume: Any | None = None,
    pdf_page: int | None = None,
    subject: str = "",
    lesson_name: str = "",
    page_image_bytes: bytes | None = None,
    skip_classify: bool = False,
) -> list[dict[str, Any]]:
    """语义 text_role 标注 + 几何绑定（图片 OCR 后调用）。"""
    working = list(atoms or [])
    if not skip_classify and working:
        if page_image_bytes is None and volume is not None and pdf_page:
            from .atom_crops import volume_page_image_bytes

            page_image_bytes = volume_page_image_bytes(volume, int(pdf_page))
        if not atoms_have_text_roles(working):
            working = classify_atom_text_roles(
                working,
                subject=subject or getattr(volume, "subject", None) or "",
                lesson_name=lesson_name,
                page_image_bytes=page_image_bytes,
            )
    return bind_figure_parts(working)


def bind_figure_parts(
    atoms: list[dict[str, Any]],
    *,
    margin: float = 0.015,
) -> list[dict[str, Any]]:
    """将 figure_interior_* text 绑定到父 illustration。"""
    if not needs_figure_part_binding(atoms, margin=margin):
        return _mark_page_chrome_only(_clear_figure_binding_fields(atoms))

    parents = [a for a in atoms if _is_parent_figure(a)]
    out = [dict(a) for a in atoms]
    parent_parts: dict[str, list[str]] = {_atom_id(p): [] for p in parents if _atom_id(p)}

    for atom in out:
        text = _plain_text(atom)
        if _is_page_chrome(text) or _text_role(atom) == "page_chrome":
            atom["page_chrome"] = True
            atom["figure_role"] = "page_chrome"
            atom["text_role"] = atom.get("text_role") or "page_chrome"
            continue
        if not _is_text_like(atom) or not text:
            continue
        if not _is_bindable_text_atom(atom, parents, margin=margin):
            continue
        parent = _pick_parent_for_part(atom, parents, margin=margin)
        if parent:
            _bind_as_part(
                atom,
                parent,
                parent_parts,
                part_kind=_part_kind_for_atom(atom),
            )

    for atom in out:
        if not _is_parent_figure(atom):
            continue
        pid = _atom_id(atom)
        if not pid:
            continue
        atom["figure_role"] = "parent"
        atom["figure_kind"] = atom.get("figure_kind") or "other"
        atom["part_atom_codes"] = list(parent_parts.get(pid, []))

    return out
