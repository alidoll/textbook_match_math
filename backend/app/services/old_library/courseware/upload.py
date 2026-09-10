"""课件 ZIP → courseware_slides。"""
from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path

from ....extensions import db
from ....models import CoursewareSlide
from ....services.blobs import store_blob
from ..lessons import get_lesson_by_uid

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_SLIDE_NUM_RE = re.compile(r"(\d+)")


def _slide_sort_key(name: str) -> tuple[int, str]:
    stem = Path(name).stem
    m = _SLIDE_NUM_RE.search(stem)
    num = int(m.group(1)) if m else 9999
    return num, name.lower()


def _mime_for_name(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/jpeg")


def upload_courseware_zip(
    *,
    lesson_uid: str,
    content: bytes,
    filename: str | None = None,
    replace: bool = True,
    commit: bool = True,
) -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type="old")
    if not les.old_course_id:
        raise ValueError("该课时无课件 id（old_course_id），无法登记幻灯片")

    if not content:
        raise ValueError("ZIP 文件为空")

    with tempfile.TemporaryDirectory(prefix="cwzip_") as tmp:
        zip_path = Path(tmp) / (filename or "courseware.zip")
        zip_path.write_bytes(content)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                entries = [
                    n
                    for n in zf.namelist()
                    if not n.endswith("/") and Path(n).suffix.lower() in _IMAGE_EXTS
                ]
                if not entries:
                    raise ValueError("ZIP 内未找到 jpg/png 等幻灯片图片")

                entries.sort(key=_slide_sort_key)

                if replace:
                    CoursewareSlide.query.filter_by(lesson_id=les.id).delete()
                    db.session.flush()

                written = 0
                for slide_index, entry in enumerate(entries, start=1):
                    data = zf.read(entry)
                    blob, _ = store_blob(content=data, mime_type=_mime_for_name(entry))
                    db.session.add(
                        CoursewareSlide(
                            lesson_id=les.id,
                            old_course_id=les.old_course_id,
                            slide_index=slide_index,
                            blob_id=blob.id,
                            fetch_status="uploaded",
                        )
                    )
                    written += 1
        except zipfile.BadZipFile as exc:
            raise ValueError("不是有效的 ZIP 文件") from exc

    les.slides_fetch_status = "uploaded"
    if commit:
        db.session.commit()
    else:
        db.session.flush()

    warning = None
    if les.page_count and written != les.page_count:
        warning = (
            f"幻灯片 {written} 张与基准库页数 {les.page_count} 不一致，请核对"
        )

    return {
        "ok": True,
        "lesson_uid": les.lesson_uid,
        "slides_written": written,
        "expected_page_count": les.page_count,
        "warning": warning,
    }
