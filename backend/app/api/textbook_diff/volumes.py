"""教材对比 — 册次 API。"""
from __future__ import annotations

from flask import jsonify, request

from ...services.textbook_diff.volume_create import (
    ensure_diff_volume_by_code,
    subject_label_from_param,
)
from ...services.textbook_diff.volumes import diff_volume_detail, get_diff_volume_by_code
from ...models import Volume
from . import api_textbook_diff_bp


@api_textbook_diff_bp.post("/volumes/ensure")
def ensure_volume():
    body = request.get_json(silent=True) or {}
    volume_code = body.get("volume_code", "").strip()
    if not volume_code:
        return jsonify({"ok": False, "error": "缺少 volume_code"}), 400
    try:
        result = ensure_diff_volume_by_code(volume_code)
        return jsonify(result)
    except (KeyError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.get("/volumes/<volume_code>")
def get_volume(volume_code: str):
    try:
        volume = get_diff_volume_by_code(volume_code)
        return jsonify({"ok": True, **diff_volume_detail(volume)})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


@api_textbook_diff_bp.get("/volumes")
def list_volumes():
    q = Volume.query.filter(Volume.book_type.in_(["diff_old", "diff_new"]))
    raw = (request.args.get("subject") or "").strip()
    subject = subject_label_from_param(raw) if raw else None
    # 指定了学科但无法解析 → 空列表，避免串出其它学科
    if raw and not subject:
        return jsonify({"ok": True, "subject": None, "volumes": []})
    if subject:
        q = q.filter(Volume.subject == subject)
    rows = q.order_by(Volume.subject, Volume.grade, Volume.semester, Volume.book_type).all()
    return jsonify(
        {
            "ok": True,
            "subject": subject,
            "volumes": [diff_volume_detail(v) for v in rows],
        }
    )
