"""创建新教材册次（无需基准库）。"""
from __future__ import annotations

from ...extensions import db
from ...models import Volume
from ..old_library.edition_registry import get_edition
from ..old_library.volume_codes import (
    make_display_title,
    make_volume_code,
    normalize_term,
    parse_volume_code,
)
from .volumes import volume_detail_dict


def create_new_volume(
    *,
    edition_id: str,
    grade: int,
    term: str,
) -> dict:
    if grade < 1 or grade > 6:
        raise ValueError("年级须为 1–6")
    edition = get_edition(edition_id)
    volume_code = make_volume_code(edition, grade=grade, term=term, book_type="new")
    term_key = normalize_term(term)

    existing = Volume.query.filter_by(volume_code=volume_code).first()
    if existing:
        return {
            "ok": True,
            "created": False,
            **volume_detail_dict(existing),
        }

    volume = Volume(
        volume_code=volume_code,
        subject=edition.subject,
        edition=edition.label,
        grade=grade,
        semester=term_key,
        book_type="new",
        display_title=make_display_title(edition, grade=grade, term=term),
        parse_status="pending",
    )
    db.session.add(volume)
    db.session.commit()
    return {
        "ok": True,
        "created": True,
        **volume_detail_dict(volume),
    }


def ensure_new_volume_by_code(volume_code: str) -> dict:
    """按 volume_code 创建空册次（新库建设入口，无需基准目录）。"""
    edition, grade, term, book_type = parse_volume_code(volume_code)
    if book_type != "new":
        raise ValueError("仅支持新教材册次")
    return create_new_volume(edition_id=edition.edition_id, grade=grade, term=term)
