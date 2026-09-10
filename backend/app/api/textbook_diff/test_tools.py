"""Test 学科专用工具 API（清缓存 / 清库 / 本地 JSON 提交）。"""
from __future__ import annotations

from flask import jsonify, request

from ...services.textbook_diff.sandbox_subjects import require_sandbox_pair_codes
from ...services.textbook_diff.test_persist import (
    flush_test_local_json_to_db,
    test_local_cache_summary,
)
from ...services.textbook_diff.test_reset import (
    clear_test_pair_cache,
    clear_test_pair_db,
)
from . import api_textbook_diff_bp


def _pair_codes_from_request() -> tuple[str, str]:
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or request.args.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or request.args.get("new_code") or "").strip()
    if not old_code or not new_code:
        raise ValueError("缺少 old_code / new_code")
    require_sandbox_pair_codes(old_code, new_code)
    return old_code, new_code


@api_textbook_diff_bp.post("/test/clear-cache")
def test_clear_cache():
    """清空本对 Test 的 OCR/对比/目录缓存（保留课时与粗分）。"""
    try:
        old_code, new_code = _pair_codes_from_request()
        result = clear_test_pair_cache(old_code=old_code, new_code=new_code)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.post("/test/clear-db")
def test_clear_db():
    """清空本对 Test 库内课时/课对/PDF 绑定，保留册次码便于重测上传→粗分。"""
    try:
        old_code, new_code = _pair_codes_from_request()
        body = request.get_json(silent=True) or {}
        if not body.get("confirm"):
            return jsonify({"ok": False, "error": "请传 confirm: true 确认清库"}), 400
        result = clear_test_pair_db(old_code=old_code, new_code=new_code)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.get("/test/local-cache-summary")
def test_local_cache_summary_api():
    """本对本地 JSON 对比缓存概况（不写库）。"""
    try:
        old_code, new_code = _pair_codes_from_request()
        return jsonify(test_local_cache_summary(old_code=old_code, new_code=new_code))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.post("/test/flush-local-to-db")
def test_flush_local_to_db():
    """把本对本地 JSON 对比结果批量写入 MySQL。"""
    try:
        old_code, new_code = _pair_codes_from_request()
        body = request.get_json(silent=True) or {}
        if not body.get("confirm"):
            return jsonify({"ok": False, "error": "请传 confirm: true 确认提交"}), 400
        result = flush_test_local_json_to_db(old_code=old_code, new_code=new_code)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
