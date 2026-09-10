"""Intake 模块「对比」：完整版课时粗分 / 修订版页级粗分。"""
from __future__ import annotations

from ...models import Lesson, Volume
from ..volume_draft_pdfs import draft_pdf_fields, resolve_draft_blob_id
from .lesson_match import match_diff_lesson_pairs
from .preview_compare import build_preview_compare_pairs, pdf_page_count
from .volumes import diff_volume_summary, resolve_diff_volume_pair


def _intake_path(volume: Volume) -> str:
    side = "old" if volume.book_type == "diff_old" else "new"
    return f"/textbook-diff/{side}/intake?code={volume.volume_code}"


def _volume_readiness(volume: Volume) -> dict:
    from sqlalchemy import and_

    base_q = Lesson.query.filter_by(volume_id=volume.id)
    lesson_count = base_q.count()
    parsed = base_q.filter(
        and_(Lesson.page_start.isnot(None), Lesson.page_end.isnot(None))
    ).count()
    draft_info = draft_pdf_fields(volume)
    return {
        "volume_code": volume.volume_code,
        "display_title": volume.display_title,
        "book_type": volume.book_type,
        "has_full_pdf": bool(volume.blob_id),
        "has_draft_pdf": draft_info["draft_pdf_count"] > 0,
        "draft_pdf_count": draft_info["draft_pdf_count"],
        "lesson_count": lesson_count,
        "catalog_ok": lesson_count > 0,
        "parse_ok": lesson_count > 0 and parsed == lesson_count,
        "parse_status": volume.parse_status,
        "intake_url": _intake_path(volume),
    }


def build_intake_compare(
    *,
    volume_code: str,
    kind: str,
    preview_blob_id: str | None = None,
    force_preview: bool = False,
    include_preview: bool = True,
) -> dict:
    """
    kind=full：完整版 → 识别目录/划分页码/课时粗分后逐课对比。
    kind=draft：修订版 → 列出预览页并粗分映射旧书页。
    """
    code = (volume_code or "").strip()
    k = (kind or "full").strip().lower()
    if k not in ("full", "draft"):
        raise ValueError("kind 须为 full 或 draft")

    old_vol, new_vol = resolve_diff_volume_pair(code)
    anchor = old_vol if code == old_vol.volume_code else new_vol

    payload: dict = {
        "kind": k,
        "volume_code": code,
        "anchor_volume": diff_volume_summary(anchor),
        "old_volume": diff_volume_summary(old_vol),
        "new_volume": diff_volume_summary(new_vol),
        "old_code": old_vol.volume_code,
        "new_code": new_vol.volume_code,
        "intake_back_url": _intake_path(anchor),
        "readiness": {
            "old": _volume_readiness(old_vol),
            "new": _volume_readiness(new_vol),
        },
    }

    if k == "full":
        payload.update(_build_full_compare(old_vol=old_vol, new_vol=new_vol))
    else:
        payload.update(
            _build_draft_compare(
                old_vol=old_vol,
                new_vol=new_vol,
                anchor=anchor,
                preview_blob_id=preview_blob_id,
                force_preview=force_preview,
                include_preview=include_preview,
            )
        )

    return payload


def build_draft_preview_payload(
    *,
    volume_code: str,
    preview_blob_id: str | None = None,
    force_preview: bool = False,
) -> dict:
    """仅构建修订版预览页粗分结果（供对比页第二阶段加载）。"""
    old_vol, new_vol = resolve_diff_volume_pair(volume_code)
    anchor = old_vol if volume_code == old_vol.volume_code else new_vol
    draft = _build_draft_compare(
        old_vol=old_vol,
        new_vol=new_vol,
        anchor=anchor,
        preview_blob_id=preview_blob_id,
        force_preview=force_preview,
        include_preview=True,
    )
    return {
        "preview_pairs": draft.get("preview_pairs") or [],
        "preview_summary": draft.get("preview_summary"),
        "preview_meta": draft.get("preview_meta"),
        "preview_error": draft.get("preview_error"),
        "ready": draft.get("ready"),
        "preview_blob_id": draft.get("preview_blob_id"),
    }


def _build_full_compare(*, old_vol: Volume, new_vol: Volume) -> dict:
    old_r = _volume_readiness(old_vol)
    new_r = _volume_readiness(new_vol)
    blockers: list[str] = []

    if not new_r["has_full_pdf"]:
        blockers.append("请在新教材侧上传完整版 PDF")
    if not old_r["has_full_pdf"]:
        blockers.append("请在旧教材侧上传完整版 PDF")
    if not new_r["catalog_ok"]:
        blockers.append("请在新教材 intake 完成「识别目录」")
    if not new_r["parse_ok"]:
        blockers.append("请在新教材 intake 完成「划分页码」")
    if not old_r["catalog_ok"]:
        blockers.append("请在旧教材 intake 完成「识别目录」（用于粗分对照）")
    if not old_r["parse_ok"]:
        blockers.append("请在旧教材 intake 完成「划分页码」（用于粗分对照）")

    lesson_pairs = match_diff_lesson_pairs(old_vol=old_vol, new_vol=new_vol)
    old_code = old_vol.volume_code
    new_code = new_vol.volume_code
    for lp in lesson_pairs:
        if lp.get("new") and lp.get("old"):
            lp["compare_url"] = (
                f"/textbook-diff/view?old_code={old_code}&new_code={new_code}"
                f"&mode=lesson&new_lesson_uid={lp['new']['lesson_uid']}"
            )

    return {
        "mode": "full",
        "title": "完整版对比 · 课时粗分",
        "hint": (
            "同版本 · 同年级 · 同学期：新教材课时与旧教材课时按课题号粗分对齐。"
            "每行点「对比」进入左旧右新建块工作台。"
        ),
        "blockers": blockers,
        "ready": not blockers,
        "lesson_pairs": lesson_pairs,
        "steps": [
            {"key": "catalog", "label": "识别目录", "done": new_r["catalog_ok"], "side": "new"},
            {"key": "parse", "label": "划分页码", "done": new_r["parse_ok"], "side": "new"},
            {
                "key": "coarse",
                "label": "粗分（本册旧版对照）",
                "done": new_r["parse_ok"] and old_r["parse_ok"],
                "side": "pair",
            },
        ],
    }


def _resolve_draft_volume(
    old_vol: Volume,
    new_vol: Volume,
    anchor: Volume,
    *,
    preview_blob_id: str | None = None,
) -> Volume:
    if preview_blob_id:
        for vol in (anchor, new_vol, old_vol):
            try:
                resolve_draft_blob_id(vol, preview_blob_id)
                return vol
            except ValueError:
                continue
        raise ValueError("指定的修订版不属于当前册次")
    for vol in (anchor, new_vol if anchor.id == old_vol.id else old_vol):
        if draft_pdf_fields(vol)["draft_pdf_count"] > 0:
            return vol
    return anchor


def _draft_label(draft_vol: Volume, preview_blob_id: str | None) -> str:
    info = draft_pdf_fields(draft_vol)
    bid = resolve_draft_blob_id(draft_vol, preview_blob_id)
    for item in info["draft_pdfs"]:
        if item["blob_id"] == bid:
            return item["label"]
    return bid[:8]


def _build_draft_compare(
    *,
    old_vol: Volume,
    new_vol: Volume,
    anchor: Volume,
    preview_blob_id: str | None = None,
    force_preview: bool = False,
    include_preview: bool = True,
) -> dict:
    old_r = _volume_readiness(old_vol)
    try:
        draft_vol = _resolve_draft_volume(
            old_vol,
            new_vol,
            anchor,
            preview_blob_id=preview_blob_id,
        )
        selected_blob_id = resolve_draft_blob_id(draft_vol, preview_blob_id)
    except ValueError as exc:
        return {
            "mode": "draft",
            "title": "修订版对比 · 预览页粗分",
            "hint": str(exc),
            "blockers": [str(exc)],
            "ready": False,
            "preview_pairs": [],
            "preview_error": str(exc),
        }

    has_draft = _volume_readiness(draft_vol)["has_draft_pdf"]
    blockers: list[str] = []
    if not has_draft:
        blockers.append("请先上传不完整修订版 PDF")
    if not old_r["has_full_pdf"]:
        blockers.append("请在旧教材侧上传完整版 PDF")
    if not old_r["catalog_ok"]:
        blockers.append("请在旧教材 intake 完成「识别目录」（用于页级粗分）")

    preview_pairs: list[dict] = []
    preview_error: str | None = None
    preview_summary = None
    preview_meta = None

    if not blockers and include_preview:
        try:
            preview = build_preview_compare_pairs(
                old_vol=old_vol,
                new_vol=draft_vol,
                new_pdf_source="draft",
                preview_blob_id=selected_blob_id,
                force_refresh=force_preview,
            )
            preview_pairs = preview.get("pairs") or []
            preview_summary = preview.get("summary")
            preview_meta = {
                "comparable_count": preview.get("comparable_count"),
                "old_offset": preview.get("old_offset"),
                "new_pdf_pages": preview.get("new_pdf_pages"),
                "build_tier": preview.get("build_tier"),
            }
        except ValueError as exc:
            preview_error = str(exc)
            blockers.append(str(exc))

    draft_pages = (
        pdf_page_count(draft_vol, pdf_source="draft", preview_blob_id=selected_blob_id)
        if has_draft
        else None
    )
    draft_label = _draft_label(draft_vol, selected_blob_id)

    return {
        "mode": "draft",
        "title": f"修订版对比 · {draft_label}",
        "hint": (
            f"当前修订版「{draft_label}」共 {draft_pages or '?'} 页，"
            "从本版本本年级本学习本科目旧教材中找出最匹配页。"
            "每行点「对比」进入左旧右新建块工作台。"
        ),
        "blockers": blockers,
        "ready": not blockers and bool(preview_pairs),
        "draft_volume_code": draft_vol.volume_code,
        "preview_blob_id": selected_blob_id,
        "draft_label": draft_label,
        "preview_pairs": preview_pairs,
        "preview_summary": preview_summary,
        "preview_meta": preview_meta,
        "preview_error": preview_error,
        "preview_pending": not include_preview and not blockers,
    }
