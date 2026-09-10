"""双轨上下文标量快照：长任务 OCR 会 session.remove()，执行层只读 snap。"""
from __future__ import annotations

from typing import Any


def _lesson_snap(les: Any) -> dict[str, Any]:
    return {
        "id": str(les.id),
        "lesson_uid": str(les.lesson_uid),
        "lesson_name": str(getattr(les, "lesson_name", None) or ""),
        "lesson_no": getattr(les, "lesson_no", None),
        "unit_title": str(getattr(les, "unit_title", None) or ""),
    }


def _page_snap(page: Any) -> dict[str, Any]:
    if isinstance(page, dict):
        return {
            "page_index": int(page.get("page_index") or 0),
            "blob_id": page.get("blob_id"),
        }
    return {
        "page_index": int(page.page_index),
        "blob_id": getattr(page, "blob_id", None),
    }


def _slide_snap(slide: Any) -> dict[str, Any]:
    if isinstance(slide, dict):
        return {
            "slide_index": int(slide.get("slide_index") or 0),
            "blob_id": slide.get("blob_id"),
            "ocr_text": str(slide.get("ocr_text") or "").strip(),
        }
    return {
        "slide_index": int(slide.slide_index),
        "blob_id": getattr(slide, "blob_id", None),
        "ocr_text": (getattr(slide, "ocr_text", None) or "").strip(),
    }


def build_dual_track_ctx_snap(ctx: dict[str, Any]) -> dict[str, Any]:
    """在仍绑定 Session 时提取全部标量，供 OCR 执行路径使用。"""
    profile = ctx.get("dual_track_profile")
    if not isinstance(profile, dict) or not profile:
        from .dual_track_pipeline_profile import get_dual_track_pipeline_profile

        profile = get_dual_track_pipeline_profile(str(ctx["new_les"].lesson_uid))
    return {
        "new_les": _lesson_snap(ctx["new_les"]),
        "old_les": _lesson_snap(ctx["old_les"]),
        "dual_track_profile": dict(profile),
        "old_pages": [_page_snap(p) for p in (ctx.get("old_pages") or [])],
        "slides": [_slide_snap(s) for s in (ctx.get("slides") or [])],
    }


def ctx_snap(ctx: dict[str, Any]) -> dict[str, Any]:
    """返回 ctx 内已有 snap，或即时构建（兼容旧调用）。"""
    snap = ctx.get("snap")
    if isinstance(snap, dict) and snap.get("new_les") and snap.get("old_les"):
        return snap
    return build_dual_track_ctx_snap(ctx)
