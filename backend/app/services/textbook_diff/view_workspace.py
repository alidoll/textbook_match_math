"""教材对比工作台：左旧右新 + 原子 OCR/锚定。"""
from __future__ import annotations

from ...models import DiffLessonPair, Lesson, Volume
from .atom_compare import load_page_atom_cache
from .lesson_match import (
    is_recurring_activity_lesson,
    lesson_at_pdf_page,
    match_diff_lesson_pairs,
)
from .volumes import get_diff_volume_by_code
from .workbook import _lesson_brief, _lesson_label


def _atom_cache_old_vol(old_vol: Volume) -> Volume:
    """小科页对原子缓存键用有 PDF 的旧库册（与 OCR API 一致）。"""
    from .xiaoke_view import is_xiaoke_volume, resolve_xiaoke_ocr_old_volume

    if is_xiaoke_volume(old_vol):
        return resolve_xiaoke_ocr_old_volume(old_vol)
    return old_vol


def _resolve_diff_lesson(lesson_uid: str, book_type: str) -> Lesson:
    uid = (lesson_uid or "").strip()
    bt = book_type if book_type.startswith("diff_") else f"diff_{book_type}"
    les = Lesson.query.filter_by(lesson_uid=uid).first()
    if not les:
        raise ValueError(f"未找到课时：{uid}")
    vol = Volume.query.get(les.volume_id)
    if not vol or vol.book_type != bt:
        raise ValueError(f"非教材对比课时：{uid}")
    return les


def _volume_pdf_cache_token(volume: Volume | None) -> str:
    """换 PDF 后页图 URL 必须变，否则浏览器会继续用旧的 /pdf-page/N.png。"""
    bid = getattr(volume, "blob_id", None) if volume is not None else None
    if not bid:
        return ""
    from ...models import FileBlob

    blob = FileBlob.query.get(bid)
    return ((getattr(blob, "content_hash", None) or "").strip())[:12]


def _pdf_cache_tokens(*, old_volume_code: str, new_vol: Volume | None) -> tuple[str, str]:
    old_side = Volume.query.filter_by(volume_code=(old_volume_code or "").strip()).first()
    return _volume_pdf_cache_token(old_side), _volume_pdf_cache_token(new_vol)


def _page_url(
    volume_code: str,
    page: int,
    *,
    source: str | None = None,
    preview_blob_id: str | None = None,
    cache_token: str | None = None,
) -> str:
    q = []
    if source:
        q.append(f"source={source}")
    if preview_blob_id:
        q.append(f"preview_blob_id={preview_blob_id}")
    if cache_token:
        q.append(f"v={cache_token}")
    suffix = f"?{'&'.join(q)}" if q else ""
    return f"/api/textbook-diff/volumes/{volume_code}/pdf-page/{page}.png{suffix}"


def _prefer_page_url(
    *,
    volume_code: str,
    pdf_page: int,
    lesson: Lesson | None = None,
    source: str | None = None,
    preview_blob_id: str | None = None,
    pdf_api: str = "textbook-diff",
    cache_token: str | None = None,
) -> str:
    # 按 PDF 物理页渲染。LessonPage 只记课内序号，页码偏移后会串到旧图。
    if pdf_api == "old-library":
        from .xiaoke_view import page_png_url

        return page_png_url(
            api="old-library",
            volume_code=volume_code,
            pdf_page=pdf_page,
            cache_token=cache_token,
        )
    return _page_url(
        volume_code,
        pdf_page,
        source=source,
        preview_blob_id=preview_blob_id,
        cache_token=cache_token,
    )


def build_page_view_workspace(
    *,
    old_code: str,
    new_code: str,
    old_page: int,
    new_page: int,
    new_pdf_source: str = "full",
    preview_blob_id: str | None = None,
    include_atoms: bool = True,
) -> dict:
    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    old_lesson = lesson_at_pdf_page(old_vol, old_page)
    new_lesson = lesson_at_pdf_page(new_vol, new_page)
    old_les = (
        Lesson.query.filter_by(lesson_uid=old_lesson["lesson_uid"]).first()
        if old_lesson and old_lesson.get("lesson_uid")
        else None
    )
    new_les = (
        Lesson.query.filter_by(lesson_uid=new_lesson["lesson_uid"]).first()
        if new_lesson and new_lesson.get("lesson_uid")
        else None
    )
    new_src = "draft" if new_pdf_source == "draft" else None
    from .xiaoke_view import is_xiaoke_volume, old_pdf_render_info

    old_render = old_pdf_render_info(old_les, fallback_old_code=old_code)
    if is_xiaoke_volume(old_vol) and old_render["api"] == "textbook-diff":
        from .xiaoke_view import resolve_xiaoke_lib_volume_for_diff_old

        lib = resolve_xiaoke_lib_volume_for_diff_old(old_vol)
        if lib and lib.blob_id:
            old_render = {
                "api": "old-library",
                "volume_code": lib.volume_code,
                "has_pdf": True,
            }
    old_pdf_cache, new_pdf_cache = _pdf_cache_tokens(
        old_volume_code=old_render["volume_code"],
        new_vol=new_vol,
    )
    payload: dict = {
        "mode": "page",
        "title": f"预览 p{new_page} ↔ 旧书 p{old_page}",
        "old_volume": {"volume_code": old_code, "display_title": old_vol.display_title},
        "new_volume": {"volume_code": new_code, "display_title": new_vol.display_title},
        "old_page": old_page,
        "new_page": new_page,
        "new_pdf_source": new_pdf_source,
        "preview_blob_id": preview_blob_id,
        "old_pdf_api": old_render["api"],
        "old_pdf_volume_code": old_render["volume_code"],
        "old_pdf_cache": old_pdf_cache,
        "new_pdf_cache": new_pdf_cache,
        "old_page_url": _prefer_page_url(
            volume_code=old_render["volume_code"],
            pdf_page=old_page,
            lesson=old_les,
            pdf_api=old_render["api"],
            cache_token=old_pdf_cache,
        ),
        "new_page_url": _prefer_page_url(
            volume_code=new_code,
            pdf_page=new_page,
            lesson=new_les if new_pdf_source != "draft" else None,
            source=new_src,
            preview_blob_id=preview_blob_id if new_pdf_source == "draft" else None,
            cache_token=new_pdf_cache,
        ),
        "old_lesson": old_lesson,
        "ocr_hint": "在左右页下方分别点击 ① 文字 OCR / ② 图片 OCR",
    }
    if include_atoms:
        payload["atoms"] = load_page_atom_cache(
            old_vol=_atom_cache_old_vol(old_vol),
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
        ) or {
            "old_page": old_page,
            "new_page": new_page,
            "old_atoms": [],
            "new_atoms": [],
            "text_ocr_done": False,
            "image_ocr_done": False,
            "summary": {},
        }
    return payload


def _resolve_old_lesson_for_new(
    *,
    old_vol: Volume,
    new_vol: Volume,
    new_les: Lesson,
) -> tuple[Lesson, str]:
    """优先用本册粗分落库课对，避免对比页重算覆盖人工改对。"""
    stored = DiffLessonPair.query.filter_by(
        old_volume_id=old_vol.id,
        new_volume_id=new_vol.id,
        new_lesson_id=new_les.id,
    ).first()
    if stored and stored.old_lesson_id and stored.pair_status != "rejected":
        old_les = Lesson.query.get(stored.old_lesson_id)
        # 语文园地等每单元重复栏目：落库若跨单元则视为错对，重新匹配
        bad_recurring = bool(
            old_les
            and is_recurring_activity_lesson(new_les)
            and old_les.unit_no is not None
            and new_les.unit_no is not None
            and int(old_les.unit_no) != int(new_les.unit_no)
            and stored.pair_status != "confirmed"
        )
        if old_les and not bad_recurring:
            return old_les, str(stored.match_method or "manual")

    from .xiaoke_view import is_xiaoke_volume, resolve_xiaoke_lib_volume_for_diff_old

    if is_xiaoke_volume(old_vol):
        lib = resolve_xiaoke_lib_volume_for_diff_old(old_vol)
        if lib:
            from .xiaoke_lesson_match import match_xiaoke_lesson_pairs

            pairs = match_xiaoke_lesson_pairs(
                old_vol=lib,
                new_vol=new_vol,
                diff_old_vol=old_vol,
                use_llm=False,
            )
            pair = next(
                (p for p in pairs if p["new"]["lesson_uid"] == new_les.lesson_uid),
                None,
            )
            if pair and pair.get("old"):
                old_les = Lesson.query.filter_by(
                    lesson_uid=pair["old"]["lesson_uid"]
                ).first()
                if old_les:
                    return old_les, str(pair.get("match_method") or "name_similar")
        raise ValueError("未找到对应的旧教材课时，请先在本册建设运行粗分并确认课对")

    pairs = match_diff_lesson_pairs(old_vol=old_vol, new_vol=new_vol)
    pair = next((p for p in pairs if p["new"]["lesson_uid"] == new_les.lesson_uid), None)
    if not pair or not pair.get("old"):
        raise ValueError("未找到对应的旧教材课时，请先在本册建设运行粗分并确认课对")
    old_les = Lesson.query.filter_by(lesson_uid=pair["old"]["lesson_uid"]).first()
    if not old_les:
        raise ValueError("未找到对应的旧教材课时")
    return old_les, str(pair.get("match_method") or "index")


def build_lesson_view_workspace(
    *,
    old_code: str,
    new_code: str,
    new_lesson_uid: str,
    page_index: int = 1,
) -> dict:
    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    new_les = _resolve_diff_lesson(new_lesson_uid, "diff_new")

    from .xiaoke_view import (
        ensure_xiaoke_new_page_ranges,
        is_xiaoke_volume,
        old_lesson_missing_pages_message,
        old_pdf_render_info,
    )

    # 小科：进入对比时若新侧尚无页码，先划分再拉图
    if is_xiaoke_volume(old_vol) and not new_les.page_start:
        ensure_xiaoke_new_page_ranges(new_code=new_code)
        new_les = _resolve_diff_lesson(new_lesson_uid, "diff_new")

    old_les, match_method = _resolve_old_lesson_for_new(
        old_vol=old_vol, new_vol=new_vol, new_les=new_les
    )
    stored = DiffLessonPair.query.filter_by(
        old_volume_id=old_vol.id,
        new_volume_id=new_vol.id,
        new_lesson_id=new_les.id,
    ).first()
    pair_status = (stored.pair_status if stored else None) or "suggested"

    old_pages_hint = ""
    if is_xiaoke_volume(old_vol):
        old_pages_hint = old_lesson_missing_pages_message(old_les) or ""
        if not new_les.page_start:
            raise ValueError(
                "新侧课时尚无页码范围。请确认已上传完整 PDF，"
                "且目录含印刷页码后重试「进入对比」（将自动划分页码）。"
            )
    elif not new_les.page_start or not old_les.page_start:
        raise ValueError("课时尚无页码范围，请先在 intake 完成解析")

    new_ps = int(new_les.page_start)
    new_pe = int(new_les.page_end or new_les.page_start)
    new_page_count = max(1, new_pe - new_ps + 1)
    if old_les.page_start:
        old_ps = int(old_les.page_start)
        old_pe = int(old_les.page_end or old_les.page_start)
        old_page_count = max(1, old_pe - old_ps + 1)
    else:
        old_ps = None
        old_pe = None
        old_page_count = 0
    pi = max(1, min(page_index, new_page_count))
    old_available = pi <= old_page_count
    old_pi = pi if old_available else None

    new_pdf_page = new_ps + pi - 1
    old_pdf_page = (old_ps + old_pi - 1) if old_available else None

    old_render = old_pdf_render_info(old_les, fallback_old_code=old_code)

    old_pdf_cache, new_pdf_cache = _pdf_cache_tokens(
        old_volume_code=old_render["volume_code"],
        new_vol=new_vol,
    )

    page_image_urls: list[dict] = []
    for i in range(1, new_page_count + 1):
        has_old = i <= old_page_count
        op = (old_ps + i - 1) if has_old else None
        np = new_ps + i - 1
        page_image_urls.append(
            {
                "page_index": i,
                "old_page": op,
                "new_page": np,
                "old_page_available": has_old,
                "old_url": (
                    _prefer_page_url(
                        volume_code=old_render["volume_code"],
                        pdf_page=op,
                        lesson=old_les,
                        pdf_api=old_render["api"],
                        cache_token=old_pdf_cache,
                    )
                    if has_old
                    else ""
                ),
                "new_url": _prefer_page_url(
                    volume_code=new_code,
                    pdf_page=np,
                    lesson=new_les,
                    cache_token=new_pdf_cache,
                ),
            }
        )
    cur = page_image_urls[pi - 1]

    def _atoms_empty(data: dict | None) -> bool:
        if not data:
            return True
        if data.get("old_atoms") or data.get("new_atoms"):
            return False
        if data.get("old_text_ocr_done") or data.get("new_text_ocr_done"):
            return False
        return True

    ocr_old = None
    if is_xiaoke_volume(old_vol):
        from .xiaoke_ocr import assemble_atoms_from_side_caches
        from .xiaoke_view import resolve_xiaoke_ocr_volume_for_old_lesson

        try:
            ocr_old = resolve_xiaoke_ocr_volume_for_old_lesson(old_vol, old_les)
        except ValueError:
            ocr_old = None

    if old_pdf_page:
        atom_data = load_page_atom_cache(
            old_vol=_atom_cache_old_vol(old_vol),
            new_vol=new_vol,
            old_page=old_pdf_page,
            new_page=new_pdf_page,
        )
        if _atoms_empty(atom_data) and is_xiaoke_volume(old_vol):
            atom_data = assemble_atoms_from_side_caches(
                ocr_old_vol=ocr_old,
                new_vol=new_vol,
                old_page=old_pdf_page,
                new_page=new_pdf_page,
            )
        atom_data = atom_data or {
            "old_page": old_pdf_page,
            "new_page": new_pdf_page,
            "old_atoms": [],
            "new_atoms": [],
            "text_ocr_done": False,
            "image_ocr_done": False,
            "summary": {},
        }
    else:
        atom_data = {
            "old_page": None,
            "new_page": new_pdf_page,
            "old_atoms": [],
            "new_atoms": [],
            "text_ocr_done": False,
            "image_ocr_done": False,
            "summary": {},
            "old_page_missing": True,
        }
        if is_xiaoke_volume(old_vol):
            from .xiaoke_ocr import assemble_atoms_from_side_caches

            side = assemble_atoms_from_side_caches(
                ocr_old_vol=None,
                new_vol=new_vol,
                old_page=None,
                new_page=new_pdf_page,
            )
            if side:
                atom_data.update(side)
                atom_data["old_page_missing"] = True

    new_brief = _lesson_brief(new_les) or {}
    old_brief = _lesson_brief(old_les) or {}

    old_shorter = old_page_count < new_page_count
    return {
        "mode": "lesson",
        "title": f"{_lesson_label(new_les)} · 第 {pi} 页",
        "old_volume": {"volume_code": old_code, "display_title": old_vol.display_title},
        "new_volume": {"volume_code": new_code, "display_title": new_vol.display_title},
        "new_lesson": new_brief,
        "old_lesson": old_brief,
        "page_index": pi,
        "page_count": new_page_count,
        "old_page_count": old_page_count,
        "old_page_available": old_available,
        "old_page": old_pdf_page,
        "new_page": new_pdf_page,
        "old_page_url": cur["old_url"],
        "new_page_url": cur["new_url"],
        "page_image_urls": page_image_urls,
        "old_pdf_api": old_render["api"],
        "old_pdf_volume_code": old_render["volume_code"],
        "old_pdf_cache": old_pdf_cache,
        "new_pdf_cache": new_pdf_cache,
        "match_method": match_method,
        "pair_status": pair_status,
        "pair_id": stored.id if stored else None,
        "atoms": atom_data,
        "ocr_hint": (
            old_pages_hint
            or (
                (
                    f"旧课仅 {old_page_count} 页、新课 {new_page_count} 页；"
                    f"第 {old_page_count + 1} 页起旧侧无对应页。"
                )
                if old_shorter
                else "跑完比对后点「确认本课对比」；状态：待对比 → 待确认 → 已确认"
            )
        ),
    }
