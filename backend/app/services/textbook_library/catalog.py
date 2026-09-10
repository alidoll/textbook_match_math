# -*- coding: utf-8 -*-
"""教材库馆藏目录：学科 → 版本 → 册次格（格内多副本）。"""
from __future__ import annotations

from typing import Any

from ...models import Lesson, TextbookAtom, Volume
from ..old_library.edition_registry import (
    GRADE_LABELS,
    edition_to_api_dict,
    get_edition,
    grade_term_pairs_for_edition,
    list_active_editions,
)
from ..old_library.volume_codes import make_volume_code, normalize_term
from .codes import (
    default_copy_label,
    list_copy_codes_for_slot,
    parse_library_volume_code,
    slot_code_prefix,
)

LIBRARY_SUBJECTS = ("化学", "数学", "科学", "生物")


def shelf_status_for_volume(
    vol: Volume | None,
    *,
    lesson_count: int = 0,
    atom_count: int = 0,
) -> str:
    if vol is None or not vol.blob_id:
        return "待入库"
    if lesson_count > 0 and atom_count > 0:
        return "已上架"
    return "建设中"


def _volume_metrics(vol: Volume) -> tuple[int, int, str]:
    lessons = Lesson.query.filter_by(volume_id=vol.id).all()
    lesson_count = len(lessons)
    atom_count = 0
    if lessons:
        ids = [x.id for x in lessons]
        atom_count = TextbookAtom.query.filter(TextbookAtom.lesson_id.in_(ids)).count()
    status = shelf_status_for_volume(
        vol, lesson_count=lesson_count, atom_count=int(atom_count or 0)
    )
    return lesson_count, int(atom_count or 0), status


def _copy_payload(vol: Volume) -> dict[str, Any]:
    lesson_count, atom_count, status = _volume_metrics(vol)
    try:
        _, _, _, _, ver = parse_library_volume_code(vol.volume_code)
    except ValueError:
        ver = 1
    name = (vol.version_label or "").strip() or default_copy_label(
        uploaded_day=(vol.created_at.date().isoformat() if vol.created_at else None)
    )
    return {
        "volume_code": vol.volume_code,
        "copy_version": ver,
        "name": name,
        "version_label": vol.version_label,
        "display_title": vol.display_title,
        "has_pdf": bool(vol.blob_id),
        "lesson_count": lesson_count,
        "atom_count": atom_count,
        "status": status,
        "created_at": vol.created_at.isoformat(sep=" ", timespec="seconds")
        if vol.created_at
        else None,
    }


def _rollup_slot_status(copy_statuses: list[str]) -> str:
    if not copy_statuses:
        return "待入库"
    if any(s == "建设中" for s in copy_statuses):
        return "建设中"
    if all(s == "已上架" for s in copy_statuses):
        return "已上架"
    if any(s == "已上架" for s in copy_statuses):
        return "建设中"
    return "待入库"


def list_library_editions(subject: str) -> list[dict[str, Any]]:
    sub = (subject or "").strip()
    if sub not in LIBRARY_SUBJECTS:
        raise ValueError(f"学科仅支持：{'、'.join(LIBRARY_SUBJECTS)}")
    rows: list[dict[str, Any]] = []
    for ed in list_active_editions():
        if ed.subject != sub:
            continue
        d = edition_to_api_dict(ed)
        d["subject"] = ed.subject
        d["label"] = ed.label
        d["code_prefix"] = ed.code_prefix
        rows.append(d)
    return rows


def list_library_volume_slots(edition_id: str) -> list[dict[str, Any]]:
    ed = get_edition(edition_id)
    if ed.subject not in LIBRARY_SUBJECTS:
        raise ValueError("该版本不在教材库一期范围内")
    slots: list[dict[str, Any]] = []
    for grade, term in grade_term_pairs_for_edition(ed):
        base = slot_code_prefix(ed, grade=grade, term=term)
        codes = list_copy_codes_for_slot(ed, grade=grade, term=term)
        copies: list[dict[str, Any]] = []
        for code in codes:
            vol = Volume.query.filter_by(volume_code=code, book_type="old").first()
            if vol:
                copies.append(_copy_payload(vol))
        slots.append(
            {
                "edition_id": ed.edition_id,
                "grade": grade,
                "term": term,
                "label": f"{GRADE_LABELS[grade]}{'上册' if term == '上' else '下册'}",
                "slot_code": base,
                "copy_count": len(copies),
                "copies": copies,
                "status": _rollup_slot_status([c["status"] for c in copies]),
                # 兼容旧字段：指向首本或空
                "volume_code": copies[0]["volume_code"] if copies else base,
                "in_db": bool(copies),
                "has_pdf": any(c["has_pdf"] for c in copies),
                "lesson_count": sum(c["lesson_count"] for c in copies),
                "atom_count": sum(c["atom_count"] for c in copies),
            }
        )
    return slots


def list_slot_copies(*, edition_id: str, grade: int, term: str) -> dict[str, Any]:
    ed = get_edition(edition_id)
    if ed.subject not in LIBRARY_SUBJECTS:
        raise ValueError("该版本不在教材库一期范围内")
    term_key = normalize_term(term)
    base = slot_code_prefix(ed, grade=int(grade), term=term_key)
    codes = list_copy_codes_for_slot(ed, grade=int(grade), term=term_key)
    copies = []
    for code in codes:
        vol = Volume.query.filter_by(volume_code=code, book_type="old").first()
        if vol:
            copies.append(_copy_payload(vol))
    return {
        "ok": True,
        "edition_id": ed.edition_id,
        "edition_label": ed.label,
        "subject": ed.subject,
        "grade": int(grade),
        "term": term_key,
        "label": f"{GRADE_LABELS[int(grade)]}{'上册' if term_key == '上' else '下册'}",
        "slot_code": base,
        "copy_count": len(copies),
        "status": _rollup_slot_status([c["status"] for c in copies]),
        "copies": copies,
    }
