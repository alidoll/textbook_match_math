"""小科（现网旧库）相关视图辅助函数。

对于非小科 volume（即纯教材对比册），所有函数返回安全默认值，
使调用方走 textbook-diff 常规路径，不影响对比功能。
"""
from __future__ import annotations

from ...models import Lesson, Volume


def is_xiaoke_volume(vol: Volume | None) -> bool:
    """判断是否为小科（现网旧库）册次。

    小科册的 book_type 通常为 'old' 且 edition 含特定标记；
    纯教材对比册为 'diff_old' / 'diff_new'。
    """
    if vol is None:
        return False
    return getattr(vol, "book_type", "") not in ("diff_old", "diff_new", "old")


def old_pdf_render_info(
    old_les: Lesson | None,
    *,
    fallback_old_code: str = "",
) -> dict:
    """旧侧 PDF 渲染信息：api + volume_code + has_pdf。

    非小科走 textbook-diff 自身的 PDF 渲染。
    """
    code = fallback_old_code
    if old_les:
        vol = Volume.query.get(old_les.volume_id)
        if vol:
            code = vol.volume_code
    return {
        "api": "textbook-diff",
        "volume_code": code,
        "has_pdf": True,
    }


def page_png_url(
    *,
    api: str = "old-library",
    volume_code: str = "",
    pdf_page: int = 1,
    cache_token: str | None = None,
) -> str:
    """小科旧库页图 URL（非小科不会走到这里，给出安全回退）。"""
    suffix = f"?v={cache_token}" if cache_token else ""
    return f"/api/{api}/volumes/{volume_code}/pdf-page/{pdf_page}.png{suffix}"


def resolve_xiaoke_lib_volume_for_diff_old(old_vol: Volume) -> Volume | None:
    """小科对比旧册 → 对应的旧库册（有 PDF）。

    非小科返回 None，调用方用 old_vol 自身的 PDF。
    """
    return None


def resolve_xiaoke_ocr_old_volume(old_vol: Volume) -> Volume:
    """小科 OCR/缓存键用的旧库册。

    非小科返回 old_vol 自身。
    """
    return old_vol


def resolve_xiaoke_ocr_volume_for_old_lesson(
    old_vol: Volume, old_les: Lesson
) -> Volume | None:
    """小科某课的 OCR 旧库册（按课定位）。

    非小科返回 None。
    """
    return None


def ensure_xiaoke_new_page_ranges(*, new_code: str) -> None:
    """小科新侧课时页码划分（进入对比时自动触发）。

    非小科无操作（页码在 intake 阶段已划分）。
    """
    return None


def old_lesson_missing_pages_message(old_les: Lesson) -> str:
    """小科旧课缺页码时的提示信息。"""
    return ""
