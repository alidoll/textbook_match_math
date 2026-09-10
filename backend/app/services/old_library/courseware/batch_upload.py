"""批量上传课件 ZIP。"""
from __future__ import annotations

from typing import Any

from ....extensions import db
from ..volumes import get_old_volume_by_code, volume_detail_dict
from .batch_match import assign_courseware_filenames
from .upload import upload_courseware_zip


def preview_courseware_batch(
    *,
    volume_code: str,
    filenames: list[str],
    min_score: float = 0.62,
) -> dict[str, Any]:
    volume = get_old_volume_by_code(volume_code)
    detail = volume_detail_dict(volume)
    lessons = detail.get("lessons") or []
    names = [str(n).strip() for n in filenames if str(n).strip()]
    if not names:
        raise ValueError("请提供至少一个 ZIP 文件名")

    plan = assign_courseware_filenames(names, lessons, min_score=min_score)
    return {
        "ok": True,
        "volume_code": volume_code,
        "file_count": len(names),
        **plan,
    }


def batch_upload_courseware_zips(
    *,
    volume_code: str,
    files: list[tuple[str, bytes]],
    mapping: list[dict[str, str]] | None = None,
    min_score: float = 0.62,
) -> dict[str, Any]:
    """
    files: [(filename, content), ...]
    mapping: 可选 [{"filename": "...", "lesson_uid": "..."}]；缺省则自动匹配。
    """
    if not files:
        raise ValueError("请选择 ZIP 文件")

    volume = get_old_volume_by_code(volume_code)
    detail = volume_detail_dict(volume)
    lessons = detail.get("lessons") or []
    lesson_by_uid = {les["lesson_uid"]: les for les in lessons}

    filename_to_uid: dict[str, str] = {}
    if mapping:
        for row in mapping:
            fn = str(row.get("filename") or "").strip()
            uid = str(row.get("lesson_uid") or "").strip()
            if fn and uid:
                filename_to_uid[fn] = uid
    else:
        plan = assign_courseware_filenames(
            [fn for fn, _ in files],
            lessons,
            min_score=min_score,
        )
        for row in plan["matches"]:
            filename_to_uid[row["filename"]] = row["lesson_uid"]

    uploaded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        for filename, content in files:
            uid = filename_to_uid.get(filename)
            if not uid:
                skipped.append({"filename": filename, "reason": "未匹配到课时"})
                continue
            if uid not in lesson_by_uid:
                skipped.append({"filename": filename, "reason": f"无效课时 {uid}"})
                continue
            try:
                result = upload_courseware_zip(
                    lesson_uid=uid,
                    content=content,
                    filename=filename,
                    commit=False,
                )
                uploaded.append(
                    {
                        "filename": filename,
                        "lesson_uid": uid,
                        "lesson_name": lesson_by_uid[uid]["lesson_name"],
                        "slides_written": result.get("slides_written"),
                        "warning": result.get("warning"),
                    }
                )
            except ValueError as exc:
                errors.append({"filename": filename, "lesson_uid": uid, "error": str(exc)})
        if errors and not uploaded:
            db.session.rollback()
        elif uploaded:
            db.session.commit()
        else:
            db.session.rollback()
    except Exception:
        db.session.rollback()
        raise

    return {
        "ok": True,
        "volume_code": volume_code,
        "uploaded_count": len(uploaded),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "uploaded": uploaded,
        "skipped": skipped,
        "errors": errors,
    }
