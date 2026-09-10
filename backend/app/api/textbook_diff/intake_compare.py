"""Intake 模块「对比」数据接口。"""
from __future__ import annotations

from flask import jsonify, request

from ...services.textbook_diff.intake_compare import (
    build_draft_preview_payload,
    build_intake_compare,
)
from . import api_textbook_diff_bp


@api_textbook_diff_bp.get("/intake-compare")
def get_intake_compare():
    code = request.args.get("code", "").strip()
    kind = request.args.get("kind", "full").strip()
    preview_blob_id = request.args.get("preview_blob_id", "").strip() or None
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    preview_only = request.args.get("preview", "").lower() in ("1", "true", "yes")
    quick = request.args.get("quick", "").lower() in ("1", "true", "yes")

    if not code:
        return jsonify({"ok": False, "error": "缺少 code 参数"}), 400

    try:
        if preview_only:
            if kind != "draft":
                return jsonify({"ok": False, "error": "preview=1 仅适用于 kind=draft"}), 400
            data = build_draft_preview_payload(
                volume_code=code,
                preview_blob_id=preview_blob_id,
                force_preview=force,
            )
            return jsonify({"ok": True, **data})

        include_preview = not (kind == "draft" and quick)
        data = build_intake_compare(
            volume_code=code,
            kind=kind,
            preview_blob_id=preview_blob_id,
            force_preview=force,
            include_preview=include_preview,
        )
        return jsonify({"ok": True, **data})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
