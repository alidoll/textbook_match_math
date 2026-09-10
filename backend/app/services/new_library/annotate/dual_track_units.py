"""双轨分层：新教材单元 ID（全课主键）枚举。"""
from __future__ import annotations

from typing import Any

from ....models import LessonPage, TextbookAtom
from ..block_pipeline_prepare import ensure_page_prepare, get_page_prepare
from .seed_from_old_page import _atom_plain_text, _bbox_ymid, _is_image_like_atom


def list_lesson_new_units(
    ctx: dict[str, Any],
    *,
    ensure_prepare: bool = True,
) -> list[dict[str, Any]]:
    """
    以 block_pipeline_prepare 的 cluster_id 为 unit_id（如 P1-C01）。
    ensure_prepare=False 时只读缓存，避免双轨测试页点击触发 LLM 建块分析。
    """
    new_les = ctx["new_les"]
    new_atoms = ctx["new_atoms"]
    new_atoms_by_code = ctx["new_atoms_by_code"]
    lesson_uid = new_les.lesson_uid

    pages = (
        LessonPage.query.filter_by(lesson_id=new_les.id)
        .order_by(LessonPage.page_index)
        .all()
    )

    units: list[dict[str, Any]] = []
    claimed: set[str] = set()

    for page in pages:
        pi = int(page.page_index)
        if ensure_prepare:
            ensure_page_prepare(
                lesson_uid=lesson_uid,
                lesson_id=new_les.id,
                page_index=pi,
                lesson_name=new_les.lesson_name or "",
            )
        prep = get_page_prepare(lesson_uid, pi) or {}
        clusters = prep.get("clusters") or []
        if not clusters and not ensure_prepare:
            page_atoms = [a for a in new_atoms if int(a.page_index) == pi]
            if page_atoms:
                clusters = [
                    {
                        "cluster_id": f"P{pi}-C01",
                        "section_name": "本页（prepare 未缓存）",
                        "atom_codes": [a.atom_code for a in page_atoms],
                        "y_mid": 0.5,
                    }
                ]
        for cl in clusters:
            uid = str(cl.get("cluster_id") or "").strip()
            if not uid or uid in claimed:
                continue
            codes = [str(c).strip() for c in (cl.get("atom_codes") or []) if c]
            claimed.update(codes)
            units.append(
                {
                    "unit_id": uid,
                    "section_name": str(cl.get("section_name") or "正文区"),
                    "page_index": pi,
                    "atom_codes": codes,
                    "y_mid": float(cl.get("y_mid") or 0.5),
                }
            )

    for atom in new_atoms:
        if atom.atom_code in claimed:
            continue
        pi = int(atom.page_index)
        uid = f"P{pi}-X{len(units) + 1:02d}"
        units.append(
            {
                "unit_id": uid,
                "section_name": "未聚类原子",
                "page_index": pi,
                "atom_codes": [atom.atom_code],
                "y_mid": _bbox_ymid(atom.bbox_json),
            }
        )
        claimed.add(atom.atom_code)

    units.sort(key=lambda u: (int(u["page_index"]), float(u.get("y_mid") or 0)))
    return units


def unit_query_text(
    unit: dict[str, Any],
    atoms_by_code: dict[str, TextbookAtom],
) -> str:
    parts = [str(unit.get("section_name") or "")]
    for code in unit.get("atom_codes") or []:
        atom = atoms_by_code.get(str(code).strip())
        if not atom:
            continue
        text = _atom_plain_text(atom)
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def unit_image_atom_codes(
    unit: dict[str, Any],
    atoms_by_code: dict[str, TextbookAtom],
) -> list[str]:
    out: list[str] = []
    for code in unit.get("atom_codes") or []:
        atom = atoms_by_code.get(str(code).strip())
        if atom and _is_image_like_atom(atom):
            out.append(atom.atom_code)
    return out


def unit_has_experiment_section(unit: dict[str, Any]) -> bool:
    name = str(unit.get("section_name") or "")
    return any(k in name for k in ("探究", "实验", "活动"))
