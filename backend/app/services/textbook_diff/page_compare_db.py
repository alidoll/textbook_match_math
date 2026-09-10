"""教材对比单页 OCR / 比对结果 MySQL 读写。"""
from __future__ import annotations

import logging
from typing import Any

from ...extensions import db
from ...models import DiffPageAtomSnapshot, DiffPageCompare, Volume

_log = logging.getLogger(__name__)

# 与 atom_compare 保持同一版本号，否则 MySQL 里的新比对结果永远灌不进内存（旧版会盖住新逻辑）
from .atom_compare import (  # noqa: E402
    _CACHE_VERSION as _ATOMS_VERSION,
    _IMAGE_COMPARE_CACHE_VERSION as _IMAGE_COMPARE_VERSION,
    _TEXT_COMPARE_CACHE_VERSION as _TEXT_COMPARE_VERSION,
)


def _preview_key(preview_blob_id: str | None) -> str:
    return (preview_blob_id or "").strip()


def _vol_code(volume: Volume) -> str:
    return str(getattr(volume, "volume_code", None) or getattr(volume, "id", "") or "")


def upsert_page_atom_snapshot(
    *,
    volume: Volume,
    page_1: int,
    pdf_source: str = "full",
    preview_blob_id: str | None = None,
    atoms: list[dict],
    text_ocr_done: bool,
    image_ocr_done: bool,
) -> None:
    """按册+页 upsert OCR 原子快照。"""
    if page_1 < 1 or not volume or not getattr(volume, "id", None):
        return
    src = (pdf_source or "full").strip() or "full"
    if src != "draft":
        src = "full"
        preview_blob_id = None
    preview = _preview_key(preview_blob_id)
    try:
        row = DiffPageAtomSnapshot.query.filter_by(
            volume_id=volume.id,
            page_1=page_1,
            pdf_source=src,
            preview_blob_id=preview,
        ).one_or_none()
        if row is None:
            row = DiffPageAtomSnapshot(
                volume_id=volume.id,
                volume_code=_vol_code(volume),
                page_1=page_1,
                pdf_source=src,
                preview_blob_id=preview,
                atoms_json=list(atoms or []),
                text_ocr_done=bool(text_ocr_done),
                image_ocr_done=bool(image_ocr_done),
                atoms_version=_ATOMS_VERSION,
            )
            db.session.add(row)
        else:
            row.volume_code = _vol_code(volume)
            row.atoms_json = list(atoms or [])
            row.text_ocr_done = bool(text_ocr_done)
            row.image_ocr_done = bool(image_ocr_done)
            row.atoms_version = _ATOMS_VERSION
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        _log.warning("diff_page_atom_snapshots 写入失败: %s", exc)


def load_page_atom_snapshot(
    *,
    volume: Volume,
    page_1: int,
    pdf_source: str = "full",
    preview_blob_id: str | None = None,
) -> dict[str, Any] | None:
    src = (pdf_source or "full").strip() or "full"
    if src != "draft":
        src = "full"
        preview_blob_id = None
    preview = _preview_key(preview_blob_id)
    try:
        row = DiffPageAtomSnapshot.query.filter_by(
            volume_id=volume.id,
            page_1=page_1,
            pdf_source=src,
            preview_blob_id=preview,
        ).one_or_none()
    except Exception as exc:
        _log.warning("diff_page_atom_snapshots 读取失败: %s", exc)
        return None
    if not row:
        return None
    atoms = row.atoms_json
    if not isinstance(atoms, list):
        atoms = []
    return {
        "atoms": atoms,
        "text_ocr_done": bool(row.text_ocr_done),
        "image_ocr_done": bool(row.image_ocr_done),
        "atoms_version": int(row.atoms_version or 0),
    }


def upsert_page_compare(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: str = "full",
    preview_blob_id: str | None = None,
    text_compare: dict | None = None,
    image_compare: dict | None = None,
    text_compare_version: int | None = None,
    image_compare_version: int | None = None,
    clear_text: bool = False,
    clear_image: bool = False,
) -> None:
    """upsert 页对比对；可只更新一侧 JSON。"""
    if not old_vol or not new_vol:
        return
    src = (new_pdf_source or "full").strip() or "full"
    if src != "draft":
        src = "full"
        preview_blob_id = None
    preview = _preview_key(preview_blob_id)
    try:
        row = DiffPageCompare.query.filter_by(
            old_volume_id=old_vol.id,
            new_volume_id=new_vol.id,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=src,
            preview_blob_id=preview,
        ).one_or_none()
        if row is None:
            row = DiffPageCompare(
                old_volume_id=old_vol.id,
                new_volume_id=new_vol.id,
                old_code=_vol_code(old_vol),
                new_code=_vol_code(new_vol),
                old_page=old_page,
                new_page=new_page,
                new_pdf_source=src,
                preview_blob_id=preview,
            )
            db.session.add(row)
        else:
            row.old_code = _vol_code(old_vol)
            row.new_code = _vol_code(new_vol)

        if clear_text:
            row.text_compare = None
            row.text_compare_version = None
        elif text_compare is not None:
            row.text_compare = text_compare
            row.text_compare_version = text_compare_version

        if clear_image:
            row.image_compare = None
            row.image_compare_version = None
        elif image_compare is not None:
            row.image_compare = image_compare
            row.image_compare_version = image_compare_version

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        _log.warning("diff_page_compares 写入失败: %s", exc)


def load_page_compare(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: str = "full",
    preview_blob_id: str | None = None,
) -> dict[str, Any] | None:
    src = (new_pdf_source or "full").strip() or "full"
    if src != "draft":
        src = "full"
        preview_blob_id = None
    preview = _preview_key(preview_blob_id)
    try:
        row = DiffPageCompare.query.filter_by(
            old_volume_id=old_vol.id,
            new_volume_id=new_vol.id,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=src,
            preview_blob_id=preview,
        ).one_or_none()
    except Exception as exc:
        _log.warning("diff_page_compares 读取失败: %s", exc)
        return None
    if not row:
        return None
    return {
        "text_compare": row.text_compare if isinstance(row.text_compare, dict) else None,
        "image_compare": row.image_compare if isinstance(row.image_compare, dict) else None,
        "text_compare_version": row.text_compare_version,
        "image_compare_version": row.image_compare_version,
    }


def hydrate_pair_entry_from_db(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: str = "full",
    preview_blob_id: str | None = None,
    entry: dict | None = None,
) -> dict:
    """用库中的单侧原子 + 比对结果填充/补全页对 entry（库优先补全）。"""
    out = dict(entry or {})
    out.setdefault("old_page", old_page)
    out.setdefault("new_page", new_page)
    out["old_raw"] = list(out.get("old_raw") or [])
    out["new_raw"] = list(out.get("new_raw") or [])
    out["old_text_ocr_done"] = bool(out.get("old_text_ocr_done"))
    out["new_text_ocr_done"] = bool(out.get("new_text_ocr_done"))
    out["old_image_ocr_done"] = bool(out.get("old_image_ocr_done"))
    out["new_image_ocr_done"] = bool(out.get("new_image_ocr_done"))

    def _merge_side(prefix: str, snap: dict | None) -> None:
        if not snap:
            return
        # 原子 OCR 可跨版本复用；仅当入口已有更新版原子时才不覆盖
        snap_ver = int(snap.get("atoms_version") or 0)
        raw_key = f"{prefix}_raw"
        text_key = f"{prefix}_text_ocr_done"
        img_key = f"{prefix}_image_ocr_done"
        file_raw = out.get(raw_key) or []
        file_text = bool(out.get(text_key))
        file_img = bool(out.get(img_key))
        entry_ver = int(out.get("cache_version") or 0)
        # 本地已是当前/更新缓存且已有 OCR → 不拿旧库快照盖掉
        if file_raw and file_text and entry_ver >= _ATOMS_VERSION and snap_ver < _ATOMS_VERSION:
            return
        if (not file_raw) or (snap["image_ocr_done"] and not file_img) or (
            snap["text_ocr_done"] and not file_text
        ):
            out[raw_key] = snap["atoms"]
            out[text_key] = snap["text_ocr_done"]
            out[img_key] = snap["image_ocr_done"]
            if snap_ver:
                out.setdefault("_atoms_versions", {})[prefix] = snap_ver

    _merge_side(
        "old",
        load_page_atom_snapshot(
            volume=old_vol, page_1=old_page, pdf_source="full", preview_blob_id=None
        ),
    )
    _merge_side(
        "new",
        load_page_atom_snapshot(
            volume=new_vol,
            page_1=new_page,
            pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
        ),
    )

    cmp = load_page_compare(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    if cmp:
        # 仅采纳版本匹配的比对；旧版 JSON 不得覆盖新逻辑
        tc_ver = cmp.get("text_compare_version")
        if cmp.get("text_compare") is not None and int(tc_ver or 0) == _TEXT_COMPARE_VERSION:
            out["text_compare"] = cmp["text_compare"]
            out["text_compare_version"] = tc_ver
        if cmp.get("image_compare") is not None and int(
            cmp.get("image_compare_version") or 0
        ) == _IMAGE_COMPARE_VERSION:
            out["image_compare"] = cmp["image_compare"]
            out["image_compare_version"] = cmp.get("image_compare_version")
    return out
