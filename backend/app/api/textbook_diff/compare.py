"""教材对比 — 对比数据接口。"""
from __future__ import annotations

from flask import jsonify, request

from ...models import Lesson, Volume
from ...query.lesson_order import order_lessons_query
from ...services.lesson_filters import filter_master_class_lessons
from ...services.textbook_diff.atom_compare import build_page_atom_compare
from ...services.textbook_diff.lesson_match import match_diff_lesson_pairs
from ...services.textbook_diff.preview_compare import (
    build_preview_compare_pairs,
    is_draft_preview_volume,
    pdf_page_count,
)
from ...services.textbook_diff.volumes import diff_volume_detail, get_diff_volume_by_code
from . import api_textbook_diff_bp


@api_textbook_diff_bp.get("/compare")
def get_compare():
    old_code = request.args.get("old_code", "").strip()
    new_code = request.args.get("new_code", "").strip()
    force_preview = request.args.get("force_preview", "").lower() in ("1", "true", "yes")

    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "需要 old_code 和 new_code 参数"}), 400

    try:
        old_vol = get_diff_volume_by_code(old_code)
        new_vol = get_diff_volume_by_code(new_code)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404

    # Verify pair: same subject, edition, grade, semester
    if (
        old_vol.subject != new_vol.subject
        or old_vol.edition != new_vol.edition
        or old_vol.grade != new_vol.grade
        or old_vol.semester != new_vol.semester
    ):
        return jsonify(
            {
                "ok": False,
                "error": f"册次不配对：{old_vol.display_title} vs {new_vol.display_title}",
            }
        ), 400

    old_lessons = filter_master_class_lessons(
        order_lessons_query(Lesson.query.filter_by(volume_id=old_vol.id)).all()
    )
    new_lessons = filter_master_class_lessons(
        order_lessons_query(Lesson.query.filter_by(volume_id=new_vol.id)).all()
    )

    old_rows = [
        {
            "lesson_uid": les.lesson_uid,
            "unit_no": les.unit_no,
            "unit_title": les.unit_title,
            "lesson_no": les.lesson_no,
            "lesson_name": les.lesson_name,
            "page_start": les.page_start,
            "page_end": les.page_end,
        }
        for les in old_lessons
    ]
    new_rows = [
        {
            "lesson_uid": les.lesson_uid,
            "unit_no": les.unit_no,
            "unit_title": les.unit_title,
            "lesson_no": les.lesson_no,
            "lesson_name": les.lesson_name,
            "page_start": les.page_start,
            "page_end": les.page_end,
        }
        for les in new_lessons
    ]

    # Align by lesson_no (1-based) — 同册次同学期对齐
    lesson_pairs = match_diff_lesson_pairs(old_vol=old_vol, new_vol=new_vol)
    for lp in lesson_pairs:
        if lp.get("new") and lp.get("old"):
            lp["compare_url"] = (
                f"/textbook-diff/view?old_code={old_code}&new_code={new_code}"
                f"&mode=lesson&new_lesson_uid={lp['new']['lesson_uid']}"
            )

    max_n = max(len(old_rows), len(new_rows))
    pairs = []
    for i in range(max_n):
        pairs.append(
            {
                "index": i + 1,
                "old": old_rows[i] if i < len(old_rows) else None,
                "new": new_rows[i] if i < len(new_rows) else None,
            }
        )

    new_pages = pdf_page_count(new_vol)
    draft_preview = is_draft_preview_volume(new_vol, lesson_count=len(new_rows))
    use_preview = draft_preview or force_preview

    payload: dict = {
        "ok": True,
        "mode": "preview" if use_preview else "lessons",
        "draft_preview": draft_preview,
        "new_pdf_pages": new_pages,
        "old_volume": diff_volume_detail(old_vol),
        "new_volume": diff_volume_detail(new_vol),
        "pairs": pairs,
        "lesson_pairs": lesson_pairs,
        "old_count": len(old_rows),
        "new_count": len(new_rows),
    }

    if use_preview:
        try:
            preview = build_preview_compare_pairs(
                old_vol=old_vol,
                new_vol=new_vol,
                force_refresh=force_preview,
            )
            payload["preview_pairs"] = preview["pairs"]
            payload["preview_summary"] = preview.get("summary")
            payload["preview_meta"] = {
                "comparable_count": preview["comparable_count"],
                "old_offset": preview["old_offset"],
                "new_pdf_pages": preview["new_pdf_pages"],
            }
            if draft_preview:
                pages_note = new_pages or preview.get("new_pdf_pages") or "?"
                payload["hint"] = (
                    f"新教材 PDF 共 {pages_note} 页（出版社未定稿预览）。"
                    "下方列出每页最接近的旧书页/课时，点右侧「对比」进入左旧右新工作台（OCR + 锚定）。"
                )
        except ValueError as exc:
            payload["preview_error"] = str(exc)
            if draft_preview:
                payload["hint"] = str(exc)

    return jsonify(payload)


@api_textbook_diff_bp.get("/compare/page-atoms")
def get_compare_page_atoms():
    """单页原子 OCR + 锚定 + 文本差异（预览页对照）。"""
    old_code = request.args.get("old_code", "").strip()
    new_code = request.args.get("new_code", "").strip()
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    try:
        old_page = int(request.args.get("old_page", "0"))
        new_page = int(request.args.get("new_page", "0"))
    except ValueError:
        return jsonify({"ok": False, "error": "需要有效的 old_page / new_page"}), 400

    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "需要 old_code 和 new_code"}), 400
    if old_page < 1 or new_page < 1:
        return jsonify({"ok": False, "error": "页码无效"}), 400

    try:
        old_vol = get_diff_volume_by_code(old_code)
        new_vol = get_diff_volume_by_code(new_code)
        data = build_page_atom_compare(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            force_refresh=force,
        )
        return jsonify({"ok": True, **data})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"原子 OCR 失败：{exc}"}), 500
