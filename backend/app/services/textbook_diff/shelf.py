"""化学教材比对本册书架：追加版本、改名、仅 HXRJ。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ...extensions import db
from ...models import Volume
from .volume_create import (
    create_diff_volume,
    make_diff_volume_code,
    next_dnew_version,
    normalize_term,
    parse_diff_volume_code,
)
from .volumes import get_diff_volume_by_code
from .workbook import volume_preprocess_status

CHEM_PREFIX = "HXRJ"
CHEM_SUBJECT = "化学"


def shelf_enabled_for_subject(subject: str | None) -> bool:
    raw = (subject or "").strip()
    return raw in (CHEM_SUBJECT, "huaxue", "Huaxue")


def assert_chemistry_shelf(prefix: str) -> None:
    pfx = (prefix or "").strip().upper()
    if pfx != CHEM_PREFIX:
        raise ValueError("书架多版本仅支持化学（HXRJ）")


def default_version_label(*, role: str, uploaded_at: date | None = None) -> str:
    day = uploaded_at or date.today()
    role_key = (role or "").strip().lower()
    if role_key in ("old", "diff_old"):
        tag = "旧教材"
    elif role_key in ("new", "diff_new"):
        tag = "新教材"
    else:
        raise ValueError("role 须为 old 或 new")
    return f"{day.isoformat()} · {tag}"


def _shelf_item(volume: Volume) -> dict[str, Any]:
    created = volume.created_at
    if isinstance(created, datetime):
        day = created.date()
    else:
        day = date.today()
    role = "old" if volume.book_type == "diff_old" else "new"
    fallback = default_version_label(role=role, uploaded_at=day)
    label = (volume.version_label or "").strip() or fallback
    status = volume_preprocess_status(volume)
    return {
        "volume_code": volume.volume_code,
        "book_type": volume.book_type,
        "role": role,
        "version_label": volume.version_label,
        "display_title": volume.display_title,
        "default_name": fallback,
        "name": label,
        "created_at": created.isoformat(sep=" ", timespec="seconds") if created else None,
        "blob_bound": bool(status.get("has_pdf") or status.get("has_preview_pdf")),
        "preprocess": status,
    }


def list_shelf(*, prefix: str, grade: int, term: str) -> list[dict[str, Any]]:
    assert_chemistry_shelf(prefix)
    term_key = normalize_term(term)
    pfx = CHEM_PREFIX
    subject, edition = ("化学", "人教版")
    # 以码前缀定位本册
    base_old = make_diff_volume_code(
        grade=grade, term=term_key, book_type="diff_old", prefix=pfx
    )
    stem = base_old[: -len("-DOLD")]  # HXRJ-9S
    rows = (
        Volume.query.filter(
            Volume.volume_code.startswith(f"{stem}-"),
            Volume.book_type.in_(("diff_old", "diff_new")),
        )
        .order_by(Volume.created_at.asc(), Volume.volume_code.asc())
        .all()
    )
    items = [
        _shelf_item(v)
        for v in rows
        if v.subject == subject and int(v.grade) == int(grade)
    ]
    return items


def add_shelf_upload(
    *,
    prefix: str,
    grade: int,
    term: str,
    role: str,
    version_label: str | None = None,
) -> dict[str, Any]:
    """仅建卷（空 PDF）；上传走既有 intake。"""
    assert_chemistry_shelf(prefix)
    term_key = normalize_term(term)
    role_key = (role or "").strip().lower()
    label = (version_label or "").strip() or default_version_label(role=role_key)

    if role_key == "old":
        code = make_diff_volume_code(
            grade=grade, term=term_key, book_type="diff_old", prefix=CHEM_PREFIX
        )
        existing = Volume.query.filter_by(volume_code=code).first()
        if existing:
            if not (existing.version_label or "").strip():
                existing.version_label = label
                db.session.commit()
            return {
                "ok": True,
                "created": False,
                "message": "本册旧教材已存在，可改名或进入预处理上传/更换 PDF",
                **_shelf_item(existing),
            }
        created = create_diff_volume(
            grade=grade,
            term=term_key,
            book_type="diff_old",
            prefix=CHEM_PREFIX,
            version_label=label,
        )
        vol = get_diff_volume_by_code(created["volume_code"])
        return {"ok": True, "created": True, **_shelf_item(vol)}

    if role_key != "new":
        raise ValueError("role 须为 old 或 new")

    ver = next_dnew_version(prefix=CHEM_PREFIX, grade=grade, term=term_key)
    created = create_diff_volume(
        grade=grade,
        term=term_key,
        book_type="diff_new",
        prefix=CHEM_PREFIX,
        version=ver,
        version_label=label,
    )
    vol = get_diff_volume_by_code(created["volume_code"])
    return {
        "ok": True,
        "created": bool(created.get("created")),
        **_shelf_item(vol),
    }


def rename_shelf_item(*, volume_code: str, version_label: str) -> dict[str, Any]:
    vol = get_diff_volume_by_code(volume_code)
    prefix, _, _, _, _ = parse_diff_volume_code(vol.volume_code)
    assert_chemistry_shelf(prefix)
    name = (version_label or "").strip()
    if not name:
        raise ValueError("显示名不能为空")
    if len(name) > 256:
        raise ValueError("显示名过长")
    vol.version_label = name
    db.session.commit()
    return {"ok": True, **_shelf_item(vol)}


def open_compare_session(*, old_code: str, new_code: str) -> dict[str, Any]:
    """校验化学同册次后返回 workbook_pair_detail（左右侧由调用方指定）。"""
    from .workbook import workbook_pair_detail

    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    op, og, ot, _, _ = parse_diff_volume_code(old_vol.volume_code)
    np_, ng, nt, _, _ = parse_diff_volume_code(new_vol.volume_code)
    assert_chemistry_shelf(op)
    if op != np_ or og != ng or ot != nt:
        raise ValueError("须选择同一化学年级册次下的两本教材")
    if old_vol.volume_code == new_vol.volume_code:
        raise ValueError("对比两侧不能是同一本教材")
    detail = workbook_pair_detail(old_code=old_code, new_code=new_code)
    return {"ok": True, **detail}


def list_compare_archives(
    *,
    prefix: str,
    grade: int,
    term: str,
    items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """本册已落库的版本对比档案（有 DiffLessonPair 的 distinct 卷对）。"""
    from sqlalchemy import func

    from ...models import DiffLessonPair

    shelf_items = items if items is not None else list_shelf(prefix=prefix, grade=grade, term=term)
    if not shelf_items:
        return []
    by_code = {it["volume_code"]: it for it in shelf_items}
    codes = list(by_code.keys())
    vols = Volume.query.filter(Volume.volume_code.in_(codes)).all()
    id_to_code = {v.id: v.volume_code for v in vols}
    vol_ids = list(id_to_code.keys())
    if not vol_ids:
        return []

    rows = (
        db.session.query(
            DiffLessonPair.old_volume_id,
            DiffLessonPair.new_volume_id,
            func.count(DiffLessonPair.id),
        )
        .filter(
            DiffLessonPair.old_volume_id.in_(vol_ids),
            DiffLessonPair.new_volume_id.in_(vol_ids),
        )
        .group_by(DiffLessonPair.old_volume_id, DiffLessonPair.new_volume_id)
        .all()
    )
    archives: list[dict[str, Any]] = []
    for old_id, new_id, pair_count in rows:
        old_code = id_to_code.get(old_id)
        new_code = id_to_code.get(new_id)
        if not old_code or not new_code:
            continue
        old_it = by_code.get(old_code) or {}
        new_it = by_code.get(new_code) or {}
        archives.append(
            {
                "old_code": old_code,
                "new_code": new_code,
                "old_name": old_it.get("name") or old_code,
                "new_name": new_it.get("name") or new_code,
                "pair_count": int(pair_count or 0),
                "label": f"{old_it.get('name') or old_code} ↔ {new_it.get('name') or new_code}",
            }
        )
    archives.sort(key=lambda a: (a["old_code"], a["new_code"]))
    return archives
