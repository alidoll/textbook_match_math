"""教材对比 — PDF 上传、目录识别、页码划分。"""
from __future__ import annotations

import threading
from pathlib import Path

from flask import Response, current_app, jsonify, request

from ...extensions import db
from ...models import Lesson, LessonPage, Volume
from ...services.textbook_diff.catalog import bootstrap_diff_catalog
from ...services.textbook_diff.parse import _pdf_path_for_volume, parse_diff_volume_pdf
from ...services.textbook_diff.pdf_upload import upload_diff_volume_pdf
from ...services.lesson_reorder import reorder_lesson_in_volume
from ...services.textbook_diff.volumes import diff_volume_detail, get_diff_volume_by_code
from ...services.new_library.pdf.lesson_page_range import update_lesson_page_range
from ...services.volume_draft_pdfs import clear_volume_full_pdf
from . import api_textbook_diff_bp


def _pdf_role_from_request() -> str:
    return (
        request.form.get("pdf_role")
        or request.args.get("pdf_role")
        or "full"
    )


@api_textbook_diff_bp.post("/volumes/<volume_code>/pdf")
def upload_pdf(volume_code: str):
    upload = request.files.get("pdf") or request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "请选择 PDF 文件（字段名 pdf）"}), 400
    try:
        content = upload.read()
        result = upload_diff_volume_pdf(
            volume_code=volume_code,
            content=content,
            filename=upload.filename,
            mime_type=upload.mimetype,
            pdf_role=_pdf_role_from_request(),
        )
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        err = str(exc)
        if "WinError 5" in err or "拒绝访问" in err:
            err = f"无法写入 PDF 镜像：目标文件可能被占用。请关闭 {volume_code}.pdf 后重试"
        return jsonify({"ok": False, "error": f"上传失败：{err}"}), 500


@api_textbook_diff_bp.delete("/volumes/<volume_code>/pdf/full")
def clear_full_pdf(volume_code: str):
    """清除本册完整版 PDF 绑定（保留目录；页图清空）。"""
    try:
        volume = get_diff_volume_by_code(volume_code)
        cleared = clear_volume_full_pdf(volume)
        db.session.commit()
        detail = diff_volume_detail(volume)
        detail.update(cleared)
        return jsonify({"ok": True, **detail})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"清除完整版失败：{exc}"}), 500


@api_textbook_diff_bp.post("/volumes/<volume_code>/draft-page-map")
def scan_draft_page_map(volume_code: str):
    """修订版：OCR/解析页脚印刷页码。"""
    from ...services.textbook_diff.draft_page_map import scan_draft_printed_pages

    body = request.get_json(silent=True) or {}
    preview_blob_id = str(body.get("preview_blob_id") or "").strip() or None
    try:
        volume = get_diff_volume_by_code(volume_code)
        if not volume.preview_blob_id and not (volume.draft_pdfs or []):
            return jsonify({"ok": False, "error": "请先上传不完整修订版 PDF"}), 400
        result = scan_draft_printed_pages(volume, preview_blob_id=preview_blob_id)
        detail = diff_volume_detail(volume)
        detail.update(result)
        return jsonify({"ok": True, **detail})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"解析印刷页码失败：{exc}"}), 500


@api_textbook_diff_bp.post("/volumes/<volume_code>/draft-pages")
def build_draft_pages(volume_code: str):
    """修订版：按物理页生成页图到磁盘。"""
    from ...services.textbook_diff.draft_page_map import build_draft_page_images

    body = request.get_json(silent=True) or {}
    preview_blob_id = str(body.get("preview_blob_id") or "").strip() or None
    try:
        volume = get_diff_volume_by_code(volume_code)
        result = build_draft_page_images(volume, preview_blob_id=preview_blob_id)
        detail = diff_volume_detail(volume)
        detail.update(result)
        return jsonify({"ok": True, **detail})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"生成修订版页图失败：{exc}"}), 500


@api_textbook_diff_bp.post("/volumes/<volume_code>/catalog-from-pdf")
def catalog_from_pdf(volume_code: str):
    body = request.get_json(silent=True) or {}
    replace = bool(body.get("replace", True))
    try:
        result = bootstrap_diff_catalog(volume_code=volume_code, replace=replace)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"目录导入失败：{exc}"}), 500


def _start_parse_in_background(volume_code: str, **kwargs) -> None:
    app = current_app._get_current_object()

    def worker() -> None:
        with app.app_context():
            try:
                parse_diff_volume_pdf(volume_code=volume_code, **kwargs)
            except Exception as exc:
                import logging
                import traceback

                logger = logging.getLogger(__name__)
                logger.exception("划分页码后台线程异常: %s", exc)
                # 写入数据库，避免状态卡在 processing
                try:
                    from ...models import Volume

                    vol = Volume.query.filter_by(volume_code=volume_code).first()
                    if vol:
                        vol.parse_status = "failed"
                        vol.parse_error = f"后台线程异常: {exc}\n{traceback.format_exc()[:1500]}"
                        db.session.commit()
                except Exception:
                    db.session.rollback()

    threading.Thread(target=worker, daemon=True, name=f"diff-parse-{volume_code}").start()


@api_textbook_diff_bp.post("/volumes/<volume_code>/parse")
def parse_pdf(volume_code: str):
    body = request.get_json(silent=True) or {}
    replace_lessons = bool(body.get("replace_lessons", False))
    force_recalibrate = bool(body.get("force_recalibrate", False))

    try:
        volume = get_diff_volume_by_code(volume_code)
        if volume.parse_status == "processing" and not force_recalibrate:
            return jsonify(
                {
                    "ok": True,
                    "parse_status": "processing",
                    "volume_code": volume_code,
                    "message": "划分页码仍在后台进行，请稍候…",
                }
            ), 202

        volume.parse_status = "processing"
        volume.parse_error = None
        db.session.commit()
        _start_parse_in_background(
            volume_code,
            replace_lessons=replace_lessons,
            force_recalibrate=force_recalibrate,
        )
        return jsonify(
            {
                "ok": True,
                "parse_status": "processing",
                "volume_code": volume_code,
                "message": "划分页码已在后台开始",
            }
        ), 202
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"解析失败：{exc}"}), 500


@api_textbook_diff_bp.get("/volumes/<volume_code>/pdf-page/<int:page_num>.png")
def render_pdf_page(volume_code: str, page_num: int):
    if page_num < 1:
        return Response("页码无效", status=400)
    try:
        from ...parsers.pdf_spread import (
            layout_for_volume,
            render_view_page_png,
        )
        from ...repo_paths import base_data_dir
        from ...services.volume_pdf import resolve_volume_pdf_path

        volume = get_diff_volume_by_code(volume_code)
        source = request.args.get("source", "full")
        preview_blob_id = request.args.get("preview_blob_id", "").strip() or None
        layout_override = request.args.get("layout", "").strip()
        dpi = 120
        pdf_path = resolve_volume_pdf_path(
            volume,
            source=source,
            preview_blob_id=preview_blob_id,
        )
        # 修订版多为不连续单页；默认强制 single，避免按整册 spread 误裁半页
        if layout_override in ("single", "spread"):
            layout = layout_override
        elif source == "draft":
            layout = "single"
        else:
            # 优先已存布局，避免每次翻页都重新检测 PDF（极慢）
            layout = layout_for_volume(volume, pdf_path=pdf_path, persist=True)

        # 磁盘缓存：翻页时右侧表格秒开，左侧图也不应每次重渲 PDF
        src_tag = "draft" if source == "draft" else "full"
        blob_tag = (preview_blob_id or "none")[:12]
        cache_dir = base_data_dir() / "cache" / "pdf_page_png"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{volume_code}_{src_tag}_{blob_tag}_p{page_num}_{layout}_{dpi}.png"
        try:
            pdf_mtime = int(Path(pdf_path).stat().st_mtime)
        except OSError:
            pdf_mtime = 0
        if cache_file.is_file() and cache_file.stat().st_mtime >= pdf_mtime:
            resp = Response(cache_file.read_bytes(), mimetype="image/png")
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp

        png = render_view_page_png(
            pdf_path,
            page_num,
            dpi=dpi,
            layout=layout,
        )
        try:
            cache_file.write_bytes(png)
        except OSError:
            pass
        resp = Response(png, mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    except ValueError as exc:
        return Response(str(exc), status=400)
    except Exception as exc:
        return Response(f"渲染失败：{exc}", status=500)


@api_textbook_diff_bp.post("/volumes/<volume_code>/lessons/<lesson_uid>/reorder")
def reorder_lesson_row(volume_code: str, lesson_uid: str):
    body = request.get_json(silent=True) or {}
    direction = body.get("direction")
    try:
        volume = get_diff_volume_by_code(volume_code)
        reorder_lesson_in_volume(
            volume_code=volume_code,
            lesson_uid=lesson_uid,
            direction=str(direction or ""),
            book_type=volume.book_type or "diff_old",
        )
        return jsonify({"ok": True, **diff_volume_detail(volume)})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400


@api_textbook_diff_bp.patch("/lessons/<lesson_uid>/page-range")
def patch_lesson_page_range(lesson_uid: str):
    body = request.get_json(silent=True) or {}
    page_start = body.get("page_start")
    page_end = body.get("page_end")
    if page_start is None or page_end is None:
        return jsonify({"ok": False, "error": "需要 page_start 与 page_end"}), 400
    try:
        result = update_lesson_page_range(
            lesson_uid=lesson_uid,
            page_start=int(page_start),
            page_end=int(page_end),
            rebuild_pages=bool(body.get("rebuild_pages", False)),
        )
        return jsonify(result)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"保存失败：{exc}"}), 500


@api_textbook_diff_bp.post("/volumes/<volume_code>/page-offset")
def shift_volume_page_offset(volume_code: str):
    """整册已划分课时的 page_start/end 一起平移，不重生成页图。"""
    from ...services.textbook_diff.page_offset import shift_volume_lesson_page_ranges

    body = request.get_json(silent=True) or {}
    try:
        delta = int(body.get("delta"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "需要整数 delta"}), 400
    try:
        return jsonify(shift_volume_lesson_page_ranges(volume_code=volume_code, delta=delta))
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"整册偏移失败：{exc}"}), 500


@api_textbook_diff_bp.get("/volumes/<volume_code>/debug-pages")
def debug_pages(volume_code: str):
    """调试：导出 PDF 前 N 页原始文字，用于定位目录提取问题。"""
    n = request.args.get("n", 12, type=int)
    try:
        from ...services.textbook_diff.catalog import _ocr_pages
        volume = get_diff_volume_by_code(volume_code)
        from ...services.textbook_diff.catalog import _pdf_path_for_volume as _pp
        pdf_path = _pp(volume)
        lines = _ocr_pages(pdf_path, max_pages=n)
        return jsonify({"ok": True, "volume_code": volume_code, "page_count": n, "lines": lines})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@api_textbook_diff_bp.get("/lessons/<lesson_uid>/pages")
def list_lesson_pages(lesson_uid: str):
    les = Lesson.query.filter_by(lesson_uid=lesson_uid).first()
    if not les:
        return jsonify({"ok": False, "error": "未找到课时"}), 404

    volume = Volume.query.get(les.volume_id)
    if not volume:
        return jsonify({"ok": False, "error": "未找到册次"}), 404

    volume_code = volume.volume_code

    # Preview mode: render PDF pages directly
    q_start = request.args.get("page_start", type=int)
    q_end = request.args.get("page_end", type=int)
    preview = q_start is not None and q_end is not None
    if preview:
        if q_start < 1 or q_end < q_start:
            return jsonify({"ok": False, "error": "页码范围无效"}), 400
        items = []
        for pdf_page in range(q_start, q_end + 1):
            page_index = pdf_page - q_start + 1
            items.append({
                "page_index": page_index,
                "pdf_page": pdf_page,
                "url": f"/api/textbook-diff/volumes/{volume_code}/pdf-page/{pdf_page}.png",
                "live": True,
            })
        return jsonify({
            "ok": True, "lesson_uid": les.lesson_uid,
            "lesson_name": les.lesson_name,
            "page_start": q_start, "page_end": q_end,
            "preview": True, "pages": items,
        })

    # Saved page images
    pages = (
        LessonPage.query.filter_by(lesson_id=les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    items = []
    for p in pages:
        pdf_page = None
        if les.page_start:
            pdf_page = les.page_start + p.page_index - 1
        items.append({
            "page_index": p.page_index,
            "pdf_page": pdf_page,
            "blob_id": p.blob_id,
            "url": f"/api/file-blobs/{p.blob_id}",
        })
    return jsonify({
        "ok": True, "lesson_uid": les.lesson_uid,
        "lesson_name": les.lesson_name,
        "page_start": les.page_start, "page_end": les.page_end,
        "pages": items,
    })


@api_textbook_diff_bp.post("/volumes/<volume_code>/lesson-pages")
def rebuild_lesson_pages(volume_code: str):
    """生成页图：将已有页码的课时渲染为 PNG 存入 lesson_pages + file_blobs。"""
    try:
        from ...services.new_library.pdf.rebuild_pages import rebuild_lesson_pages_for_volume
        from ...services.new_library.pdf.parse import _pdf_path_for_volume as _new_pp
        from ...services.textbook_diff.volumes import get_diff_volume_by_code as _get, diff_volume_detail
        from ...models import Lesson as _Les
        from ...query.lesson_order import order_lessons_query
        from ...services.old_library.pdf.lesson_pages_build import build_lesson_pages_from_parse
        from ...extensions import db as _db

        volume = _get(volume_code)
        if not volume.blob_id:
            return jsonify({"ok": False, "error": "请先上传 PDF"}), 400

        lessons = order_lessons_query(_Les.query.filter_by(volume_id=volume.id)).all()
        row_updates = [
            {"lesson_id": les.id, "page_start": les.page_start, "page_end": les.page_end}
            for les in lessons
            if les.page_start and les.page_end and les.page_end >= les.page_start
        ]
        if not row_updates:
            return jsonify(
                {
                    "ok": False,
                    "error": "尚无页码数据，请先点「划分页码」（识别目录后若重导过目录也需再划分一次）",
                }
            ), 400

        # Use our diff catalog's _pdf_path_for_volume
        from ...services.textbook_diff.catalog import _pdf_path_for_volume
        pdf_path = _pdf_path_for_volume(volume)
        written = build_lesson_pages_from_parse(
            volume=volume, pdf_path=pdf_path, row_updates=row_updates, render_dpi=120,
        )
        _db.session.commit()

        result = diff_volume_detail(volume)
        result.update({"ok": True, "lesson_pages_written": written, "message": f"已生成 {written} 张教材页图"})
        return jsonify(result)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": f"生成页图失败：{exc}"}), 500
