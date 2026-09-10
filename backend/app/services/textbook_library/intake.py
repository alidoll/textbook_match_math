# -*- coding: utf-8 -*-
"""教材库入馆：ensure 册次 + 进度聚合。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ...extensions import db
from ...models import Block, CoursewareSlide, Lesson, LessonPage, TextbookAtom, Volume
from ..old_library.edition_registry import get_edition
from ..old_library.volume_codes import make_display_title, normalize_term
from .catalog import LIBRARY_SUBJECTS, shelf_status_for_volume
from .codes import (
    default_copy_label,
    make_library_volume_code,
    next_copy_version,
    parse_library_volume_code,
)


def ensure_library_volume(*, edition_id: str, grade: int, term: str) -> dict[str, Any]:
    """确保册次格至少有首本副本（v1）；已有则返回首本，不追加。"""
    ed = get_edition(edition_id)
    if ed.subject not in LIBRARY_SUBJECTS:
        raise ValueError("该版本不在教材库一期范围内")
    term_key = normalize_term(term)
    code = make_library_volume_code(
        ed, grade=int(grade), term=term_key, book_type="old", version=1
    )
    vol = Volume.query.filter_by(volume_code=code, book_type="old").first()
    created = False
    if not vol:
        existing = Volume.query.filter_by(volume_code=code).first()
        if existing:
            if existing.book_type != "old":
                raise ValueError(f"册次码 {code} 已存在且类型为 {existing.book_type}")
            vol = existing
        else:
            label = default_copy_label()
            vol = Volume(
                volume_code=code,
                subject=ed.subject,
                edition=ed.label,
                grade=int(grade),
                semester=term_key,
                book_type="old",
                display_title=make_display_title(ed, grade=int(grade), term=term_key),
                version_label=label,
                parse_status="pending",
            )
            db.session.add(vol)
            db.session.commit()
            created = True
    return {
        "ok": True,
        "created": created,
        "volume_code": vol.volume_code,
        "name": (vol.version_label or "").strip() or default_copy_label(),
        "display_title": vol.display_title,
        "edition_id": ed.edition_id,
        "edition_label": ed.label,
        "subject": ed.subject,
        "grade": vol.grade,
        "term": vol.semester,
        "has_old_benchmark": bool(ed.has_old_benchmark),
    }


def append_library_copy(
    *,
    edition_id: str,
    grade: int,
    term: str,
    version_label: str | None = None,
) -> dict[str, Any]:
    """在册次格追加一本新副本（下一 version）。"""
    ed = get_edition(edition_id)
    if ed.subject not in LIBRARY_SUBJECTS:
        raise ValueError("该版本不在教材库一期范围内")
    term_key = normalize_term(term)
    # 若格内尚无 v1，先建 v1；否则建 next
    from .codes import list_copy_codes_for_slot

    existing_codes = list_copy_codes_for_slot(ed, grade=int(grade), term=term_key)
    if not existing_codes:
        return ensure_library_volume(edition_id=edition_id, grade=grade, term=term_key)

    ver = next_copy_version(ed, grade=int(grade), term=term_key)
    code = make_library_volume_code(
        ed, grade=int(grade), term=term_key, book_type="old", version=ver
    )
    if Volume.query.filter_by(volume_code=code).first():
        raise ValueError(f"册次码已存在：{code}")
    label = (version_label or "").strip() or default_copy_label()
    vol = Volume(
        volume_code=code,
        subject=ed.subject,
        edition=ed.label,
        grade=int(grade),
        semester=term_key,
        book_type="old",
        display_title=make_display_title(ed, grade=int(grade), term=term_key),
        version_label=label,
        parse_status="pending",
    )
    db.session.add(vol)
    db.session.commit()
    return {
        "ok": True,
        "created": True,
        "volume_code": vol.volume_code,
        "name": label,
        "copy_version": ver,
        "edition_id": ed.edition_id,
        "grade": vol.grade,
        "term": vol.semester,
        "has_old_benchmark": bool(ed.has_old_benchmark),
    }


def rename_library_copy(*, volume_code: str, version_label: str) -> dict[str, Any]:
    name = (version_label or "").strip()
    if not name:
        raise ValueError("显示名不能为空")
    if len(name) > 256:
        raise ValueError("显示名过长")
    vol = Volume.query.filter_by(volume_code=volume_code.strip(), book_type="old").first()
    if not vol:
        raise ValueError(f"未找到教材库副本：{volume_code}")
    parse_library_volume_code(vol.volume_code)  # 校验码
    vol.version_label = name
    db.session.commit()
    return {
        "ok": True,
        "volume_code": vol.volume_code,
        "name": name,
        "version_label": name,
    }


def _purge_volume_disk_artifacts(volume_code: str) -> None:
    """清理本册页图/缓存目录（尽力删除，失败忽略）。"""
    import shutil

    from ...repo_paths import repo_root

    root = repo_root()
    code = (volume_code or "").strip()
    if not code:
        return
    for path in (
        root / "data" / "lesson-pages" / code,
        root / "data" / "diff-textbook" / "draft-pages" / code,
        root / "data" / "diff-textbook" / "draft-page-maps" / f"{code}.json",
    ):
        try:
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _delete_one_library_copy(vol: Volume) -> dict[str, int]:
    """硬删单本教材库副本及相关数据（调用方已校验码与 book_type）。"""
    from sqlalchemy import or_

    from ...models import (
        DiffLessonPair,
        DiffPageAtomSnapshot,
        DiffPageCompare,
        FileBlob,
        VolumeDraftPdf,
    )
    from ..lesson_delete import delete_all_lessons_for_volume

    code = vol.volume_code
    volume_id = vol.id
    blob_ids = {b for b in (vol.blob_id, vol.preview_blob_id) if b}

    lessons = Lesson.query.filter_by(volume_id=volume_id).all()
    lesson_ids = [les.id for les in lessons]
    pair_conds = [
        DiffLessonPair.old_volume_id == volume_id,
        DiffLessonPair.new_volume_id == volume_id,
    ]
    if lesson_ids:
        pair_conds.append(DiffLessonPair.new_lesson_id.in_(lesson_ids))
        pair_conds.append(DiffLessonPair.old_lesson_id.in_(lesson_ids))
    pairs = DiffLessonPair.query.filter(or_(*pair_conds)).all()
    for pair in pairs:
        db.session.delete(pair)
    if pairs:
        db.session.flush()

    DiffPageAtomSnapshot.query.filter_by(volume_id=volume_id).delete(
        synchronize_session=False
    )
    DiffPageCompare.query.filter(
        or_(
            DiffPageCompare.old_volume_id == volume_id,
            DiffPageCompare.new_volume_id == volume_id,
        )
    ).delete(synchronize_session=False)
    db.session.flush()

    lessons_n = delete_all_lessons_for_volume(vol)

    drafts = VolumeDraftPdf.query.filter_by(volume_id=volume_id).all()
    for d in drafts:
        if d.blob_id:
            blob_ids.add(d.blob_id)
        db.session.delete(d)
    if drafts:
        db.session.flush()

    db.session.delete(vol)
    db.session.flush()

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
        if blob.storage_path:
            from ...repo_paths import repo_root

            p = repo_root() / blob.storage_path
            if p.is_file():
                p.unlink(missing_ok=True)
        db.session.delete(blob)
        blobs_deleted += 1

    _purge_volume_disk_artifacts(code)
    return {
        "lessons_deleted": lessons_n,
        "diff_pairs_deleted": len(pairs),
        "drafts_deleted": len(drafts),
        "blobs_deleted": blobs_deleted,
    }


def delete_library_copies(*, volume_codes: list[str]) -> dict[str, Any]:
    """硬删多本教材库副本（*-OLD / *-OLD-vN）。"""
    raw = [str(c or "").strip() for c in (volume_codes or [])]
    codes = []
    seen: set[str] = set()
    for c in raw:
        if not c or c in seen:
            continue
        seen.add(c)
        codes.append(c)
    if not codes:
        raise ValueError("请选择要删除的副本")

    deleted: list[str] = []
    details: list[dict[str, Any]] = []
    for code in codes:
        try:
            parse_library_volume_code(code)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        vol = Volume.query.filter_by(volume_code=code, book_type="old").first()
        if not vol:
            raise ValueError(f"未找到教材库副本：{code}")
        stats = _delete_one_library_copy(vol)
        deleted.append(code)
        details.append({"volume_code": code, **stats})

    db.session.commit()
    return {
        "ok": True,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "details": details,
        "message": f"已删除 {len(deleted)} 本副本",
    }


def _counts_for_volume(vol: Volume) -> dict[str, int]:
    lessons = Lesson.query.filter_by(volume_id=vol.id).all()
    lesson_ids = [x.id for x in lessons]
    lesson_count = len(lessons)
    atom_count = 0
    block_count = 0
    page_image_count = 0
    slide_count = 0
    if lesson_ids:
        atom_count = (
            db.session.scalar(
                select(func.count()).select_from(TextbookAtom).where(
                    TextbookAtom.lesson_id.in_(lesson_ids)
                )
            )
            or 0
        )
        block_count = (
            db.session.scalar(
                select(func.count()).select_from(Block).where(Block.lesson_id.in_(lesson_ids))
            )
            or 0
        )
        page_image_count = (
            db.session.scalar(
                select(func.count()).select_from(LessonPage).where(
                    LessonPage.lesson_id.in_(lesson_ids),
                    LessonPage.blob_id.isnot(None),
                )
            )
            or 0
        )
        # 部分环境 LessonPage 可能无 image_blob_id；回退 slide 数
        slide_count = (
            db.session.scalar(
                select(func.count()).select_from(CoursewareSlide).where(
                    CoursewareSlide.lesson_id.in_(lesson_ids)
                )
            )
            or 0
        )
    return {
        "lesson_count": lesson_count,
        "atom_count": int(atom_count),
        "block_count": int(block_count),
        "page_image_count": int(page_image_count),
        "slide_count": int(slide_count),
    }


def intake_progress(volume_code: str) -> dict[str, Any]:
    code = (volume_code or "").strip()
    vol = Volume.query.filter_by(volume_code=code, book_type="old").first()
    if not vol:
        raise ValueError(f"未找到教材库册次：{code}")
    try:
        ed, _, _, _, _ = parse_library_volume_code(code)
        edition_id = ed.edition_id
        has_bench = bool(ed.has_old_benchmark)
    except ValueError:
        edition_id = None
        has_bench = False

    c = _counts_for_volume(vol)
    has_pdf = bool(vol.blob_id)
    catalog_done = c["lesson_count"] > 0
    atoms_done = c["atom_count"] > 0
    ocr_done = atoms_done  # 一期：有原子即视为 OCR 已落库
    blocks_done = c["block_count"] > 0
    images_done = c["page_image_count"] > 0 or c["slide_count"] > 0

    steps = [
        {
            "key": "upload",
            "label": "上传教材 PDF",
            "done": has_pdf,
            "detail": "已绑定 PDF" if has_pdf else "待上传",
        },
        {
            "key": "catalog",
            "label": "提取目录",
            "done": catalog_done,
            "detail": f"{c['lesson_count']} 课" if catalog_done else "待提取",
        },
        {
            "key": "ocr",
            "label": "全文 OCR",
            "done": ocr_done,
            "detail": f"{c['atom_count']} 原子" if ocr_done else "待 OCR",
        },
        {
            "key": "atoms",
            "label": "原子归档",
            "done": atoms_done,
            "detail": f"{c['atom_count']} 条" if atoms_done else "待归档",
        },
        {
            "key": "blocks",
            "label": "原子块归档",
            "done": blocks_done,
            "detail": f"{c['block_count']} 块" if blocks_done else "待建块",
        },
        {
            "key": "images",
            "label": "图片信息归档",
            "done": images_done,
            "detail": (
                f"页图 {c['page_image_count']}"
                if images_done
                else "待归档"
            ),
        },
    ]
    return {
        "ok": True,
        "volume_code": code,
        "display_title": vol.display_title,
        "edition_id": edition_id,
        "has_old_benchmark": has_bench,
        "status": shelf_status_for_volume(
            vol, lesson_count=c["lesson_count"], atom_count=c["atom_count"]
        ),
        "counts": c,
        "steps": steps,
        "annotate_hint": "/textbook-library/",
        "intake_url": f"/textbook-library/volumes/{code}/intake",
    }
