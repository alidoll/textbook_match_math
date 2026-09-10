"""教材对比 — 本册建设 / 粗分 API。"""
from __future__ import annotations

from flask import jsonify, request

from ...services.textbook_diff.workbook import (
    build_editions_payload,
    build_xiaoke_editions_payload,
    confirm_all_pairs,
    confirm_pair_by_new_lesson,
    create_workbook_pair,
    ensure_workbook_pair,
    ensure_xiaoke_workbook_pair,
    list_workbook_pairs,
    mark_pair_ready_for_review,
    run_coarse_match,
    update_coarse_pair,
    workbook_pair_detail,
)
from ...services.textbook_diff.shelf import (
    add_shelf_upload,
    list_compare_archives,
    list_shelf,
    open_compare_session,
    rename_shelf_item,
    shelf_enabled_for_subject,
)
from ...services.textbook_diff.lesson_pipeline import (
    cancel_pipeline,
    get_pipeline_job,
    start_pipeline,
)
from . import api_textbook_diff_bp


@api_textbook_diff_bp.get("/workbook/pairs")
def workbook_list_pairs():
    subject = request.args.get("subject")
    return jsonify({"ok": True, "pairs": list_workbook_pairs(subject=subject)})


@api_textbook_diff_bp.get("/workbook/shelf")
def workbook_shelf_list():
    prefix = (request.args.get("prefix") or "HXRJ").strip().upper()
    grade_raw = request.args.get("grade")
    term = request.args.get("term") or "上"
    try:
        grade = int(grade_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少或无效 grade"}), 400
    try:
        items = list_shelf(prefix=prefix, grade=grade, term=term)
        archives = list_compare_archives(
            prefix=prefix, grade=grade, term=term, items=items
        )
        return jsonify(
            {
                "ok": True,
                "items": items,
                "archives": archives,
                "shelf_enabled": True,
            }
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.post("/workbook/shelf/items")
def workbook_shelf_add():
    body = request.get_json(silent=True) or {}
    prefix = (body.get("prefix") or "HXRJ").strip().upper()
    term = body.get("term") or "上"
    role = body.get("role") or "new"
    version_label = body.get("version_label")
    try:
        grade = int(body.get("grade"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "缺少或无效 grade"}), 400
    try:
        out = add_shelf_upload(
            prefix=prefix,
            grade=grade,
            term=term,
            role=role,
            version_label=version_label,
        )
        return jsonify(out)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.patch("/workbook/shelf/items/<volume_code>")
def workbook_shelf_rename(volume_code: str):
    body = request.get_json(silent=True) or {}
    try:
        out = rename_shelf_item(
            volume_code=volume_code,
            version_label=body.get("version_label") or "",
        )
        return jsonify(out)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.post("/workbook/compare-session")
def workbook_compare_session():
    body = request.get_json(silent=True) or {}
    old_code = (body.get("old_code") or "").strip()
    new_code = (body.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code 或 new_code"}), 400
    try:
        out = open_compare_session(old_code=old_code, new_code=new_code)
        return jsonify(out)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.get("/workbook/shelf-enabled")
def workbook_shelf_enabled():
    subject = request.args.get("subject")
    return jsonify({"ok": True, "enabled": shelf_enabled_for_subject(subject)})


@api_textbook_diff_bp.get("/workbook/xiaoke/editions")
def workbook_xiaoke_editions():
    try:
        return jsonify({"ok": True, "editions": build_xiaoke_editions_payload()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.get("/workbook/editions")
def workbook_editions():
    """通用本册入口：按学科列出版本 × 册次 + 各册预处理状态（数学等非小科学科）。"""
    from ...services.textbook_diff.volume_create import subject_label_from_param

    raw = (request.args.get("subject") or "").strip()
    label = subject_label_from_param(raw) or raw
    registry_subject = "科学" if label == "小科" else label
    if not registry_subject:
        return jsonify({"ok": False, "error": "缺少 subject"}), 400
    try:
        return jsonify({"ok": True, "editions": build_editions_payload(registry_subject)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.post("/workbook/ensure")
def workbook_ensure():
    """按需新建本册旧+新两行 volumes（通用版，不校验旧库基准）。"""
    body = request.get_json(silent=True) or {}
    try:
        result = ensure_workbook_pair(
            subject=str(body.get("subject") or ""),
            edition_id=str(body.get("edition_id") or body.get("edition") or ""),
            grade=int(body.get("grade") or 0),
            term=str(body.get("term") or body.get("semester") or ""),
        )
        return jsonify(result)
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.get("/workbook/xiaoke/edition-catalogs")
def workbook_xiaoke_edition_catalogs():
    """同社全年级旧库目录（只读），供本册页跨年级对照。"""
    old_code = (request.args.get("old_code") or "").strip()
    if not old_code:
        return jsonify({"ok": False, "error": "缺少 old_code"}), 400
    try:
        from ...services.textbook_diff.volumes import get_diff_volume_by_code
        from ...services.textbook_diff.xiaoke_old_catalog import (
            old_library_edition_catalogs,
        )

        old_vol = get_diff_volume_by_code(old_code)
        if (old_vol.subject or "").strip() != "小科" and not str(
            old_vol.volume_code or ""
        ).upper().startswith("XK"):
            return jsonify({"ok": False, "error": "仅小科册次支持同社全年级目录"}), 400
        payload = old_library_edition_catalogs(old_vol)
        return jsonify({"ok": True, **payload})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.post("/workbook/xiaoke/ensure")
def workbook_xiaoke_ensure():
    body = request.get_json(silent=True) or {}
    try:
        result = ensure_xiaoke_workbook_pair(
            edition_id=str(body.get("edition_id") or body.get("edition") or ""),
            grade=int(body.get("grade") or 0),
            term=str(body.get("term") or body.get("semester") or ""),
        )
        return jsonify(result)
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.post("/workbook/xiaoke/recognize-catalog")
def workbook_xiaoke_recognize_catalog():
    """小科专用：新侧目录划分 + 新旧目录粗分（不跑页码/页图/整册对比）。"""
    from ...extensions import db
    from ...services.textbook_diff.xiaoke_recognize import (
        recognize_xiaoke_catalog_and_compare,
    )

    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    try:
        result = recognize_xiaoke_catalog_and_compare(
            old_code=old_code,
            new_code=new_code,
            replace=bool(body.get("replace", True)),
            compare=bool(body.get("compare", True)),
        )
        return jsonify(result)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"小科目录识别失败：{exc}"}), 500


@api_textbook_diff_bp.post("/workbook/xiaoke/parse-pages")
def workbook_xiaoke_parse_pages():
    """小科专用：新侧划分页码（后台异步，保留已识别目录）。"""
    import threading

    from flask import current_app

    from ...extensions import db
    from ...services.textbook_diff.xiaoke_parse import (
        prepare_xiaoke_new_page_parse,
        run_xiaoke_new_page_parse,
    )

    body = request.get_json(silent=True) or {}
    new_code = str(body.get("new_code") or "").strip()
    force_recalibrate = bool(body.get("force_recalibrate", False))
    if not new_code:
        return jsonify({"ok": False, "error": "缺少 new_code"}), 400
    try:
        prepared = prepare_xiaoke_new_page_parse(
            new_code=new_code,
            force_recalibrate=force_recalibrate,
        )
        if not prepared.get("start_worker"):
            status = 200 if prepared.get("parse_status") == "done" else 202
            return jsonify(prepared), status

        app = current_app._get_current_object()

        def worker() -> None:
            with app.app_context():
                run_xiaoke_new_page_parse(
                    new_code=new_code,
                    force_recalibrate=force_recalibrate,
                )

        threading.Thread(
            target=worker, daemon=True, name=f"xiaoke-parse-{new_code}"
        ).start()
        return jsonify(prepared), 202
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"小科划分页码失败：{exc}"}), 500


@api_textbook_diff_bp.post("/workbook/xiaoke/ocr-new-lesson")
def workbook_xiaoke_ocr_new_lesson():
    """小科专用：当前课对新侧全部页 OCR（文字→图片），返回逐页原子数据。"""
    from ...extensions import db
    from ...services.textbook_diff.xiaoke_ocr import ocr_xiaoke_new_lesson_pages

    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    new_lesson_uid = str(body.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    phases_raw = body.get("phases")
    phases = None
    if isinstance(phases_raw, list) and phases_raw:
        phases = [str(p).strip() for p in phases_raw if str(p).strip()]
    try:
        result = ocr_xiaoke_new_lesson_pages(
            old_code=old_code,
            new_code=new_code,
            new_lesson_uid=new_lesson_uid,
            phases=phases,
            skip_cached=bool(body.get("skip_cached", True)),
            include_atoms=bool(body.get("include_atoms", True)),
        )
        status = 200 if result.get("ok") else 207
        return jsonify(result), status
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"小科新侧整课 OCR 失败：{exc}"}), 500


@api_textbook_diff_bp.post("/workbook/xiaoke/lesson-full")
def workbook_xiaoke_lesson_full():
    """小科：旧侧全页 OCR → 新侧全页 OCR → 按课内页序比对（后台任务）。"""
    from ...extensions import db
    from ...services.textbook_diff.xiaoke_ocr import start_xiaoke_lesson_full

    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    new_lesson_uid = str(body.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    try:
        job = start_xiaoke_lesson_full(
            old_code=old_code,
            new_code=new_code,
            new_lesson_uid=new_lesson_uid,
            skip_cached=bool(body.get("skip_cached", True)),
        )
        return jsonify({"ok": True, "job": job})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"小科整课一键启动失败：{exc}"}), 500


@api_textbook_diff_bp.post("/workbook/xiaoke/volume-full")
def workbook_xiaoke_volume_full():
    """小科：对粗分已匹配的全部课时依次整课一键（含环节整理）。"""
    from ...extensions import db
    from ...services.textbook_diff.xiaoke_ocr import (
        cancel_xiaoke_volume_full,
        start_xiaoke_volume_full,
    )

    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    if body.get("cancel") or str(body.get("action") or "").strip() == "cancel":
        return jsonify(cancel_xiaoke_volume_full(old_code=old_code, new_code=new_code))
    uids = body.get("new_lesson_uids")
    if uids is not None and not isinstance(uids, list):
        return jsonify({"ok": False, "error": "new_lesson_uids 须为数组"}), 400
    try:
        job = start_xiaoke_volume_full(
            old_code=old_code,
            new_code=new_code,
            skip_cached=bool(body.get("skip_cached", True)),
            new_lesson_uids=[str(u) for u in (uids or [])] or None,
        )
        return jsonify({"ok": True, "job": job})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"小科整册一键启动失败：{exc}"}), 500


@api_textbook_diff_bp.get("/workbook/xiaoke/volume-full/status")
def workbook_xiaoke_volume_full_status():
    """轮询小科整册一键进度。"""
    from ...services.textbook_diff.xiaoke_ocr import get_xiaoke_volume_full_status

    old_code = str(request.args.get("old_code") or "").strip()
    new_code = str(request.args.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    job = get_xiaoke_volume_full_status(old_code=old_code, new_code=new_code)
    return jsonify({"ok": True, "job": job})


@api_textbook_diff_bp.post("/workbook/xiaoke/volume-full/cancel")
def workbook_xiaoke_volume_full_cancel():
    """停止小科整册一键。"""
    from ...services.textbook_diff.xiaoke_ocr import cancel_xiaoke_volume_full

    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or request.args.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or request.args.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    return jsonify(cancel_xiaoke_volume_full(old_code=old_code, new_code=new_code))


@api_textbook_diff_bp.post("/workbook/xiaoke/textbook-block-trial")
def workbook_xiaoke_textbook_block_trial():
    """小科：无课件教材轨试验。

    默认：复用对比页文字+图片 OCR → 图文共建块 → 单块对照（不重跑 OCR）。
    """
    from ...extensions import db
    from ...services.textbook_diff.xiaoke_textbook_block_trial import (
        DEFAULT_TRIAL_STEPS,
        start_textbook_block_trial,
    )

    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    new_lesson_uid = str(body.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    steps = body.get("steps")
    if steps is None:
        steps = list(DEFAULT_TRIAL_STEPS)
    elif not isinstance(steps, list):
        return jsonify({"ok": False, "error": "steps 须为数组"}), 400
    try:
        job = start_textbook_block_trial(
            old_code=old_code,
            new_code=new_code,
            new_lesson_uid=new_lesson_uid,
            skip_cached=bool(body.get("skip_cached", True)),
            skip_curate=bool(body.get("skip_curate", True)),
            steps=steps,
        )
        return jsonify({"ok": True, "job": job})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"教材轨试验启动失败：{exc}"}), 500


@api_textbook_diff_bp.get("/workbook/xiaoke/textbook-block-trial/status")
def workbook_xiaoke_textbook_block_trial_status():
    """轮询教材轨试验进度。"""
    from ...services.textbook_diff.xiaoke_textbook_block_trial import (
        get_textbook_block_trial_status,
    )

    old_code = str(request.args.get("old_code") or "").strip()
    new_code = str(request.args.get("new_code") or "").strip()
    new_lesson_uid = str(request.args.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    job = get_textbook_block_trial_status(
        old_code=old_code,
        new_code=new_code,
        new_lesson_uid=new_lesson_uid,
    )
    return jsonify({"ok": True, "job": job})


@api_textbook_diff_bp.get("/workbook/xiaoke/section-block-compare")
def workbook_xiaoke_section_block_compare():
    """小科：栏目块（图文共建）对照，供对比页右侧变化说明表格。"""
    from ...extensions import db
    from ...services.textbook_diff.xiaoke_section_block_compare import (
        build_section_block_compare,
    )

    old_code = str(request.args.get("old_code") or "").strip()
    new_code = str(request.args.get("new_code") or "").strip()
    new_lesson_uid = str(request.args.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    try:
        result = build_section_block_compare(
            old_code=old_code,
            new_code=new_code,
            new_lesson_uid=new_lesson_uid,
        )
        return jsonify({"ok": True, "result": result})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"栏目块对照失败：{exc}"}), 500


@api_textbook_diff_bp.get("/workbook/xiaoke/lesson-full/status")
def workbook_xiaoke_lesson_full_status():
    """轮询小科整课一键进度。"""
    from ...services.textbook_diff.xiaoke_ocr import get_xiaoke_lesson_full_status

    old_code = str(request.args.get("old_code") or "").strip()
    new_code = str(request.args.get("new_code") or "").strip()
    new_lesson_uid = str(request.args.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    job = get_xiaoke_lesson_full_status(
        old_code=old_code,
        new_code=new_code,
        new_lesson_uid=new_lesson_uid,
    )
    return jsonify({"ok": True, "job": job})


@api_textbook_diff_bp.get("/workbook/xiaoke/lesson-full/result")
def workbook_xiaoke_lesson_full_result():
    """读取小科整课合成比对完整结果。"""
    from ...services.textbook_diff.xiaoke_ocr import get_xiaoke_lesson_full_result

    old_code = str(request.args.get("old_code") or "").strip()
    new_code = str(request.args.get("new_code") or "").strip()
    new_lesson_uid = str(request.args.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    try:
        result = get_xiaoke_lesson_full_result(
            old_code=old_code,
            new_code=new_code,
            new_lesson_uid=new_lesson_uid,
        )
        if not result:
            return jsonify({"ok": False, "error": "尚无整课比对结果，请先跑整课一键"}), 404
        return jsonify({"ok": True, "result": result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.post("/workbook/xiaoke/phase-segment")
def workbook_xiaoke_phase_segment():
    """小科：划分教学环节。缺文字时只补文字 OCR，不跑图片识别和块比对。"""
    from ...extensions import db
    from ...services.textbook_diff.xiaoke_ocr import (
        run_xiaoke_lesson_phase_segment,
        start_xiaoke_lesson_full,
        xiaoke_lesson_needs_text_ocr,
    )

    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    new_lesson_uid = str(body.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    try:
        if xiaoke_lesson_needs_text_ocr(
            old_code=old_code,
            new_code=new_code,
            new_lesson_uid=new_lesson_uid,
        ):
            job = start_xiaoke_lesson_full(
                old_code=old_code,
                new_code=new_code,
                new_lesson_uid=new_lesson_uid,
                skip_cached=True,
                purpose="phase",
            )
            return jsonify({
                "ok": True,
                "started": True,
                "job": job,
                "message": "正在文字识别后划分环节（不跑图片 OCR / 文字块比对）…",
            })
        result = run_xiaoke_lesson_phase_segment(
            old_code=old_code,
            new_code=new_code,
            new_lesson_uid=new_lesson_uid,
            force=bool(body.get("force", True)),
        )
        return jsonify(result)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"环节划分失败：{exc}"}), 500


@api_textbook_diff_bp.post("/workbook/pairs")
def workbook_create_pair():
    body = request.get_json(silent=True) or {}
    try:
        result = create_workbook_pair(
            subject=str(body.get("subject") or ""),
            edition=str(body.get("edition") or "人教版"),
            grade=int(body.get("grade") or 0),
            term=str(body.get("term") or body.get("semester") or ""),
        )
        return jsonify(result)
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.get("/workbook/pair")
def workbook_get_pair():
    old_code = (request.args.get("old_code") or "").strip()
    new_code = (request.args.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    try:
        return jsonify(workbook_pair_detail(old_code=old_code, new_code=new_code))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.post("/workbook/coarse-match")
def workbook_coarse_match():
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    try:
        result = run_coarse_match(
            old_code=old_code,
            new_code=new_code,
            replace=bool(body.get("replace", True)),
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.patch("/workbook/coarse-pairs/<pair_id>")
def workbook_patch_coarse_pair(pair_id: str):
    body = request.get_json(silent=True) or {}
    try:
        result = update_coarse_pair(
            pair_id=pair_id,
            old_lesson_uid=body.get("old_lesson_uid"),
            clear_old=bool(body.get("clear_old", False)),
            pair_status=body.get("pair_status"),
            change_advice=body.get("change_advice") if "change_advice" in body else None,
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.post("/workbook/coarse-confirm-all")
def workbook_confirm_all():
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    try:
        return jsonify(confirm_all_pairs(old_code=old_code, new_code=new_code))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.post("/workbook/coarse-confirm-lesson")
def workbook_confirm_lesson():
    """对比页：教研看完结果后手工确认课对。"""
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    new_lesson_uid = str(body.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    try:
        return jsonify(
            confirm_pair_by_new_lesson(
                old_code=old_code,
                new_code=new_code,
                new_lesson_uid=new_lesson_uid,
            )
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.post("/workbook/coarse-ready-review")
def workbook_mark_ready_review():
    """比对跑完后标记为待确认（不覆盖已确认）。"""
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    new_lesson_uid = str(body.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    try:
        return jsonify(
            mark_pair_ready_for_review(
                old_code=old_code,
                new_code=new_code,
                new_lesson_uid=new_lesson_uid,
            )
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.post("/workbook/pipeline/start")
def workbook_pipeline_start():
    """本课时 / 整册一键对比：后台串行跑各页 ①→④。"""
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    scope = str(body.get("scope") or "lesson").strip() or "lesson"
    new_lesson_uid = str(body.get("new_lesson_uid") or "").strip() or None
    raw_uids = body.get("new_lesson_uids") or body.get("only_new_lesson_uids") or []
    only_new_lesson_uids = None
    if isinstance(raw_uids, list):
        only_new_lesson_uids = [str(u).strip() for u in raw_uids if str(u).strip()] or None
    try:
        job = start_pipeline(
            old_code=old_code,
            new_code=new_code,
            scope=scope,  # type: ignore[arg-type]
            new_lesson_uid=new_lesson_uid,
            only_new_lesson_uids=only_new_lesson_uids,
        )
        return jsonify({"ok": True, "job": job})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.post("/workbook/pipeline/cancel")
def workbook_pipeline_cancel():
    """取消本册进行中的一键对比（整册 / 本课时）。"""
    body = request.get_json(silent=True) or {}
    old_code = str(body.get("old_code") or "").strip()
    new_code = str(body.get("new_code") or "").strip()
    scope = str(body.get("scope") or "").strip() or None
    new_lesson_uid = str(body.get("new_lesson_uid") or "").strip() or None
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    try:
        result = cancel_pipeline(
            old_code=old_code,
            new_code=new_code,
            scope=scope,  # type: ignore[arg-type]
            new_lesson_uid=new_lesson_uid,
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.get("/workbook/pipeline/status")
def workbook_pipeline_status():
    old_code = (request.args.get("old_code") or "").strip()
    new_code = (request.args.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    from ...services.textbook_diff.lesson_pipeline import get_pipeline_snapshot

    snap = get_pipeline_snapshot(old_code=old_code, new_code=new_code)
    return jsonify({"ok": True, "job": snap, "snapshot": snap})


@api_textbook_diff_bp.get("/workbook/export-xlsx")
def workbook_export_xlsx():
    """下载整册对比结果 Excel：总表 + 单元表 + 附录（识字表/写字表/词语表分 sheet）。"""
    from io import BytesIO

    from flask import send_file

    from ...services.textbook_diff.workbook_export import build_workbook_volume_export_xlsx

    old_code = (request.args.get("old_code") or "").strip()
    new_code = (request.args.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    try:
        raw, download_name = build_workbook_volume_export_xlsx(
            old_code=old_code,
            new_code=new_code,
        )
        return send_file(
            BytesIO(raw),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=download_name,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.get("/workbook/export-lesson-compare")
def workbook_export_lesson_compare():
    """下载单课对比结果 Excel：该课全部页的文字+图片比对。"""
    from io import BytesIO

    from flask import send_file

    from ...services.textbook_diff.workbook_export import build_lesson_compare_export_xlsx

    old_code = (request.args.get("old_code") or "").strip()
    new_code = (request.args.get("new_code") or "").strip()
    new_lesson_uid = (request.args.get("new_lesson_uid") or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code / new_lesson_uid"}), 400
    try:
        raw, download_name = build_lesson_compare_export_xlsx(
            old_code=old_code,
            new_code=new_code,
            new_lesson_uid=new_lesson_uid,
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
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.get("/workbook/export-coarse-xlsx")
def workbook_export_coarse_xlsx():
    """下载课时粗分结果 Excel（出版社一次 + 新旧年级/册次/单元课时，匹配方式居中）。"""
    from io import BytesIO

    from flask import send_file

    from ...services.textbook_diff.coarse_export import build_coarse_match_export_xlsx

    old_code = (request.args.get("old_code") or "").strip()
    new_code = (request.args.get("new_code") or "").strip()
    if not old_code or not new_code:
        return jsonify({"ok": False, "error": "缺少 old_code / new_code"}), 400
    try:
        raw, download_name = build_coarse_match_export_xlsx(
            old_code=old_code,
            new_code=new_code,
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
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.get("/workbook/xiaoke/editions/<edition_id>/export-coarse-targets")
def workbook_xiaoke_export_coarse_targets(edition_id: str):
    """小科版本页：列出各册粗分导出状态（已匹配/未匹配计数）。"""
    from ...services.old_library.edition_registry import resolve_edition_id
    from ...services.textbook_diff.coarse_export import (
        list_xiaoke_edition_coarse_export_targets,
    )

    try:
        eid = resolve_edition_id(edition_id) or edition_id
        data = list_xiaoke_edition_coarse_export_targets(eid)
        return jsonify({"ok": True, **data})
    except (KeyError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.post("/workbook/xiaoke/editions/<edition_id>/export-coarse-xlsx")
def workbook_xiaoke_export_coarse_xlsx(edition_id: str):
    """小科整版下载粗分：文件名=版本.xlsx，每册一个 sheet。"""
    from io import BytesIO

    from flask import send_file

    from ...services.old_library.edition_registry import resolve_edition_id
    from ...services.textbook_diff.coarse_export import (
        build_edition_coarse_match_export_xlsx,
    )

    body = request.get_json(silent=True) or {}
    pairs = body.get("pairs")
    include_matched = body.get("include_matched", True)
    include_unmatched = body.get("include_unmatched", True)
    if include_matched is None:
        include_matched = True
    if include_unmatched is None:
        include_unmatched = True
    try:
        eid = resolve_edition_id(edition_id) or edition_id
        raw, download_name = build_edition_coarse_match_export_xlsx(
            edition_id=eid,
            pairs=pairs,
            include_matched=bool(include_matched),
            include_unmatched=bool(include_unmatched),
        )
        return send_file(
            BytesIO(raw),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=download_name,
        )
    except (KeyError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"导出失败：{exc}"}), 500


@api_textbook_diff_bp.post("/workbook/xiaoke/editions/<edition_id>/import-textbooks")
def workbook_xiaoke_import_textbooks(edition_id: str):
    """小科：一键上传本版所有新教材 PDF。"""
    from ...extensions import db
    from ...services.old_library.edition_registry import resolve_edition_id
    from ...services.textbook_diff.xiaoke_batch_import import (
        import_xiaoke_edition_textbooks_from_uploads,
        preview_xiaoke_edition_textbook_import,
    )

    try:
        eid = resolve_edition_id(edition_id) or edition_id
    except Exception:
        eid = edition_id

    if request.args.get("preview") == "1" or (
        request.is_json and (request.get_json(silent=True) or {}).get("preview")
    ):
        body = request.get_json(silent=True) or {}
        try:
            result = preview_xiaoke_edition_textbook_import(
                edition_id=eid,
                filenames=body.get("filenames"),
                min_score=float(body.get("min_score") or 0.72),
            )
            return jsonify({"ok": True, **result})
        except (KeyError, FileNotFoundError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    uploads = request.files.getlist("pdf") or request.files.getlist("files")
    if not uploads:
        return jsonify({"ok": False, "error": "请选择 PDF 文件"}), 400
    files: list[tuple[str, bytes]] = []
    for up in uploads:
        if not up or not up.filename:
            continue
        if not up.filename.lower().endswith(".pdf"):
            return jsonify({"ok": False, "error": f"非 PDF：{up.filename}"}), 400
        files.append((up.filename, up.read()))
    replace = str(request.form.get("replace") or "").lower() in ("1", "true", "yes")
    only_missing = str(request.form.get("only_missing") or "1").lower() not in (
        "0",
        "false",
        "no",
    )
    mapping = None
    mapping_raw = request.form.get("mapping")
    if mapping_raw:
        import json

        try:
            mapping = json.loads(mapping_raw)
        except json.JSONDecodeError:
            return jsonify({"ok": False, "error": "mapping 须为 JSON"}), 400
    try:
        result = import_xiaoke_edition_textbooks_from_uploads(
            edition_id=eid,
            files=files,
            replace=replace,
            only_missing=only_missing,
            mapping=mapping,
        )
        return jsonify({"ok": True, **result})
    except (KeyError, FileNotFoundError, ValueError) as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"导入失败：{exc}"}), 500


@api_textbook_diff_bp.post("/workbook/xiaoke/editions/<edition_id>/preprocess-all")
def workbook_xiaoke_preprocess_all(edition_id: str):
    """小科：一键开启本版所有新教材预处理（目录+粗分+划页，后台串行）。"""
    from flask import current_app

    from ...extensions import db
    from ...services.old_library.edition_registry import resolve_edition_id
    from ...services.textbook_diff.xiaoke_edition_preprocess_batch import (
        start_xiaoke_edition_preprocess_all,
    )

    body = request.get_json(silent=True) or {}
    only_pending = body.get("only_pending", True)
    if only_pending is None:
        only_pending = True
    try:
        eid = resolve_edition_id(edition_id) or edition_id
        result = start_xiaoke_edition_preprocess_all(
            edition_id=eid,
            app=current_app._get_current_object(),
            only_pending=bool(only_pending),
        )
        return jsonify(result)
    except (KeyError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"启动失败：{exc}"}), 500


@api_textbook_diff_bp.get("/workbook/xiaoke/editions/<edition_id>/preprocess-all")
def workbook_xiaoke_preprocess_all_status(edition_id: str):
    from ...services.old_library.edition_registry import resolve_edition_id
    from ...services.textbook_diff.xiaoke_edition_preprocess_batch import (
        get_xiaoke_edition_preprocess_job,
    )

    try:
        eid = resolve_edition_id(edition_id) or edition_id
    except Exception:
        eid = edition_id
    job = get_xiaoke_edition_preprocess_job(eid)
    return jsonify({"ok": True, "job": job})


@api_textbook_diff_bp.post("/workbook/xiaoke/editions/<edition_id>/preprocess-all/cancel")
def workbook_xiaoke_preprocess_all_cancel(edition_id: str):
    from ...services.old_library.edition_registry import resolve_edition_id
    from ...services.textbook_diff.xiaoke_edition_preprocess_batch import (
        cancel_xiaoke_edition_preprocess_job,
    )

    try:
        eid = resolve_edition_id(edition_id) or edition_id
    except Exception:
        eid = edition_id
    ok = cancel_xiaoke_edition_preprocess_job(eid)
    return jsonify({"ok": ok})
