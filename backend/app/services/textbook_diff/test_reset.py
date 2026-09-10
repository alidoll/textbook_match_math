"""Test 学科专用：清缓存 / 清库（不触及语文、化学册次）。"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import or_

from ...extensions import db
from ...models import (
    DiffLessonPair,
    DiffPageAtomSnapshot,
    DiffPageCompare,
    FileBlob,
    Lesson,
    Volume,
    VolumeDraftPdf,
)
from ...parsers.llm_toc_cache import clear_llm_toc_cache
from ...repo_paths import base_data_dir, repo_root
from ..lesson_delete import delete_all_lessons_for_volume
from ..volume_pdf import resolve_volume_pdf_path
from .sandbox_subjects import (
    is_sandbox_subject_label,
    is_sandbox_volume_code,
    require_sandbox_pair_codes,
)

_log = logging.getLogger(__name__)


def _require_test_volume(volume: Volume) -> None:
    sub = (volume.subject or "").strip()
    if not is_sandbox_subject_label(sub):
        raise ValueError(f"仅允许操作 Test / 小科 册次，当前为：{volume.subject}")
    if not is_sandbox_volume_code(volume.volume_code):
        raise ValueError(f"册次编码不是 TEST-* / XK*-*：{volume.volume_code}")


def _load_test_pair(old_code: str, new_code: str) -> tuple[Volume, Volume]:
    from .volumes import get_diff_volume_by_code

    require_sandbox_pair_codes(old_code, new_code)
    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    _require_test_volume(old_vol)
    _require_test_volume(new_vol)
    if (old_vol.subject or "").strip() != (new_vol.subject or "").strip():
        raise ValueError("新旧册次学科不一致，Test 与小科数据相互隔离")
    return old_vol, new_vol


def _collect_blob_ids(volumes: list[Volume]) -> set[str]:
    ids: set[str] = set()
    vol_ids = [v.id for v in volumes]
    for v in volumes:
        if v.blob_id:
            ids.add(v.blob_id)
        if v.preview_blob_id:
            ids.add(v.preview_blob_id)
    for d in VolumeDraftPdf.query.filter(VolumeDraftPdf.volume_id.in_(vol_ids)).all():
        if d.blob_id:
            ids.add(d.blob_id)
    return ids


def _hash12_for_blobs(blob_ids: set[str]) -> set[str]:
    out: set[str] = set()
    for bid in blob_ids:
        b = db.session.get(FileBlob, bid)
        if b and b.content_hash:
            out.add(b.content_hash[:12])
            out.add(b.content_hash)
    return out


def clear_test_pair_cache(*, old_code: str, new_code: str) -> dict[str, Any]:
    """清 OCR/对比/目录缓存与页级 DB 快照；保留册次、课时、粗分课对。"""
    old_vol, new_vol = _load_test_pair(old_code, new_code)
    volumes = [old_vol, new_vol]
    vol_ids = [v.id for v in volumes]
    codes = [v.volume_code for v in volumes]
    blob_ids = _collect_blob_ids(volumes)
    hashes = _hash12_for_blobs(blob_ids)

    uids = [
        les.lesson_uid
        for v in volumes
        for les in Lesson.query.filter_by(volume_id=v.id).all()
    ]

    removed: dict[str, int] = {}
    cache = Path(base_data_dir()) / "cache"

    def _rm(paths: list[Path], label: str) -> None:
        n = 0
        for p in paths:
            try:
                if p.is_file():
                    p.unlink(missing_ok=True)
                    n += 1
            except OSError as exc:
                _log.warning("删除缓存失败 %s: %s", p, exc)
        removed[label] = n

    # page_text
    pt_dir = cache / "page_text"
    pt_del: list[Path] = []
    if pt_dir.is_dir():
        for uid in uids:
            safe = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", uid)
            pt_del.extend(pt_dir.glob(f"{safe}_p*.json"))
        for p in pt_dir.glob("*.json"):
            if any(c in p.name for c in codes) or any(
                f"diff_{c}" in p.name for c in codes
            ):
                pt_del.append(p)
    _rm(list({p.resolve(): p for p in pt_del}.values()), "page_text")

    # diff_page_atoms
    da_dir = cache / "diff_page_atoms"
    da_del: list[Path] = []
    if da_dir.is_dir() and hashes:
        for p in da_dir.glob("*.json"):
            if any(h[:12] in p.name for h in hashes):
                da_del.append(p)
    _rm(da_del, "diff_page_atoms")

    # page_layout / pdf_page_png
    for sub in ("page_layout", "pdf_page_png"):
        d = cache / sub
        hit: list[Path] = []
        if d.is_dir() and hashes:
            for p in d.glob("*"):
                if any(h[:12] in p.name or h in p.name for h in hashes):
                    hit.append(p)
        _rm(hit, sub)

    # llm toc
    toc_n = 0
    for v in volumes:
        if not (v.blob_id or v.preview_blob_id):
            continue
        try:
            pdf = resolve_volume_pdf_path(v, source="auto")
        except Exception:
            continue
        bid = v.blob_id or v.preview_blob_id
        blob = db.session.get(FileBlob, bid) if bid else None
        clear_llm_toc_cache(
            pdf, content_hash=(blob.content_hash if blob else None)
        )
        toc_n += 1
    removed["llm_toc"] = toc_n

    n_snap = DiffPageAtomSnapshot.query.filter(
        DiffPageAtomSnapshot.volume_id.in_(vol_ids)
    ).delete(synchronize_session=False)
    n_cmp = DiffPageCompare.query.filter(
        or_(
            DiffPageCompare.old_volume_id.in_(vol_ids),
            DiffPageCompare.new_volume_id.in_(vol_ids),
        )
    ).delete(synchronize_session=False)
    db.session.commit()
    removed["db_atom_snapshots"] = int(n_snap or 0)
    removed["db_page_compares"] = int(n_cmp or 0)

    return {"ok": True, "old_code": old_code, "new_code": new_code, "removed": removed}


def clear_test_pair_db(*, old_code: str, new_code: str) -> dict[str, Any]:
    """清空本对 Test 库内数据并解绑 PDF，保留册次码便于重新上传测试。

    不删除仍被其它学科引用的 file_blobs。
    """
    old_vol, new_vol = _load_test_pair(old_code, new_code)
    volumes = [old_vol, new_vol]
    vol_ids = [v.id for v in volumes]
    codes = [v.volume_code for v in volumes]
    blob_ids = _collect_blob_ids(volumes)

    summary: dict[str, Any] = {"old_code": old_code, "new_code": new_code}

    pairs = DiffLessonPair.query.filter(
        or_(
            DiffLessonPair.old_volume_id.in_(vol_ids),
            DiffLessonPair.new_volume_id.in_(vol_ids),
        )
    ).all()
    for p in pairs:
        db.session.delete(p)
    if pairs:
        db.session.flush()
    summary["diff_lesson_pairs"] = len(pairs)

    n_snap = DiffPageAtomSnapshot.query.filter(
        DiffPageAtomSnapshot.volume_id.in_(vol_ids)
    ).delete(synchronize_session=False)
    n_cmp = DiffPageCompare.query.filter(
        or_(
            DiffPageCompare.old_volume_id.in_(vol_ids),
            DiffPageCompare.new_volume_id.in_(vol_ids),
        )
    ).delete(synchronize_session=False)
    db.session.flush()
    summary["diff_page_atom_snapshots"] = int(n_snap or 0)
    summary["diff_page_compares"] = int(n_cmp or 0)

    lessons_n = 0
    for v in volumes:
        lessons_n += delete_all_lessons_for_volume(v)
    summary["lessons"] = lessons_n

    drafts = VolumeDraftPdf.query.filter(VolumeDraftPdf.volume_id.in_(vol_ids)).all()
    for d in drafts:
        db.session.delete(d)
    if drafts:
        db.session.flush()
    summary["volume_draft_pdfs"] = len(drafts)

    # 解绑 PDF，重置解析状态（保留 Volume 行）
    for v in volumes:
        v.blob_id = None
        v.preview_blob_id = None
        v.parse_status = "pending"
        v.parse_error = None
        v.parse_suggestions_json = None
    db.session.flush()

    # 仅删无人引用的 blob
    blobs_deleted = 0
    for bid in blob_ids:
        still_vol = Volume.query.filter(
            or_(Volume.blob_id == bid, Volume.preview_blob_id == bid)
        ).count()
        still_draft = VolumeDraftPdf.query.filter_by(blob_id=bid).count()
        if still_vol or still_draft:
            continue
        blob = db.session.get(FileBlob, bid)
        if not blob:
            continue
        # 仅删本 TEST 册镜像文件，避免误删其它学科仍在用的同内容路径
        rel = (blob.storage_path or "").replace("\\", "/")
        owned = any(
            rel.endswith(f"/{code}.pdf")
            or f"/{code}.draft." in f"/{rel}"
            for code in codes
        )
        if owned and blob.storage_path:
            p = repo_root() / blob.storage_path
            if p.is_file():
                p.unlink(missing_ok=True)
        db.session.delete(blob)
        blobs_deleted += 1
    db.session.commit()
    summary["file_blobs"] = blobs_deleted

    disk_n = 0
    root = repo_root()
    for code in codes:
        for path in (
            root / "data" / "lesson-pages" / code,
            root / "data" / "diff-textbook" / "draft-pages" / code,
            root / "data" / "diff-textbook" / "draft-page-maps" / f"{code}.json",
            root / "data" / "diff-textbook" / f"{code}.pdf",
        ):
            if path.is_file():
                path.unlink(missing_ok=True)
                disk_n += 1
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                disk_n += 1
        # draft mirrors: CODE.draft.*.pdf
        dt = root / "data" / "diff-textbook"
        if dt.is_dir():
            for p in dt.glob(f"{code}.draft*.pdf"):
                p.unlink(missing_ok=True)
                disk_n += 1
    summary["disk_paths_removed"] = disk_n

    # 顺带清缓存（课都没了，缓存无用）
    try:
        cache_result = clear_test_pair_cache(old_code=old_code, new_code=new_code)
        summary["cache"] = cache_result.get("removed") or {}
    except Exception as exc:
        _log.warning("清库后清缓存跳过: %s", exc)
        summary["cache"] = {"skipped": str(exc)}

    return {"ok": True, **summary}
