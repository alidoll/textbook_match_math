"""解析成功后：按课内页码渲染 PNG 写入 lesson_pages。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from ....extensions import db
from ....models import FileBlob, Lesson, LessonPage, Volume
from ....parsers.pdf_render import render_pdf_page_png
from ....parsers.pdf_spread import layout_for_volume, render_view_page_png
from ....repo_paths import lesson_page_png_path, lesson_pages_dir, repo_root
from ...blobs import _rel_storage_path, store_blob

_log = logging.getLogger(__name__)


def _delete_lesson_pages_for_lessons(lesson_ids: list[str]) -> None:
    if not lesson_ids:
        return

    pages = LessonPage.query.filter(LessonPage.lesson_id.in_(lesson_ids)).all()
    blob_ids = {p.blob_id for p in pages if p.blob_id}

    for page in pages:
        db.session.delete(page)
    db.session.flush()

    for bid in blob_ids:
        still_used = LessonPage.query.filter_by(blob_id=bid).first()
        if still_used:
            continue
        blob = db.session.get(FileBlob, bid)
        if not blob:
            continue
        if blob.storage_path:
            path = repo_root() / blob.storage_path
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                _log.warning("删除页图失败 %s: %s", path, exc)
        db.session.delete(blob)
    db.session.flush()
    _delete_orphan_lesson_page_blobs(lesson_ids)


def _delete_orphan_lesson_page_blobs(lesson_ids: list[str]) -> None:
    """清掉已无 lesson_pages 引用、但仍占着课内页图路径/哈希的 blob。"""
    lessons = Lesson.query.filter(Lesson.id.in_(lesson_ids)).all()
    if not lessons:
        return
    volumes = {
        vol.id: vol
        for vol in Volume.query.filter(
            Volume.id.in_({les.volume_id for les in lessons})
        ).all()
    }
    for les in lessons:
        vol = volumes.get(les.volume_id)
        if not vol:
            continue
        prefix = _rel_storage_path(
            lesson_page_png_path(vol.volume_code, les.lesson_uid, 1).parent
        )
        orphans = FileBlob.query.filter(FileBlob.storage_path.like(f"{prefix}/%")).all()
        for blob in orphans:
            if LessonPage.query.filter_by(blob_id=blob.id).first():
                continue
            if blob.storage_path:
                path = repo_root() / blob.storage_path
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    _log.warning("删除孤儿页图失败 %s: %s", path, exc)
            db.session.delete(blob)
    db.session.flush()


def _remove_empty_lesson_dirs(volume_code: str, lesson_uids: list[str]) -> None:
    base = lesson_pages_dir() / volume_code
    for uid in lesson_uids:
        d = base / uid.replace("\\", "_").replace("/", "_")
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def build_lesson_pages_from_parse(
    *,
    volume: Volume,
    pdf_path: Path,
    row_updates: list[dict[str, Any]],
    render_dpi: int = 120,
) -> int:
    """
    根据 page_start/end 渲染每页 PNG，写入 lesson_pages + file_blobs + 磁盘。
    返回写入的 lesson_pages 行数。
    """
    lesson_ids = [r["lesson_id"] for r in row_updates]
    lessons_by_id = {
        les.id: les
        for les in Lesson.query.filter(Lesson.id.in_(lesson_ids)).all()
    }

    uids = [les.lesson_uid for les in lessons_by_id.values()]
    _delete_lesson_pages_for_lessons(lesson_ids)
    _remove_empty_lesson_dirs(volume.volume_code, uids)

    written = 0
    layout = layout_for_volume(volume)
    for row in row_updates:
        les = lessons_by_id.get(row["lesson_id"])
        if not les:
            continue
        page_start = row["page_start"]
        page_end = row["page_end"]
        if not page_start or not page_end or page_end < page_start:
            continue

        for pdf_page in range(page_start, page_end + 1):
            page_index = pdf_page - page_start + 1
            if layout == "spread":
                png = render_view_page_png(
                    pdf_path, pdf_page, dpi=render_dpi, layout=layout
                )
            else:
                png = render_pdf_page_png(pdf_path, pdf_page - 1, dpi=render_dpi)
            dest = lesson_page_png_path(volume.volume_code, les.lesson_uid, page_index)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(png)

            # 不去重复用其它课的 blob：否则易串课（同 hash 指向别课目录）
            blob, _ = store_blob(
                content=png,
                mime_type="image/png",
                disk_path=dest,
                storage_path=dest,
                force_new=True,
            )
            lp = LessonPage(
                lesson_id=les.id,
                page_index=page_index,
                blob_id=blob.id,
            )
            db.session.add(lp)
            written += 1

    return written
