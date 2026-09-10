"""新课双轨实验试点配置（前置 OCR 只跑新教材，旧侧读库/缓存）。"""
from __future__ import annotations

from typing import Any


def _page_pi(page: Any) -> int:
    if isinstance(page, dict):
        return int(page.get("page_index") or 0)
    return int(page.page_index)


def old_library_text_atoms_complete_in_memory(
    pages: list[Any],
    old_atoms_by_code: dict[str, Any],
) -> bool:
    """ctx 已加载旧库原子时，无需再查库。"""
    if not pages or not old_atoms_by_code:
        return False
    for page in pages:
        pi = _page_pi(page)
        has = any(
            (getattr(a, "atom_type", None) or "text").lower() in ("text", "title")
            for a in old_atoms_by_code.values()
            if int(getattr(a, "page_index", 0) or 0) == pi
        )
        if not has:
            return False
    return True


def old_library_text_atoms_complete(
    old_lesson_id: Any,
    pages: list[Any],
) -> bool:
    """旧库每页均有 text/title 原子（试点旧侧以库为准）。"""
    if not pages:
        return False
    from ....extensions import db
    from ....models import TextbookAtom

    for page in pages:
        pi = _page_pi(page)
        has = (
            db.session.query(TextbookAtom.id)
            .filter_by(lesson_id=old_lesson_id, page_index=pi)
            .filter(TextbookAtom.atom_type.in_(("text", "title")))
            .first()
        )
        if not has:
            return False
    return True


def old_side_text_authoritative_from_rows(
    old_textbook_rows: list[dict[str, Any]],
) -> bool:
    """coverage 行显示旧库每页文字齐全。"""
    if not old_textbook_rows:
        return False
    return all(
        int(r.get("text_atom_count") or 0) > 0 and not r.get("low_coverage")
        for r in old_textbook_rows
    )


def prefer_doubao_old_textbook(
    *,
    dual_track_profile: dict[str, Any] | None,
    old_lesson_id: Any,
    old_pages: list[Any] | None,
    old_atoms_by_code: dict[str, Any] | None = None,
) -> bool:
    """教材轨匹配是否优先豆包 page_text 缓存（试点旧库齐则否）。"""
    profile = dual_track_profile or {}
    if not profile.get("old_side_readonly"):
        return True
    pages = old_pages or []
    if old_atoms_by_code and old_library_text_atoms_complete_in_memory(
        pages, old_atoms_by_code
    ):
        return False
    if old_library_text_atoms_complete(old_lesson_id, pages):
        return False
    return True


def show_old_textbook_doubao_table(
    *,
    dual_track_profile: dict[str, Any] | None,
    old_textbook_rows: list[dict[str, Any]],
    has_doubao_rows: bool,
) -> bool:
    """试点且旧库 atom 已齐时，隐藏不完整的豆包缓存对照表。"""
    if not has_doubao_rows:
        return False
    profile = dual_track_profile or {}
    if profile.get("old_side_readonly") and old_side_text_authoritative_from_rows(
        old_textbook_rows
    ):
        return False
    return True

# 湘科四上 U1-L2《蜡的有趣变化》↔ 旧库四下同名课（旧侧 OCR 已在旧库试点完成）
DUAL_TRACK_PIPELINE_PROFILES: dict[str, dict[str, Any]] = {
    "湘科版-4-上-new-U1-L2": {
        "label": "蜡的有趣变化 · 双轨试点",
        "old_lesson_uid": "湘科版-4-下-old-U1-L2",
        "old_side_readonly": True,
        "old_side_fallback_ocr": True,
    },
}


def get_dual_track_pipeline_profile(lesson_uid: str) -> dict[str, Any]:
    uid = (lesson_uid or "").strip()
    base = dict(DUAL_TRACK_PIPELINE_PROFILES.get(uid) or {})
    return {
        "enabled": bool(base),
        "label": base.get("label") or "",
        "old_lesson_uid": (base.get("old_lesson_uid") or "").strip(),
        "old_side_readonly": bool(base.get("old_side_readonly")),
        "old_side_fallback_ocr": bool(base.get("old_side_fallback_ocr", True)),
    }
