"""Test 学科：对比内容先落本地 JSON，再一键提交 MySQL（不改语文/化学路径）。"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ...extensions import db
from ...models import FileBlob, Volume
from ...repo_paths import base_data_dir
from .atom_compare import (
    _CACHE_VERSION,
    _IMAGE_COMPARE_CACHE_VERSION,
    _TEXT_COMPARE_CACHE_VERSION,
    _read_cache_entry,
)
from .page_compare_db import upsert_page_atom_snapshot, upsert_page_compare
from .sandbox_subjects import is_sandbox_subject_label, require_sandbox_pair_codes

_log = logging.getLogger(__name__)


def is_test_volume(volume: Volume | None) -> bool:
    """沙箱学科（Test / 小科）：对比过程只写本地 JSON。"""
    if volume is None:
        return False
    return is_sandbox_subject_label(getattr(volume, "subject", None))


def should_persist_compare_to_db(*volumes: Volume | None) -> bool:
    """Test 册对比过程中不写 OCR/比对到 MySQL。"""
    if any(is_test_volume(v) for v in volumes):
        return False
    return True


def should_hydrate_compare_from_db(*volumes: Volume | None) -> bool:
    """Test 册读取时只信本地 JSON，避免对比过程打 MySQL。"""
    return should_persist_compare_to_db(*volumes)


def _require_test_pair(old_code: str, new_code: str) -> tuple[Volume, Volume]:
    from .volumes import get_diff_volume_by_code

    require_sandbox_pair_codes(old_code, new_code)
    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    if not is_test_volume(old_vol) or not is_test_volume(new_vol):
        raise ValueError("仅允许 Test / 小科 学科册次提交本地 JSON")
    if (old_vol.subject or "").strip() != (new_vol.subject or "").strip():
        raise ValueError("新旧册次学科不一致，Test 与小科数据相互隔离")
    return old_vol, new_vol


def _hash12(volume: Volume) -> str | None:
    if not volume.blob_id:
        return None
    blob = db.session.get(FileBlob, volume.blob_id)
    if not blob or not blob.content_hash:
        return None
    return blob.content_hash[:12]


def list_test_local_cache_files(old_vol: Volume, new_vol: Volume) -> list[Path]:
    """本对完整版 PDF 对应的 diff_page_atoms JSON 文件。"""
    oh, nh = _hash12(old_vol), _hash12(new_vol)
    if not oh or not nh:
        return []
    root = base_data_dir() / "cache" / "diff_page_atoms"
    if not root.is_dir():
        return []
    # v{ver}_f_{old12}_{new12}_o{n}_n{m}.json
    prefix = f"v{_CACHE_VERSION}_f_{oh}_{nh}_"
    return sorted(p for p in root.glob(f"{prefix}*.json") if p.is_file())


def _parse_pages_from_name(name: str) -> tuple[int, int] | None:
    m = re.search(r"_o(\d+)_n(\d+)\.json$", name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def load_test_local_compares_by_new_page(
    old_vol: Volume,
    new_vol: Volume,
) -> dict[int, dict[str, Any]]:
    """从本地 diff_page_atoms JSON 汇总 text/image_compare，供本册变动列展示。

    Test 对比过程不写 MySQL，章节变动统计须读本地缓存。
    """
    out: dict[int, dict[str, Any]] = {}
    if not is_test_volume(old_vol) or not is_test_volume(new_vol):
        return out
    for path in list_test_local_cache_files(old_vol, new_vol):
        pages = _parse_pages_from_name(path.name)
        if not pages:
            continue
        _old_page, new_page = pages
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        tc = data.get("text_compare") if isinstance(data.get("text_compare"), dict) else None
        ic = data.get("image_compare") if isinstance(data.get("image_compare"), dict) else None
        if not tc and not ic:
            continue
        out[int(new_page)] = {"text_compare": tc, "image_compare": ic}
    return out


def test_local_cache_summary(*, old_code: str, new_code: str) -> dict[str, Any]:
    old_vol, new_vol = _require_test_pair(old_code, new_code)
    files = list_test_local_cache_files(old_vol, new_vol)
    with_text = 0
    with_image = 0
    with_atoms = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("old_raw") or data.get("new_raw"):
            with_atoms += 1
        if isinstance(data.get("text_compare"), dict):
            with_text += 1
        if isinstance(data.get("image_compare"), dict):
            with_image += 1
    return {
        "ok": True,
        "old_code": old_code,
        "new_code": new_code,
        "cache_files": len(files),
        "pages_with_atoms": with_atoms,
        "pages_with_text_compare": with_text,
        "pages_with_image_compare": with_image,
        "db_writes_during_compare": False,
        "hint": "沙箱学科（Test / 小科）对比结果仅保存在本地 JSON；点「提交到数据库」再写入 MySQL。",
    }


def flush_test_local_json_to_db(*, old_code: str, new_code: str) -> dict[str, Any]:
    """把本对本地 diff_page_atoms JSON 批量写入 MySQL。"""
    old_vol, new_vol = _require_test_pair(old_code, new_code)
    files = list_test_local_cache_files(old_vol, new_vol)
    atoms_pages = 0
    text_pages = 0
    image_pages = 0
    skipped = 0
    errors: list[str] = []

    for path in files:
        pages = _parse_pages_from_name(path.name)
        if not pages:
            skipped += 1
            continue
        old_page, new_page = pages
        try:
            _cache_file, entry = _read_cache_entry(
                old_vol,
                new_vol,
                old_page,
                new_page,
                new_pdf_source="full",
                preview_blob_id=None,
            )
            if not entry:
                skipped += 1
                continue

            old_raw = entry.get("old_raw") if isinstance(entry.get("old_raw"), list) else []
            new_raw = entry.get("new_raw") if isinstance(entry.get("new_raw"), list) else []
            if old_raw or entry.get("old_text_ocr_done") or entry.get("old_image_ocr_done"):
                upsert_page_atom_snapshot(
                    volume=old_vol,
                    page_1=old_page,
                    pdf_source="full",
                    preview_blob_id=None,
                    atoms=list(old_raw or []),
                    text_ocr_done=bool(entry.get("old_text_ocr_done")),
                    image_ocr_done=bool(entry.get("old_image_ocr_done")),
                )
            if new_raw or entry.get("new_text_ocr_done") or entry.get("new_image_ocr_done"):
                upsert_page_atom_snapshot(
                    volume=new_vol,
                    page_1=new_page,
                    pdf_source="full",
                    preview_blob_id=None,
                    atoms=list(new_raw or []),
                    text_ocr_done=bool(entry.get("new_text_ocr_done")),
                    image_ocr_done=bool(entry.get("new_image_ocr_done")),
                )
            if old_raw or new_raw:
                atoms_pages += 1

            text_cmp = entry.get("text_compare")
            image_cmp = entry.get("image_compare")
            if isinstance(text_cmp, dict):
                upsert_page_compare(
                    old_vol=old_vol,
                    new_vol=new_vol,
                    old_page=old_page,
                    new_page=new_page,
                    new_pdf_source="full",
                    preview_blob_id=None,
                    text_compare=text_cmp,
                    text_compare_version=int(
                        entry.get("text_compare_version") or _TEXT_COMPARE_CACHE_VERSION
                    ),
                )
                text_pages += 1
            if isinstance(image_cmp, dict):
                upsert_page_compare(
                    old_vol=old_vol,
                    new_vol=new_vol,
                    old_page=old_page,
                    new_page=new_page,
                    new_pdf_source="full",
                    preview_blob_id=None,
                    image_compare=image_cmp,
                    image_compare_version=int(
                        entry.get("image_compare_version") or _IMAGE_COMPARE_CACHE_VERSION
                    ),
                )
                image_pages += 1
        except Exception as exc:
            errors.append(f"p{old_page}↔p{new_page}: {exc}")
            _log.warning("flush test json failed %s: %s", path.name, exc)

    return {
        "ok": True,
        "old_code": old_code,
        "new_code": new_code,
        "cache_files": len(files),
        "atoms_pages": atoms_pages,
        "text_pages": text_pages,
        "image_pages": image_pages,
        "skipped": skipped,
        "errors": errors[:20],
        "error_count": len(errors),
        "hint": "对比 JSON 已写入 MySQL；可在对比页查看。课对状态请用「本课时一键」或手动确认更新。",
    }
