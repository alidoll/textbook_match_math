"""教材对比册次查询与详情。"""
from __future__ import annotations

from ...models import FileBlob, Lesson, LessonPage, Volume
from ...query.lesson_order import order_lessons_query
from ..lesson_filters import filter_master_class_lessons


def get_diff_volume_by_code(volume_code: str) -> Volume:
    code = (volume_code or "").strip()
    if not code:
        raise ValueError("缺少 volume_code")
    volume = Volume.query.filter_by(volume_code=code).first()
    if not volume:
        raise ValueError(f"未找到教材对比册次：{code}")
    return volume


def paired_diff_volume_code(volume_code: str) -> str:
    """同册次配对：…-DOLD ↔ …-DNEW；…-DNEW-vN → …-DOLD。"""
    code = (volume_code or "").strip()
    if code.endswith("-DOLD"):
        return f"{code[:-5]}-DNEW"
    # …-DNEW or …-DNEW-vN
    if "-DNEW-v" in code:
        base, _, _ver = code.rpartition("-v")
        if base.endswith("-DNEW") and _ver.isdigit():
            return f"{base[:-5]}-DOLD"
    if code.endswith("-DNEW"):
        return f"{code[:-5]}-DOLD"
    raise ValueError(f"无法解析教材对比册次编码：{code}")


def resolve_diff_volume_pair(volume_code: str) -> tuple[Volume, Volume]:
    """返回 (旧教材册次, 新教材册次)。"""
    vol = get_diff_volume_by_code(volume_code)
    other = get_diff_volume_by_code(paired_diff_volume_code(vol.volume_code))
    if vol.book_type == "diff_old":
        return vol, other
    if other.book_type != "diff_old":
        raise ValueError(f"配对册次类型异常：{other.volume_code}")
    return other, vol


def diff_volume_summary(volume: Volume) -> dict:
    """对比 hub 用轻量册次信息（不含课时明细）。"""
    from ..volume_pdf import volume_pdf_blob_fields

    lesson_count = Lesson.query.filter_by(volume_id=volume.id).count()
    return {
        "volume_id": volume.id,
        "volume_code": volume.volume_code,
        "display_title": volume.display_title,
        "version_label": getattr(volume, "version_label", None),
        "edition": volume.edition,
        "subject": volume.subject,
        "grade": volume.grade,
        "semester": volume.semester,
        "book_type": volume.book_type,
        "parse_status": volume.parse_status,
        "parse_error": volume.parse_error,
        **volume_pdf_blob_fields(volume),
        "lesson_count": lesson_count,
    }


def diff_volume_detail(volume: Volume) -> dict:
    from ..parse_status_heal import heal_stuck_parse_status

    heal_stuck_parse_status(volume)
    lessons = filter_master_class_lessons(
        order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    )
    from ...parsers.pdf_spread import page_layout_label, stored_page_layout
    from ..volume_pdf import volume_pdf_blob_fields

    pdf_fields = volume_pdf_blob_fields(volume)
    layout = stored_page_layout(volume)
    sug = volume.parse_suggestions_json or {}
    catalog_degraded = bool(sug.get("catalog_degraded"))
    catalog_warnings = list(sug.get("catalog_warnings") or []) if catalog_degraded else []
    from .draft_page_map import draft_preprocess_fields

    draft_fields = draft_preprocess_fields(volume)
    lesson_rows = [
        {
            "lesson_uid": les.lesson_uid,
            "unit_no": les.unit_no,
            "unit_title": les.unit_title,
            "lesson_no": les.lesson_no,
            "lesson_name": les.lesson_name,
            "import_batch": les.import_batch,
            "page_start": les.page_start,
            "page_end": les.page_end,
            "page_range_verified": bool(les.page_range_verified),
            "lesson_page_count": LessonPage.query.filter_by(lesson_id=les.id).count(),
        }
        for les in lessons
    ]
    return {
        "volume_id": volume.id,
        "volume_code": volume.volume_code,
        "display_title": volume.display_title,
        "version_label": getattr(volume, "version_label", None),
        "edition": volume.edition,
        "subject": volume.subject,
        "grade": volume.grade,
        "semester": volume.semester,
        "book_type": volume.book_type,
        "parse_status": volume.parse_status,
        "parse_error": volume.parse_error,
        "page_layout": layout,
        "page_layout_label": page_layout_label(layout) if layout else None,
        "catalog_degraded": catalog_degraded,
        "catalog_warnings": catalog_warnings,
        **pdf_fields,
        **draft_fields,
        "lesson_count": len(lessons),
        "lessons": lesson_rows,
    }
