"""旧库试点：豆包文字 OCR + 版面插图 OCR（教材 + 课件，与双轨前置同款缓存）。"""

from __future__ import annotations



import logging

from typing import Any



from ....extensions import db

from ....models import CoursewareSlide, LessonPage, TextbookAtom

from ...lesson_lookup import get_lesson_by_uid

from ...llm.slide_layout_extract import (

    load_lesson_slide_layout_cache,

    ocr_lesson_slides_layout,

    slide_layout_cache_complete,

    slide_layout_for_index,

)

from ...llm.slide_text_extract import (

    _slide_fields,

    load_lesson_slide_jobs,

    load_lesson_slide_text_cache,

    ocr_lesson_slides_text,

    persist_slide_text_to_db,

    slide_text_cache_complete,

    slide_text_entry_done,

    slide_text_for_index,

)

from ...new_library.annotate.doubao_textbook_ocr import ocr_lesson_pages_doubao_text

from .atom_lineage import IMAGE_OCR_SCANNED_KEY

from .atoms import extract_atoms_for_page



logger = logging.getLogger(__name__)





def _lesson_page_indices(lesson_id: str) -> list[int]:

    return [

        int(row[0])

        for row in (

            db.session.query(LessonPage.page_index)

            .filter_by(lesson_id=lesson_id)

            .order_by(LessonPage.page_index)

            .all()

        )

    ]





def _lesson_slide_jobs(lesson_id: str) -> list[dict[str, Any]]:
    """课件页快照（dict），避免教材 OCR 期间 session.remove 导致 ORM 游离。"""
    return load_lesson_slide_jobs(lesson_id)





def _textbook_text_complete(lesson_id: str, page_count: int) -> bool:

    if page_count <= 0:

        return False

    text_pages = {

        int(a.page_index)

        for a in TextbookAtom.query.filter_by(lesson_id=lesson_id).all()

        if (a.atom_type or "text").lower() in ("text", "title")

    }

    return len(text_pages) >= page_count





def _textbook_image_complete(

    page_rows: list[LessonPage],

    *,

    lesson_has_tb_images: bool,

    text_complete: bool,

) -> bool:

    page_count = len(page_rows)

    if page_count <= 0:

        return False

    if all(lp.ocr_atoms_json for lp in page_rows):

        return True

    if not lesson_has_tb_images and text_complete:

        return True

    scanned = sum(

        1

        for lp in page_rows

        if (lp.atom_lineage_json or {}).get(IMAGE_OCR_SCANNED_KEY)

    )

    return scanned >= page_count and text_complete





def lesson_ocr_phase_stats(*, lesson_uid: str, book_type: str = "old") -> dict[str, Any]:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    lesson_id = str(les.id)
    page_rows = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    slide_jobs = load_lesson_slide_jobs(lesson_id)
    atoms = TextbookAtom.query.filter_by(lesson_id=lesson_id).all()



    text_pages: set[int] = set()

    image_pages: set[int] = set()

    lesson_has_tb_images = False

    for a in atoms:

        pi = int(a.page_index)

        t = (a.atom_type or "text").lower()

        if t in ("text", "title"):

            text_pages.add(pi)

        elif t == "image":

            image_pages.add(pi)

            lesson_has_tb_images = True



    for lp in page_rows:

        for item in lp.ocr_atoms_json or []:

            if not isinstance(item, dict):

                continue

            if (item.get("atom_type") or item.get("type") or "").strip().lower() == "image":

                lesson_has_tb_images = True

                break



    page_count = len(page_rows)

    slide_count = len(slide_jobs)

    tb_text_complete = page_count > 0 and len(text_pages) >= page_count

    tb_image_complete = _textbook_image_complete(

        page_rows,

        lesson_has_tb_images=lesson_has_tb_images,

        text_complete=tb_text_complete,

    )



    text_cache = load_lesson_slide_text_cache(lesson_uid)

    layout_cache = load_lesson_slide_layout_cache(lesson_uid)

    slides_text_done = 0

    slides_layout_done = 0

    slide_image_regions = 0

    for job in slide_jobs:

        idx, _blob, db_text = _slide_fields(job)

        entry = slide_text_for_index(lesson_uid, idx, cache=text_cache)

        if db_text or slide_text_entry_done(entry):

            slides_text_done += 1

        layout = slide_layout_for_index(lesson_uid, idx, cache=layout_cache)

        if layout and layout.get("layout_scanned"):

            slides_layout_done += 1

            slide_image_regions += int(

                layout.get("region_count") or len(layout.get("regions") or [])

            )



    slide_text_complete = slide_count == 0 or slides_text_done >= slide_count

    slide_layout_complete = slide_count == 0 or slides_layout_done >= slide_count



    return {

        "page_count": page_count,

        "slide_count": slide_count,

        "text_pages_done": len(text_pages),

        "image_pages_done": len(image_pages),

        "slides_text_done": slides_text_done,

        "slides_layout_done": slides_layout_done,

        "slide_image_regions": slide_image_regions,

        "textbook_text_ocr_complete": tb_text_complete,

        "textbook_image_ocr_complete": tb_image_complete,

        "slide_text_ocr_complete": slide_text_complete,

        "slide_image_ocr_complete": slide_layout_complete,

        "text_ocr_complete": tb_text_complete and slide_text_complete,

        "image_ocr_complete": tb_image_complete and slide_layout_complete,

        "lesson_has_images": lesson_has_tb_images,

        "text_atom_count": sum(

            1 for a in atoms if (a.atom_type or "text").lower() in ("text", "title")

        ),

        "image_atom_count": sum(

            1 for a in atoms if (a.atom_type or "").lower() == "image"

        ),

    }





def ocr_old_lesson_text_doubao(

    *,

    lesson_uid: str,

    force_refresh: bool = False,

) -> dict[str, Any]:

    les = get_lesson_by_uid(lesson_uid, book_type="old")

    lesson_id = str(les.id)



    tb_done, tb_total, tb_warnings = ocr_lesson_pages_doubao_text(

        lesson_uid=lesson_uid,

        book_type="old",

        force_refresh=force_refresh,

    )



    # 教材 OCR 每页会 commit/remove session，课件须在之后重新读取
    slide_jobs = load_lesson_slide_jobs(lesson_id)

    slide_warnings: list[str] = []

    slides_with_text = 0

    slides_persisted = 0

    if slide_jobs:

        slide_cache, slide_warnings = ocr_lesson_slides_text(

            lesson_uid=lesson_uid,

            lesson_id=lesson_id,

            force_refresh=force_refresh,

        )

        slides_persisted = persist_slide_text_to_db(

            lesson_id=lesson_id,

            lesson_uid=lesson_uid,

            cache=slide_cache,

        )

        slides_with_text = sum(

            1

            for _k, v in (slide_cache.get("slides") or {}).items()

            if isinstance(v, dict) and (v.get("text") or "").strip()

        )

        db.session.commit()



    stats = lesson_ocr_phase_stats(lesson_uid=lesson_uid)

    warnings = tb_warnings + slide_warnings

    return {

        "ok": True,

        "phase": "text",

        "engine": "doubao_page_text+slide_text",

        "textbook": {

            "pages_done": tb_done,

            "pages_total": tb_total,

        },

        "courseware": {

            "slides_total": len(slide_jobs),

            "slides_with_text": slides_with_text,

            "slides_persisted": slides_persisted,

        },

        "pages_done": tb_done,

        "pages_total": tb_total,

        "warnings": warnings,

        "stats": stats,

    }





def ocr_old_lesson_images(

    *,

    lesson_uid: str,

    force_refresh: bool = False,

) -> dict[str, Any]:

    les = get_lesson_by_uid(lesson_uid, book_type="old")

    lesson_id = str(les.id)

    pages = _lesson_page_indices(lesson_id)

    slide_jobs = _lesson_slide_jobs(lesson_id)

    if not pages and not slide_jobs:

        raise ValueError("本课尚无教材页与课件，请先解析 PDF 并上传 ZIP")



    warnings: list[str] = []

    pre_stats = lesson_ocr_phase_stats(lesson_uid=lesson_uid)
    tb_already_done = bool(pre_stats.get("textbook_image_ocr_complete"))

    tb_done = 0

    for pi in pages:

        has_text = (

            db.session.query(TextbookAtom.id)

            .filter_by(lesson_id=lesson_id, page_index=pi)

            .filter(TextbookAtom.atom_type.in_(("text", "title")))

            .first()

        )

        if not has_text:

            warnings.append(f"教材P{pi}：须先完成文字 OCR")

            continue

        if tb_already_done and not force_refresh:

            tb_done += 1

            continue

        try:

            extract_atoms_for_page(

                lesson_uid=lesson_uid,

                page_index=pi,

                replace_page=False,

                use_ocr=True,

                fill_gaps=True,

                book_type="old",

                ocr_phase="images",

            )

            tb_done += 1

        except ValueError as exc:

            warnings.append(f"教材P{pi}：{exc}")

        except Exception as exc:

            logger.exception("old textbook image OCR P%s failed", pi)

            warnings.append(f"教材P{pi}：{exc}")



    # extract_atoms_for_page 也会 remove session，课件重新读取
    slide_jobs = load_lesson_slide_jobs(lesson_id)

    slide_layout_done = 0

    slide_regions = 0

    if slide_jobs:

        if not slide_text_cache_complete(lesson_uid, slide_jobs):

            warnings.append("部分课件页尚未完成文字 OCR，仍继续跑插图版面识别")

        layout_cache, slide_warnings = ocr_lesson_slides_layout(

            lesson_uid=lesson_uid,

            lesson_id=lesson_id,

            force_refresh=force_refresh,

        )

        warnings.extend(slide_warnings)

        for job in slide_jobs:

            idx, _, _ = _slide_fields(job)

            entry = slide_layout_for_index(lesson_uid, idx, cache=layout_cache)

            if entry and entry.get("layout_scanned"):

                slide_layout_done += 1

                slide_regions += int(

                    entry.get("region_count") or len(entry.get("regions") or [])

                )



    db.session.commit()

    stats = lesson_ocr_phase_stats(lesson_uid=lesson_uid)

    return {

        "ok": True,

        "phase": "images",

        "engine": "opencv+doubao_layout+slide_layout",

        "textbook": {

            "pages_done": tb_done,

            "pages_total": len(pages),

        },

        "courseware": {

            "slides_total": len(slide_jobs),

            "slides_layout_done": slide_layout_done,

            "image_regions": slide_regions,

        },

        "pages_done": tb_done,

        "pages_total": len(pages),

        "warnings": warnings,

        "stats": stats,

    }





def ocr_old_lesson_split_pipeline(

    *,

    lesson_uid: str,

    force_text: bool = False,

) -> dict[str, Any]:

    text_out = ocr_old_lesson_text_doubao(

        lesson_uid=lesson_uid,

        force_refresh=force_text,

    )

    img_out = ocr_old_lesson_images(lesson_uid=lesson_uid)

    return {

        "ok": True,

        "text": text_out,

        "images": img_out,

        "warnings": (text_out.get("warnings") or []) + (img_out.get("warnings") or []),

        "stats": lesson_ocr_phase_stats(lesson_uid=lesson_uid),

    }





def old_lesson_courseware_ocr_cached(lesson_uid: str) -> dict[str, bool]:

    les = get_lesson_by_uid(lesson_uid, book_type="old")

    slide_jobs = _lesson_slide_jobs(str(les.id))

    return {

        "slide_text_complete": slide_text_cache_complete(lesson_uid, slide_jobs),

        "slide_layout_complete": slide_layout_cache_complete(lesson_uid, slide_jobs),

    }

