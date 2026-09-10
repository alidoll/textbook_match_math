"""教材对比工作台 API。"""
from __future__ import annotations

import logging
from io import BytesIO

from flask import jsonify, request, send_file

from ...services.textbook_diff.atom_compare import (
    compare_cached_page_text,
    run_diff_page_ocr,
    run_diff_page_ocr_both,
)
from ...services.textbook_diff.compare_export import build_compare_export_xlsx
from ...services.textbook_diff.page_image_compare import compare_cached_page_images
from ...services.textbook_diff.view_workspace import (
    build_lesson_view_workspace,
    build_page_view_workspace,
)
from ...services.textbook_diff.volumes import get_diff_volume_by_code
from . import api_textbook_diff_bp

_log = logging.getLogger(__name__)


def _ocr_old_vol(old_vol):
    """小科 DOLD 无 PDF：OCR/比对/缓存键改用旧库册。"""
    from ...services.textbook_diff.xiaoke_view import (
        is_xiaoke_volume,
        resolve_xiaoke_ocr_old_volume,
    )

    if is_xiaoke_volume(old_vol):
        return resolve_xiaoke_ocr_old_volume(old_vol)
    return old_vol


@api_textbook_diff_bp.get("/view/workspace")
def get_view_workspace():
    old_code = request.args.get("old_code", "").strip()
    new_code = request.args.get("new_code", "").strip()
    mode = request.args.get("mode", "page").strip()

    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "需要 old_code 和 new_code"}), 400

    try:
        if mode == "lesson":
            uid = request.args.get("new_lesson_uid", "").strip()
            if not uid:
                return jsonify({"ok": False, "error": "需要 new_lesson_uid"}), 400
            page_index = int(request.args.get("page_index", "1") or "1")
            data = build_lesson_view_workspace(
                old_code=old_code,
                new_code=new_code,
                new_lesson_uid=uid,
                page_index=page_index,
            )
        else:
            old_page = int(request.args.get("old_page", "0"))
            new_page = int(request.args.get("new_page", "0"))
            if old_page < 1 or new_page < 1:
                return jsonify({"ok": False, "error": "需要有效的 old_page / new_page"}), 400
            data = build_page_view_workspace(
                old_code=old_code,
                new_code=new_code,
                old_page=old_page,
                new_page=new_page,
                new_pdf_source=request.args.get("new_pdf_source", "full").strip() or "full",
                preview_blob_id=request.args.get("preview_blob_id", "").strip() or None,
                include_atoms=request.args.get("include_atoms", "1").lower() not in ("0", "false", "no"),
            )
        if data.get("atoms"):
            s = data["atoms"].get("summary") or {}
            _log.info(
                "view/workspace atoms old=%s new=%s p%d↔p%d old_n=%s new_n=%s",
                old_code,
                new_code,
                data.get("old_page"),
                data.get("new_page"),
                s.get("old_atom_count"),
                s.get("new_atom_count"),
            )
        return jsonify({"ok": True, **data})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"加载失败：{exc}"}), 500


@api_textbook_diff_bp.post("/view/ocr")
def run_view_page_ocr():
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    old_page = int(body.get("old_page") or 0)
    new_page = int(body.get("new_page") or 0)
    phase = str(body.get("phase") or body.get("ocr_phase") or "").strip()
    side = str(body.get("side") or "both").strip() or "both"
    if not old_code or not new_code or old_page < 1 or new_page < 1:
        return jsonify({"ok": False, "error": "参数不完整"}), 400
    if phase not in ("text", "images"):
        return jsonify({"ok": False, "error": "phase 须为 text 或 images"}), 400
    if side not in ("old", "new", "both"):
        return jsonify({"ok": False, "error": "side 须为 old、new 或 both"}), 400
    new_pdf_source = str(body.get("new_pdf_source") or "full").strip() or "full"
    preview_blob_id = str(body.get("preview_blob_id") or "").strip() or None
    try:
        old_vol = _ocr_old_vol(get_diff_volume_by_code(old_code))
        new_vol = get_diff_volume_by_code(new_code)
        _log.info(
            "view/ocr start side=%s phase=%s old=%s(%s) new=%s p%d↔p%d source=%s",
            side,
            phase,
            old_code,
            old_vol.volume_code,
            new_code,
            old_page,
            new_page,
            new_pdf_source,
        )
        if side == "both":
            atoms = run_diff_page_ocr_both(
                old_vol=old_vol,
                new_vol=new_vol,
                old_page=old_page,
                new_page=new_page,
                ocr_phase=phase,
                new_pdf_source=new_pdf_source,
                preview_blob_id=preview_blob_id,
            )
        else:
            atoms = run_diff_page_ocr(
                old_vol=old_vol,
                new_vol=new_vol,
                old_page=old_page,
                new_page=new_page,
                side=side,
                ocr_phase=phase,
                new_pdf_source=new_pdf_source,
                preview_blob_id=preview_blob_id,
            )
        s = atoms.get("summary") or {}
        _log.info(
            "view/ocr done side=%s phase=%s old_atoms=%s new_atoms=%s",
            side,
            phase,
            s.get("old_atom_count"),
            s.get("new_atom_count"),
        )
        return jsonify({"ok": True, "atoms": atoms})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.post("/view/text-compare")
def run_view_text_compare():
    """② 文字比对：基于两侧文字 OCR 做正文/标点差异（语文优先）。"""
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    old_page = int(body.get("old_page") or 0)
    new_page = int(body.get("new_page") or 0)
    if not old_code or not new_code or old_page < 1 or new_page < 1:
        return jsonify({"ok": False, "error": "参数不完整"}), 400
    new_pdf_source = str(body.get("new_pdf_source") or "full").strip() or "full"
    preview_blob_id = str(body.get("preview_blob_id") or "").strip() or None
    force = bool(body.get("force"))
    try:
        old_vol = _ocr_old_vol(get_diff_volume_by_code(old_code))
        new_vol = get_diff_volume_by_code(new_code)
        result = compare_cached_page_text(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            force=force,
        )
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.post("/view/image-compare")
def run_view_image_compare():
    """④ 图片比对：版面(A) + 说明(C) + 画面相似(B)。"""
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    old_page = int(body.get("old_page") or 0)
    new_page = int(body.get("new_page") or 0)
    if not old_code or not new_code or old_page < 1 or new_page < 1:
        return jsonify({"ok": False, "error": "参数不完整"}), 400
    new_pdf_source = str(body.get("new_pdf_source") or "full").strip() or "full"
    preview_blob_id = str(body.get("preview_blob_id") or "").strip() or None
    try:
        old_vol = _ocr_old_vol(get_diff_volume_by_code(old_code))
        new_vol = get_diff_volume_by_code(new_code)
        result = compare_cached_page_images(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
        )
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.get("/view/compare-export")
@api_textbook_diff_bp.get("/view/compare-export.xlsx")
def get_view_compare_export_xlsx():
    """下载当前页文字比对 + 图片比对为双工作表 Excel。"""
    old_code = str(request.args.get("old_code") or "").strip()
    new_code = str(request.args.get("new_code") or "").strip()
    old_page = int(request.args.get("old_page") or 0)
    new_page = int(request.args.get("new_page") or 0)
    if not old_code or not new_code or old_page < 1 or new_page < 1:
        return jsonify({"ok": False, "error": "参数不完整"}), 400
    new_pdf_source = str(request.args.get("new_pdf_source") or "full").strip() or "full"
    preview_blob_id = str(request.args.get("preview_blob_id") or "").strip() or None
    try:
        old_vol = _ocr_old_vol(get_diff_volume_by_code(old_code))
        new_vol = get_diff_volume_by_code(new_code)
        raw, download_name = build_compare_export_xlsx(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
        )
        return send_file(
            BytesIO(raw),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=download_name,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        _log.exception("compare-export failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
