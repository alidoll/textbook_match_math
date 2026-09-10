"""本册建设：册次就绪状态、新建本册、粗分落库。"""
from __future__ import annotations

import re
from typing import Any

from ...extensions import db
from ...models import DiffLessonPair, Lesson, LessonPage, Volume
from .volumes import diff_volume_detail, get_diff_volume_by_code, paired_diff_volume_code
from .volume_create import (
    create_diff_volume,
    normalize_term,
    subject_label_from_param,
)
from .lesson_match import match_diff_lesson_pairs


def _prefix_for_subject_edition(subject: str, edition: str) -> str:
    sub = (subject or "").strip()
    edi = (edition or "").strip()
    if sub == "化学" and edi == "人教版":
        return "HXRJ"
    if sub == "语文" and edi == "人教版":
        return "YWRJ"
    if sub == "数学" and edi == "冀教版":
        return "SXJJ"
    if sub == "生物" and edi == "人教版":
        return "SWRJ"
    if sub == "科学" and edi == "冀人版":
        return "KXJR"
    if sub == "Test" and edi == "人教版":
        return "TEST"
    if sub == "小科":
        from ..old_library.edition_registry import get_edition, resolve_edition_id
        from .sandbox_subjects import xiaoke_diff_prefix

        if edi in ("人教版", ""):
            return "XKRJ"
        eid = resolve_edition_id(edi)
        if not eid:
            raise ValueError(f"暂不支持的小科版本：{edition}")
        return xiaoke_diff_prefix(get_edition(eid))
    raise ValueError(f"暂不支持的学科/版本：{subject} · {edition}")


def volume_preprocess_status(volume: Volume) -> dict[str, Any]:
    from ..blobs import blob_id_available
    from ..volume_pdf import volume_pdf_blob_fields
    from .draft_page_map import draft_preprocess_fields

    lessons = Lesson.query.filter_by(volume_id=volume.id).all()
    lesson_count = len(lessons)
    ranged = sum(1 for les in lessons if les.page_start and les.page_end)
    page_img = (
        db.session.query(LessonPage.id)
        .join(Lesson, Lesson.id == LessonPage.lesson_id)
        .filter(Lesson.volume_id == volume.id)
        .count()
    )
    has_pdf = blob_id_available(volume.blob_id)
    pdf_fields = volume_pdf_blob_fields(volume)
    has_preview = bool(pdf_fields.get("has_preview_pdf"))
    draft_only = (not has_pdf) and has_preview
    draft = draft_preprocess_fields(volume)
    draft_mapped = int(draft.get("draft_page_mapped") or 0)
    draft_pages_ready = bool(draft.get("draft_pages_ready"))
    if draft_only:
        # 修订版粗分看页码/页图，不依赖脏课时目录
        ready_for_coarse = draft_pages_ready or draft_mapped > 0
    else:
        ready_for_coarse = lesson_count > 0 and ranged > 0
    return {
        "volume_code": volume.volume_code,
        "display_title": volume.display_title,
        "version_label": getattr(volume, "version_label", None),
        "book_type": volume.book_type,
        "parse_status": volume.parse_status,
        "has_pdf": has_pdf,
        "pdf_blob_missing": bool(volume.blob_id) and not has_pdf,
        "has_preview_pdf": has_preview,
        "draft_only": draft_only,
        "preprocess_mode": "draft" if draft_only else "full",
        "lesson_count": lesson_count,
        "lessons_with_page_range": ranged,
        "page_image_count": int(page_img),
        "catalog_ready": lesson_count > 0,
        "page_range_ready": lesson_count > 0 and ranged >= lesson_count,
        "page_images_ready": page_img > 0,
        "ready_for_coarse": ready_for_coarse,
        "draft_page_mapped": draft_mapped,
        "draft_page_count": draft.get("draft_page_count") or 0,
        "draft_pages_ready": draft_pages_ready,
        "draft_pages_count": draft.get("draft_pages_count") or 0,
    }


def _printed_page_from_pair(pair: dict[str, Any], draft_row: dict[str, Any] | None) -> int | None:
    if draft_row and draft_row.get("logical_page") is not None:
        try:
            return int(draft_row["logical_page"])
        except (TypeError, ValueError):
            pass
    note = str(pair.get("note") or "")
    m = re.search(r"页脚目录\s*p(\d+)", note, flags=re.I)
    if m:
        return int(m.group(1))
    return None


def list_draft_page_pairs(
    *,
    old_vol: Volume,
    new_vol: Volume,
    force_refresh: bool = False,
    build_if_missing: bool = True,
) -> dict[str, Any]:
    """修订版页级粗分：结果来自 preview_compare 缓存，不写 DiffLessonPair。"""
    import json

    from .draft_page_map import get_stored_draft_page_map
    from .preview_compare import (
        _cache_path,
        build_preview_compare_pairs,
        enrich_preview_result,
    )

    def _empty(*, error: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": "draft_pages",
            "pair_count": 0,
            "comparable_count": 0,
            "confirmed_count": 0,
            "items": [],
        }
        if error:
            out["error"] = error
        return out

    preview: dict[str, Any] | None = None
    if force_refresh:
        # 清缓存后走秒级页脚粗分；避免 force_refresh=True 的全书 OCR
        cache_file = _cache_path(old_vol, new_vol, new_pdf_source="draft")
        if cache_file and cache_file.is_file():
            try:
                cache_file.unlink()
            except OSError:
                pass
        preview = build_preview_compare_pairs(
            old_vol=old_vol,
            new_vol=new_vol,
            new_pdf_source="draft",
            force_refresh=False,
        )
    elif not build_if_missing:
        cache_file = _cache_path(old_vol, new_vol, new_pdf_source="draft")
        if not cache_file or not cache_file.is_file():
            return _empty()
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if not cached.get("pairs"):
                return _empty()
            preview = enrich_preview_result(cached)
        except (OSError, json.JSONDecodeError, TypeError):
            return _empty()
    else:
        preview = build_preview_compare_pairs(
            old_vol=old_vol,
            new_vol=new_vol,
            new_pdf_source="draft",
            force_refresh=False,
        )

    if not preview:
        return _empty()

    page_map = get_stored_draft_page_map(new_vol) or {}
    by_pdf: dict[int, dict[str, Any]] = {}
    for row in page_map.get("pages") or []:
        try:
            by_pdf[int(row["pdf_page"])] = row
        except (KeyError, TypeError, ValueError):
            continue

    items: list[dict[str, Any]] = []
    for i, pair in enumerate(preview.get("pairs") or [], start=1):
        new_page = pair.get("new_page")
        draft_row = None
        if new_page is not None:
            try:
                draft_row = by_pdf.get(int(new_page))
            except (TypeError, ValueError):
                draft_row = None
        label = pair.get("label") or (draft_row or {}).get("label") or ""
        compare_url = pair.get("compare_url")
        if compare_url and "from=" not in str(compare_url):
            sep = "&" if "?" in str(compare_url) else "?"
            compare_url = f"{compare_url}{sep}from=workbook"
        items.append(
            {
                "sort_order": int(pair.get("index") or i),
                "new_page": new_page,
                "printed_page": _printed_page_from_pair(pair, draft_row),
                "old_page": pair.get("old_page"),
                "label": label,
                "kind": pair.get("kind"),
                "comparable": bool(pair.get("comparable")),
                "match_method": str(pair.get("match_method") or ""),
                "note": pair.get("note") or "",
                "compare_url": compare_url,
                "old_lesson_label": pair.get("old_lesson_label"),
                "change": pair.get("change"),
                "change_summary": pair.get("change_summary") or "",
                "similarity": pair.get("similarity"),
            }
        )
    comparable = sum(1 for it in items if it["comparable"] and it.get("old_page"))
    summary = preview.get("summary") or {}
    chapter_overview = _draft_chapter_overview(items)
    return {
        "mode": "draft_pages",
        "pair_count": len(items),
        "comparable_count": int(preview.get("comparable_count") or comparable),
        "confirmed_count": 0,
        "items": items,
        "summary": summary,
        "chapter_overview": chapter_overview,
        "build_tier": preview.get("build_tier"),
    }


def _clear_lesson_pairs(*, old_vol_id: str, new_vol_id: str) -> int:
    n = DiffLessonPair.query.filter_by(
        old_volume_id=old_vol_id, new_volume_id=new_vol_id
    ).delete(synchronize_session=False)
    db.session.flush()
    return int(n or 0)


def create_workbook_pair(
    *,
    subject: str,
    edition: str,
    grade: int,
    term: str,
) -> dict[str, Any]:
    """按需新建同册旧+新两行 volumes。"""
    label = subject_label_from_param(subject) or subject.strip()
    if label not in ("化学", "语文", "数学", "生物", "科学", "Test", "小科"):
        raise ValueError("学科须为化学、语文、数学、生物、科学、Test 或 小科")
    term_n = normalize_term(term)
    prefix = _prefix_for_subject_edition(label, edition.strip())
    old = create_diff_volume(
        grade=int(grade), term=term_n, book_type="diff_old", prefix=prefix
    )
    new = create_diff_volume(
        grade=int(grade), term=term_n, book_type="diff_new", prefix=prefix
    )
    return {
        "ok": True,
        "subject": label,
        "edition": edition.strip(),
        "grade": int(grade),
        "semester": term_n,
        "old": old,
        "new": new,
        "old_code": old.get("volume_code"),
        "new_code": new.get("volume_code"),
    }


def _edition_diff_prefix(ed, subject: str) -> str:
    """版本对应的 diff 册次码前缀：小科加 XK* 前缀，其他学科用 registry volume_code_prefix。"""
    if subject == "科学":
        from .sandbox_subjects import xiaoke_diff_prefix

        return xiaoke_diff_prefix(ed)
    return (ed.volume_code_prefix or ed.edition_id.upper()[:8]).strip().upper()


def build_editions_payload(subject: str) -> list[dict[str, Any]]:
    """本册入口：版本 × 册次网格 + 各册预处理状态。

    subject 为 edition_registry 中的 subject 字段值（科学/数学/化学…）。
    has_old_benchmark 的学科额外带旧库目录状态，其余学科只看 diff 旧/新侧。
    """
    from ..old_library.edition_registry import (
        edition_to_api_dict,
        grade_term_pairs_for_edition,
        list_active_editions,
    )
    from ..old_library.volume_codes import make_volume_code
    from .volume_create import make_diff_volume_code
    from .volumes import get_diff_volume_by_code

    editions: list[dict[str, Any]] = []
    for ed in list_active_editions():
        if (ed.subject or "").strip() != subject:
            continue
        prefix = _edition_diff_prefix(ed, subject)
        volumes: list[dict[str, Any]] = []
        for grade, term in grade_term_pairs_for_edition(ed):
            old_code = make_diff_volume_code(
                grade=grade, term=term, book_type="diff_old", prefix=prefix
            )
            new_code = make_diff_volume_code(
                grade=grade, term=term, book_type="diff_new", prefix=prefix
            )
            # 仅 has_old_benchmark 的学科（小科）才读旧库目录；其余学科旧侧走 diff PDF
            use_library = bool(ed.has_old_benchmark)
            library_code = ""
            library_lesson_count = 0
            if use_library:
                # 与旧库工作台 build_editions_payload 完全同一套码
                library_code = make_volume_code(
                    ed, grade=grade, term=term, book_type="old"
                )
                lib_vol = Volume.query.filter_by(
                    volume_code=library_code, book_type="old"
                ).first()
                library_lesson_count = (
                    Lesson.query.filter_by(volume_id=lib_vol.id).count()
                    if lib_vol
                    else 0
                )

            old_vol = None
            new_vol = None
            try:
                old_vol = get_diff_volume_by_code(old_code)
            except ValueError:
                pass
            try:
                new_vol = get_diff_volume_by_code(new_code)
            except ValueError:
                pass
            old_st = volume_preprocess_status(old_vol) if old_vol else None
            new_st = volume_preprocess_status(new_vol) if new_vol else None
            # old_ready：小科看旧库目录；其余学科看 diff 旧侧是否已上传 PDF
            old_ready = (
                library_lesson_count > 0
                if use_library
                else bool((old_st or {}).get("has_pdf"))
            )
            volumes.append(
                {
                    "grade": grade,
                    "term": term,
                    "old_code": old_code,
                    "new_code": new_code,
                    "library_code": library_code,
                    "library_lesson_count": library_lesson_count,
                    "library_catalog_ready": library_lesson_count > 0,
                    "in_db": old_vol is not None and new_vol is not None,
                    "old_lesson_count": (old_st or {}).get("lesson_count") or 0,
                    "new_lesson_count": (new_st or {}).get("lesson_count") or 0,
                    "has_new_pdf": bool((new_st or {}).get("has_pdf")),
                    "has_old_pdf": bool((old_st or {}).get("has_pdf")),
                    "pdf_blob_missing": bool((new_st or {}).get("pdf_blob_missing")),
                    "old_ready": old_ready,
                    "new_ready": bool((new_st or {}).get("ready_for_coarse")),
                }
            )
        payload = edition_to_api_dict(ed)
        payload["diff_prefix"] = prefix
        payload["has_old_benchmark"] = bool(ed.has_old_benchmark)
        payload["volumes"] = volumes
        editions.append(payload)
    return editions


def build_xiaoke_editions_payload() -> list[dict[str, Any]]:
    """小科本册入口（薄封装，向后兼容）。"""
    return build_editions_payload("科学")


def ensure_workbook_pair(
    *,
    subject: str,
    edition_id: str,
    grade: int,
    term: str,
) -> dict[str, Any]:
    """按需新建本册旧+新两行 volumes；不校验旧库基准（数学等学科也可用）。"""
    from ..old_library.edition_registry import EDITIONS, get_edition, list_active_editions

    label = subject_label_from_param(subject) or (subject or "").strip()
    # 前端学科 → edition_registry subject 对齐
    registry_subject = "科学" if label == "小科" else label
    # 先按前端传的 edition_id 直接查；查不到则按学科兜底（前端 seed 的 id 可能与
    # registry edition_id 不一致，如数学 jijiao ≠ shuxue_jijiao）
    try:
        ed = get_edition(edition_id)
    except KeyError:
        eds = [
            e
            for e in list_active_editions()
            if (e.subject or "").strip() == registry_subject
        ]
        if not eds:
            raise ValueError(f"未找到 {label} 学科的已注册版本")
        ed = eds[0]
    if (ed.subject or "").strip() != registry_subject:
        raise ValueError(f"版本 {edition_id} 不属于 {label} 学科")
    return create_workbook_pair(
        subject=label,
        edition=ed.label,
        grade=int(grade),
        term=str(term),
    )


def ensure_xiaoke_workbook_pair(
    *,
    edition_id: str,
    grade: int,
    term: str,
) -> dict[str, Any]:
    from ..old_library.edition_registry import get_edition

    ed = get_edition(edition_id)
    if (ed.subject or "").strip() != "科学" or not ed.has_old_benchmark:
        raise ValueError(f"非小科开放版本：{edition_id}")
    return create_workbook_pair(
        subject="小科",
        edition=ed.label,
        grade=int(grade),
        term=str(term),
    )


def list_workbook_pairs(*, subject: str | None = None) -> list[dict[str, Any]]:
    """按学科列出可建设的新旧册对（以 diff_new 为主键聚合）。

    传入 subject 时只返回该学科；无法解析则返回空列表（禁止串出其它学科册次）。
    """
    q = Volume.query.filter(Volume.book_type == "diff_new")
    raw = (subject or "").strip()
    if raw:
        label = subject_label_from_param(raw)
        if not label:
            return []
        q = q.filter(Volume.subject == label)
    news = q.order_by(Volume.grade, Volume.semester, Volume.created_at.desc()).all()
    out: list[dict[str, Any]] = []
    for new_vol in news:
        try:
            old_code = paired_diff_volume_code(new_vol.volume_code)
            old_vol = get_diff_volume_by_code(old_code)
        except ValueError:
            continue
        out.append(
            {
                "subject": new_vol.subject,
                "edition": new_vol.edition,
                "grade": new_vol.grade,
                "semester": new_vol.semester,
                "old_code": old_vol.volume_code,
                "new_code": new_vol.volume_code,
                "display_title": new_vol.display_title,
                "old": volume_preprocess_status(old_vol),
                "new": volume_preprocess_status(new_vol),
            }
        )
    return out


def workbook_pair_detail(*, old_code: str, new_code: str) -> dict[str, Any]:
    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    if old_vol.id == new_vol.id:
        raise ValueError("对比两侧不能是同一本教材")
    if (
        old_vol.subject != new_vol.subject
        or old_vol.edition != new_vol.edition
        or old_vol.grade != new_vol.grade
        or old_vol.semester != new_vol.semester
    ):
        raise ValueError("新旧册次学科/版本/年级/学期不一致")
    legacy_ok = old_vol.book_type == "diff_old" and new_vol.book_type == "diff_new"
    chem_shelf_ok = (
        (old_vol.subject or "").strip() == "化学"
        and old_vol.book_type in ("diff_old", "diff_new")
        and new_vol.book_type in ("diff_old", "diff_new")
    )
    if not legacy_ok and not chem_shelf_ok:
        raise ValueError("须分别为 diff_old 与 diff_new 册次")

    is_xiaoke = (old_vol.subject or "").strip() == "小科" or str(
        old_vol.volume_code or ""
    ).upper().startswith("XK")
    old_library = None
    old_library_edition = None
    if is_xiaoke:
        from .xiaoke_old_catalog import (
            old_library_catalog_snapshot,
            old_library_edition_catalogs,
        )

        # 只读拉取旧库已有目录，不写库、不复制到 DOLD
        old_library = old_library_catalog_snapshot(old_vol)
        old_library_edition = old_library_edition_catalogs(old_vol)

    old_st = volume_preprocess_status(old_vol)
    new_st = volume_preprocess_status(new_vol)
    if is_xiaoke:
        lib_ready = bool((old_library or {}).get("catalog_ready"))
        old_st = {
            **old_st,
            "catalog_ready": lib_ready,
            "ready_for_coarse": lib_ready,
            "lesson_count": int((old_library or {}).get("lesson_count") or 0),
            "xiaoke_no_old_pdf": True,
            "catalog_source": "old_library_readonly",
        }

    if new_st.get("draft_only") and not is_xiaoke:
        try:
            coarse = list_draft_page_pairs(
                old_vol=old_vol,
                new_vol=new_vol,
                force_refresh=False,
                build_if_missing=False,
            )
        except ValueError as exc:
            coarse = {
                "mode": "draft_pages",
                "pair_count": 0,
                "comparable_count": 0,
                "confirmed_count": 0,
                "items": [],
                "error": str(exc),
            }
    elif new_st.get("draft_only") and is_xiaoke:
        coarse = {
            "mode": "lessons",
            "pair_count": 0,
            "comparable_count": 0,
            "confirmed_count": 0,
            "items": [],
            "error": "小科不使用旧 PDF，修订版页级粗分不可用；请上传新侧完整版后按课时粗分",
        }
    else:
        coarse = list_stored_pairs(
            old_vol_id=old_vol.id,
            new_vol_id=new_vol.id,
            old_code=old_vol.volume_code,
            new_code=new_vol.volume_code,
        )
        coarse = {**coarse, "mode": "lessons"}

    def _side(vol: Volume, st: dict[str, Any]) -> dict[str, Any]:
        detail = diff_volume_detail(vol)
        # 本册页不需要完整 draft_page_map，避免详情 JSON 过重
        detail.pop("draft_page_map", None)
        return {**detail, **st}

    out: dict[str, Any] = {
        "ok": True,
        "subject": old_vol.subject,
        "edition": old_vol.edition,
        "grade": old_vol.grade,
        "semester": old_vol.semester,
        "old_code": old_vol.volume_code,
        "new_code": new_vol.volume_code,
        "old": _side(old_vol, old_st),
        "new": _side(new_vol, new_st),
        "coarse": coarse,
    }
    if is_xiaoke:
        out["old_library"] = old_library
        out["old_library_edition"] = old_library_edition
        # DOLD 本身不落目录；粗分下拉与展示复用旧库课时列表
        lib_lessons = list((old_library or {}).get("lessons") or [])
        if lib_lessons:
            out["old"] = {
                **out["old"],
                "lessons": lib_lessons,
                "lesson_count": int(
                    (old_library or {}).get("lesson_count") or len(lib_lessons)
                ),
            }
    return out


def _soft_text_verdict(verdict: str | None) -> bool:
    """一致 / 仅标点 / 没变化 → 不算章节「有变动」。

    「音标变动注意」不算软结论，需出现在课时行变动统计中。
    """
    v = (verdict or "").strip()
    if not v:
        return True
    if v in ("一致", "仅标点差异", "没变化"):
        return True
    if v == "音标变动注意":
        return False
    if "标点" in v and "文字" not in v and "音标" not in v:
        return True
    return False


def _text_has_pinyin_attention(tc: dict[str, Any] | None) -> bool:
    if not isinstance(tc, dict):
        return False
    if (tc.get("verdict") or "").strip() == "音标变动注意":
        return True
    for row in tc.get("block_rows") or []:
        if isinstance(row, dict) and (row.get("change") or "").strip() == "音标变动注意":
            return True
    return False


def _draft_chapter_overview(items: list[dict[str, Any]]) -> dict[str, Any]:
    """页级粗分：按 label（章节/课名）汇总变动页。"""
    soft = {"基本一致", "一致", "仅标点差异", "没变化"}
    chapters: dict[str, dict[str, Any]] = {}
    for it in items or []:
        if not it.get("comparable"):
            continue
        label = (it.get("label") or "").strip() or (
            f"印刷页 {it.get('printed_page')}" if it.get("printed_page") is not None
            else f"修订 p{it.get('new_page')}"
        )
        ch = chapters.setdefault(
            label,
            {
                "label": label,
                "page_count": 0,
                "changed_pages": 0,
                "by_change": {},
                "pages": [],
            },
        )
        ch["page_count"] += 1
        change = str(it.get("change") or "未知")
        ch["by_change"][change] = int(ch["by_change"].get(change) or 0) + 1
        has_change = change not in soft
        if has_change:
            ch["changed_pages"] += 1
        ch["pages"].append(
            {
                "new_page": it.get("new_page"),
                "old_page": it.get("old_page"),
                "printed_page": it.get("printed_page"),
                "change": change,
                "change_summary": it.get("change_summary") or "",
                "compare_url": it.get("compare_url"),
            }
        )
    chapter_list = sorted(
        chapters.values(),
        key=lambda c: (-int(c.get("changed_pages") or 0), str(c.get("label") or "")),
    )
    changed_chapters = sum(1 for c in chapter_list if int(c.get("changed_pages") or 0) > 0)
    return {
        "chapter_count": len(chapter_list),
        "changed_chapter_count": changed_chapters,
        "chapters": chapter_list,
    }


def _lesson_page_change_stats(
    *,
    compares_by_new_page: dict[int, dict[str, Any]],
    page_start: int | None,
    page_end: int | None,
    page_count_hint: int = 0,
) -> dict[str, Any]:
    """根据 DiffPageCompare 汇总一课的变动页 / 差异块数。"""
    ps = int(page_start or 0)
    pe = int(page_end or page_start or 0)
    pages_total = max(0, pe - ps + 1) if ps else int(page_count_hint or 0)
    pages_compared = 0
    pages_changed = 0
    text_changed_blocks = 0
    pinyin_attention_pages = 0
    changed_page_list: list[dict[str, Any]] = []
    if ps and pe >= ps:
        for pg in range(ps, pe + 1):
            row = compares_by_new_page.get(pg)
            if not row:
                continue
            tc = row.get("text_compare") if isinstance(row.get("text_compare"), dict) else None
            ic = row.get("image_compare") if isinstance(row.get("image_compare"), dict) else None
            if not tc and not ic:
                continue
            pages_compared += 1
            blocks = 0
            text_changed = False
            pinyin_attn = _text_has_pinyin_attention(tc)
            if tc:
                blocks = int((tc.get("summary") or {}).get("changed_blocks") or 0)
                text_changed_blocks += max(0, blocks)
                if not _soft_text_verdict(tc.get("verdict")):
                    text_changed = True
            if pinyin_attn:
                pinyin_attention_pages += 1
            image_changed = False
            if ic:
                iv = str(ic.get("verdict") or "")
                if iv and iv not in ("一致", "没变化") and "差异" in iv:
                    image_changed = True
                elif int((ic.get("summary") or {}).get("changed_count") or 0) > 0:
                    image_changed = True
            if text_changed or image_changed:
                pages_changed += 1
                changed_page_list.append(
                    {
                        "new_page": pg,
                        "text_verdict": (tc or {}).get("verdict"),
                        "image_verdict": (ic or {}).get("verdict"),
                        "changed_blocks": blocks,
                        "pinyin_attention": pinyin_attn,
                    }
                )
    return {
        "pages_total": pages_total,
        "pages_compared": pages_compared,
        "pages_changed": pages_changed,
        "text_changed_blocks": text_changed_blocks,
        "pinyin_attention_pages": pinyin_attention_pages,
        "has_change": pages_changed > 0,
        "has_pinyin_attention": pinyin_attention_pages > 0,
        "changed_pages": changed_page_list[:20],
    }


def _xiaoke_lesson_change_stats(uid: str) -> dict[str, Any] | None:
    """小科整课比对没有页级 DiffPageCompare，用整课缓存填变动列。"""
    if not uid:
        return None
    try:
        from .xiaoke_lesson_compare import compare_brief, load_latest_lesson_compare

        result = load_latest_lesson_compare(uid)
    except Exception:
        return None
    if not result:
        return None
    brief = compare_brief(result) or {}
    verdict = str(brief.get("verdict") or "").strip()
    if not verdict:
        return None
    soft = verdict in ("一致", "没变化", "无文字", "仅标点差异")
    changed_blocks = int(brief.get("changed_blocks") or 0)
    return {
        "pages_compared": 1,
        "pages_changed": 0 if soft else 1,
        "text_changed_blocks": changed_blocks,
        "has_change": (not soft) or changed_blocks > 0,
        "lesson_verdict": verdict,
        "has_pinyin_attention": verdict == "音标变动注意",
    }


def _load_compares_by_new_page(
    *,
    old_vol: Volume,
    new_vol: Volume,
) -> dict[int, dict[str, Any]]:
    from ...models import DiffPageCompare
    from .test_persist import is_test_volume, load_test_local_compares_by_new_page

    out: dict[int, dict[str, Any]] = {}
    # Test：对比只落本地 JSON，变动统计优先读本地（DB 可能为空）
    if is_test_volume(old_vol) or is_test_volume(new_vol):
        local = load_test_local_compares_by_new_page(old_vol, new_vol)
        if local:
            return local
    try:
        rows = DiffPageCompare.query.filter_by(
            old_volume_id=old_vol.id,
            new_volume_id=new_vol.id,
            new_pdf_source="full",
            preview_blob_id="",
        ).all()
    except Exception:
        return out
    for row in rows or []:
        try:
            np = int(row.new_page)
        except (TypeError, ValueError):
            continue
        out[np] = {
            "text_compare": row.text_compare if isinstance(row.text_compare, dict) else None,
            "image_compare": row.image_compare if isinstance(row.image_compare, dict) else None,
        }
    return out


def enrich_lesson_coarse_change_stats(
    coarse: dict[str, Any],
    *,
    old_vol: Volume,
    new_vol: Volume,
) -> dict[str, Any]:
    """为课对课粗分表附加整册比对后的章节变动统计。"""
    items = list(coarse.get("items") or [])
    if not items:
        return {
            **coarse,
            "chapter_overview": {
                "chapter_count": 0,
                "changed_chapter_count": 0,
                "chapters": [],
            },
        }
    compares = _load_compares_by_new_page(old_vol=old_vol, new_vol=new_vol)
    from .test_persist import is_test_volume

    backfill_test_status = is_test_volume(old_vol) and is_test_volume(new_vol)
    enriched: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    backfill_uids: list[str] = []
    for it in items:
        n = it.get("new") or {}
        stats = _lesson_page_change_stats(
            compares_by_new_page=compares,
            page_start=n.get("page_start"),
            page_end=n.get("page_end"),
        )
        if not int(stats.get("pages_compared") or 0):
            xk = _xiaoke_lesson_change_stats(str(n.get("lesson_uid") or "").strip())
            if xk:
                stats = {**stats, **xk}
        # Test 已跑完本地比对但课对仍停在「待对比」时，稍后批量补标「待确认」
        if (
            backfill_test_status
            and int(stats.get("pages_compared") or 0) > 0
            and (it.get("pair_status") or "") == "suggested"
            and n.get("lesson_uid")
        ):
            backfill_uids.append(str(n.get("lesson_uid")))
            it = {**it, "pair_status": "pending_review"}
        row = {**it, "change_stats": stats}
        enriched.append(row)
        label = (
            (n.get("lesson_label") or "").strip()
            or f"{n.get('lesson_no') or ''} {n.get('lesson_name') or ''}".strip()
            or n.get("lesson_uid")
            or "未命名课时"
        )
        unit = (n.get("unit_title") or "").strip()
        chapters.append(
            {
                "label": label,
                "unit_title": unit,
                "lesson_uid": n.get("lesson_uid"),
                "pair_status": it.get("pair_status"),
                "compare_url": it.get("compare_url"),
                "page_count": stats.get("pages_total") or 0,
                "pages_compared": stats.get("pages_compared") or 0,
                "changed_pages": stats.get("pages_changed") or 0,
                "text_changed_blocks": stats.get("text_changed_blocks") or 0,
                "pinyin_attention_pages": stats.get("pinyin_attention_pages") or 0,
                "has_change": bool(stats.get("has_change")),
                "has_pinyin_attention": bool(stats.get("has_pinyin_attention")),
                "pages": stats.get("changed_pages") or [],
            }
        )
    if backfill_uids:
        try:
            uid_set = set(backfill_uids)
            lesson_ids = {
                les.id
                for les in Lesson.query.filter(
                    Lesson.volume_id == new_vol.id,
                    Lesson.lesson_uid.in_(uid_set),
                ).all()
            }
            if lesson_ids:
                rows = DiffLessonPair.query.filter(
                    DiffLessonPair.old_volume_id == old_vol.id,
                    DiffLessonPair.new_volume_id == new_vol.id,
                    DiffLessonPair.new_lesson_id.in_(lesson_ids),
                    DiffLessonPair.pair_status == "suggested",
                ).all()
                for row in rows:
                    if row.old_lesson_id and row.pair_status != "rejected":
                        row.pair_status = "pending_review"
                if rows:
                    db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
    changed_chapter_count = sum(1 for c in chapters if c.get("has_change"))
    pinyin_chapter_count = sum(1 for c in chapters if c.get("has_pinyin_attention"))
    chapters_sorted = sorted(
        chapters,
        key=lambda c: (
            0 if c.get("has_change") else 1,
            -int(c.get("changed_pages") or 0),
            str(c.get("unit_title") or ""),
            str(c.get("label") or ""),
        ),
    )
    return {
        **coarse,
        "items": enriched,
        "chapter_overview": {
            "chapter_count": len(chapters),
            "changed_chapter_count": changed_chapter_count,
            "pinyin_attention_chapter_count": pinyin_chapter_count,
            "pages_changed": sum(int(c.get("changed_pages") or 0) for c in chapters),
            "pinyin_attention_pages": sum(
                int(c.get("pinyin_attention_pages") or 0) for c in chapters
            ),
            "text_changed_blocks": sum(
                int(c.get("text_changed_blocks") or 0) for c in chapters
            ),
            "chapters": chapters_sorted,
        },
    }


def _lesson_label(les: Lesson) -> str:
    """语文园地/附录表等：标题常在 lesson_no，lesson_name 为空。"""
    no = str(les.lesson_no or "").strip()
    name = str(les.lesson_name or "").strip()
    if no and name:
        return f"{no} {name}".strip()
    return name or no or les.lesson_uid


def _lesson_option_label(les: Lesson) -> str:
    """下拉选项：课名 · 单元 · 页码（课名优先露出全称）。"""
    unit = str(les.unit_title or "").strip()
    label = _lesson_label(les)
    ps, pe = les.page_start, les.page_end
    page = ""
    if ps:
        page = f"p{ps}" if not pe or pe == ps else f"p{ps}-{pe}"
    parts = [p for p in (label, unit, page) if p]
    return " · ".join(parts) if parts else les.lesson_uid


def _lesson_brief(
    les: Lesson | None,
    *,
    volume: Volume | None = None,
) -> dict[str, Any] | None:
    if not les:
        return None
    vol = volume or getattr(les, "volume", None)
    option = _lesson_option_label(les)
    grade = getattr(vol, "grade", None) if vol is not None else None
    semester = getattr(vol, "semester", None) if vol is not None else None
    if grade:
        vol_tag = f"{grade}年级{semester or ''}".strip()
        if vol_tag and vol_tag not in option:
            option = f"{option} · {vol_tag}"
    out: dict[str, Any] = {
        "lesson_id": les.id,
        "lesson_uid": les.lesson_uid,
        "lesson_no": les.lesson_no,
        "lesson_name": les.lesson_name,
        "lesson_label": _lesson_label(les),
        "option_label": option,
        "unit_title": les.unit_title,
        "page_start": les.page_start,
        "page_end": les.page_end,
        "old_course_id": getattr(les, "old_course_id", None),
    }
    if grade:
        out["grade"] = grade
        out["semester"] = semester
        out["volume_code"] = getattr(vol, "volume_code", None)
        out["volume_label"] = f"{grade}年级{semester or ''}".strip()
    return out


def list_stored_pairs(
    *,
    old_vol_id: str,
    new_vol_id: str,
    old_code: str | None = None,
    new_code: str | None = None,
    enrich_changes: bool = True,
) -> dict[str, Any]:
    rows = (
        DiffLessonPair.query.filter_by(
            old_volume_id=old_vol_id, new_volume_id=new_vol_id
        )
        .order_by(DiffLessonPair.sort_order, DiffLessonPair.created_at)
        .all()
    )
    old_v = Volume.query.get(old_vol_id)
    new_v = Volume.query.get(new_vol_id)
    if old_code is None or new_code is None:
        old_code = old_v.volume_code if old_v else ""
        new_code = new_v.volume_code if new_v else ""

    lesson_ids = {r.new_lesson_id for r in rows} | {
        r.old_lesson_id for r in rows if r.old_lesson_id
    }
    lessons = {
        les.id: les
        for les in Lesson.query.filter(Lesson.id.in_(lesson_ids)).all()
    } if lesson_ids else {}
    # 跨年级匹配时需册次元数据标年级
    vol_ids = {les.volume_id for les in lessons.values() if getattr(les, "volume_id", None)}
    volumes_by_id = {
        v.id: v for v in Volume.query.filter(Volume.id.in_(vol_ids)).all()
    } if vol_ids else {}
    items = []
    confirmed = 0
    for r in rows:
        if r.pair_status == "confirmed":
            confirmed += 1
        new_les = lessons.get(r.new_lesson_id)
        old_les = lessons.get(r.old_lesson_id) if r.old_lesson_id else None
        old_vol_meta = (
            volumes_by_id.get(old_les.volume_id) if old_les is not None else None
        )
        compare_url = None
        if r.old_lesson_id and r.pair_status != "rejected" and new_les:
            compare_url = (
                f"/textbook-diff/view?old_code={old_code}&new_code={new_code}"
                f"&mode=lesson&new_lesson_uid={new_les.lesson_uid}"
                f"&from=workbook"
            )
        items.append(
            {
                "id": r.id,
                "sort_order": r.sort_order,
                "match_method": r.match_method,
                "pair_status": r.pair_status,
                "new": _lesson_brief(new_les),
                "old": _lesson_brief(old_les, volume=old_vol_meta),
                "compare_url": compare_url,
            }
        )
    coarse = {
        "pair_count": len(items),
        "confirmed_count": confirmed,
        "items": items,
    }
    if enrich_changes and old_v and new_v:
        coarse = enrich_lesson_coarse_change_stats(coarse, old_vol=old_v, new_vol=new_v)
    else:
        coarse = {
            **coarse,
            "chapter_overview": {
                "chapter_count": len(items),
                "changed_chapter_count": 0,
                "chapters": [],
            },
        }
    if old_v and new_v:
        attach_change_advice_to_coarse(coarse, old_vol=old_v, new_vol=new_v)
    return coarse


def run_coarse_match(*, old_code: str, new_code: str, replace: bool = True) -> dict[str, Any]:
    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    is_xiaoke = (getattr(old_vol, "subject", None) or "").strip() == "小科" or str(
        old_vol.volume_code or ""
    ).upper().startswith("XK")
    match_old_vol = old_vol
    if is_xiaoke:
        from .xiaoke_old_catalog import (
            old_library_catalog_snapshot,
            resolve_old_library_volume,
        )

        snap = old_library_catalog_snapshot(old_vol)
        lib_vol = resolve_old_library_volume(old_vol)
        if not snap.get("catalog_ready") or not lib_vol:
            raise ValueError(
                f"旧库尚无该册目录（{snap.get('library_code') or '—'}）。"
                "小科只读拉取，请先在旧库建设载入基准目录后再回来。"
            )
        match_old_vol = lib_vol

    old_st = volume_preprocess_status(old_vol)
    new_st = volume_preprocess_status(new_vol)

    # 新侧仅有修订版：页级粗分（印刷页 → 旧书 PDF 页），不写课对课脏数据
    if new_st.get("draft_only"):
        if is_xiaoke:
            raise ValueError(
                "小科不依赖旧 PDF，暂不支持修订版页级粗分；请上传新侧完整版后按课时粗分"
            )
        if not old_vol.blob_id or not old_st.get("catalog_ready"):
            raise ValueError("请先完成旧侧完整版上传与目录识别后再粗分")
        if not (
            new_st.get("draft_pages_ready")
            or int(new_st.get("draft_page_mapped") or 0) > 0
        ):
            raise ValueError("请先完成新侧修订版「解析印刷页码」与「生成页图」后再粗分")
        cleared = 0
        if replace:
            cleared = _clear_lesson_pairs(
                old_vol_id=old_vol.id, new_vol_id=new_vol.id
            )
            db.session.commit()
        coarse = list_draft_page_pairs(
            old_vol=old_vol, new_vol=new_vol, force_refresh=True
        )
        return {
            "ok": True,
            "mode": "draft_pages",
            "created": int(coarse.get("pair_count") or 0),
            "cleared_lesson_pairs": cleared,
            "coarse": coarse,
        }

    if is_xiaoke:
        if not new_st["catalog_ready"]:
            raise ValueError("请先完成新侧目录识别后再粗分")
    elif not old_st["catalog_ready"] or not new_st["catalog_ready"]:
        raise ValueError("请先完成新旧两侧目录识别（课时拆分）后再粗分")

    # 小科：课名三档匹配（完全 → 同年级相似 → 同社跨年级）；其它学科仍走通用对齐
    if is_xiaoke:
        from .xiaoke_lesson_match import match_xiaoke_lesson_pairs

        suggested = match_xiaoke_lesson_pairs(
            old_vol=match_old_vol,
            new_vol=new_vol,
            diff_old_vol=old_vol,
        )
    else:
        suggested = match_diff_lesson_pairs(old_vol=match_old_vol, new_vol=new_vol)
    if replace:
        _clear_lesson_pairs(old_vol_id=old_vol.id, new_vol_id=new_vol.id)

    created = 0
    for row in suggested:
        new_info = row.get("new") or {}
        old_info = row.get("old")
        new_uid = new_info.get("lesson_uid")
        if not new_uid:
            continue
        new_les = Lesson.query.filter_by(lesson_uid=new_uid).first()
        if not new_les:
            continue
        old_les = None
        if old_info and old_info.get("lesson_uid"):
            old_les = Lesson.query.filter_by(lesson_uid=old_info["lesson_uid"]).first()
        db.session.add(
            DiffLessonPair(
                old_volume_id=old_vol.id,
                new_volume_id=new_vol.id,
                new_lesson_id=new_les.id,
                old_lesson_id=old_les.id if old_les else None,
                match_method=str(row.get("match_method") or "index"),
                pair_status="suggested",
                sort_order=int(row.get("index") or created + 1),
            )
        )
        created += 1
    db.session.commit()
    coarse = list_stored_pairs(
        old_vol_id=old_vol.id,
        new_vol_id=new_vol.id,
        old_code=old_code,
        new_code=new_code,
    )
    return {
        "ok": True,
        "mode": "lessons",
        "created": created,
        "coarse": {**coarse, "mode": "lessons"},
    }


def attach_change_advice_to_coarse(
    coarse: dict[str, Any],
    *,
    old_vol: Volume,
    new_vol: Volume,
) -> dict[str, Any]:
    """给课对表附上改动建议（自动推断或人工选择）。

    改动建议 / 校准是小科专用流程，依赖项目拆分后已删除的 ``xiaoke_*`` 模块。
    数学等非小科学科无此流程，直接返回（本仓库为数学专用，小科分支不会触发）。
    """
    is_xiaoke = (getattr(new_vol, "subject", None) or "").strip() == "小科" or str(
        getattr(new_vol, "volume_code", "") or ""
    ).upper().startswith("XK")
    if not is_xiaoke:
        return coarse
    from .xiaoke_phase_segment import (
        apply_compare_calibration_to_record,
        cached_reuse_advice_is_adopted,
        compute_xiaoke_change_advice,
        load_change_advice_map,
        save_change_advice_map,
    )
    from .xiaoke_view import is_xiaoke_volume

    items = list(coarse.get("items") or [])
    if not items:
        return coarse
    advice_map = load_change_advice_map(old_vol, new_vol)
    dirty = False
    for it in items:
        uid = str((it.get("new") or {}).get("lesson_uid") or "").strip()
        rec = dict(advice_map.get(uid) or {})
        if uid and rec.get("source") != "user" and is_xiaoke_volume(new_vol):
            changes, _has_phases = _phase_changes_for_coarse_item(
                old_vol=old_vol, new_vol=new_vol, item=it
            )
            old_b = new_b = None
            try:
                from .xiaoke_lesson_compare import load_latest_lesson_bundle

                old_b = load_latest_lesson_bundle(uid, "old")
                new_b = load_latest_lesson_bundle(uid, "new")
            except Exception:
                old_b = new_b = None
            rec, flipped = apply_compare_calibration_to_record(
                rec,
                changes=changes,
                old_bundle=old_b,
                new_bundle=new_b,
            )
            if flipped:
                advice_map[uid] = rec
                dirty = True
            elif not cached_reuse_advice_is_adopted(rec):
                computed = compute_xiaoke_change_advice(
                    changes,
                    old_bundle=old_b,
                    new_bundle=new_b,
                    use_llm=False,
                )
                auto = str(computed.get("value") or "")
                # 整册刚写完缓存后，列表若暂时读不到 bundle，不得把建议清空成「无数据」
                if auto and (
                    auto != str(rec.get("auto") or "")
                    or str(rec.get("value") or "") != auto
                    or str(rec.get("method") or "") != str(computed.get("method") or "")
                ):
                    rec["auto"] = auto
                    rec["value"] = auto
                    rec["source"] = "auto"
                    rec["method"] = str(computed.get("method") or "similarity")
                    rec["reason"] = str(computed.get("reason") or "")
                    advice_map[uid] = rec
                    dirty = True
        value = str(rec.get("value") or rec.get("auto") or "")
        it["change_advice"] = value
        it["change_advice_auto"] = str(rec.get("auto") or "")
        it["change_advice_source"] = str(rec.get("source") or "")
        it["change_advice_reason"] = str(rec.get("reason") or "")
    if dirty:
        save_change_advice_map(old_vol, new_vol, advice_map)
    coarse["items"] = items
    return coarse


def _phase_changes_for_coarse_item(
    *,
    old_vol: Volume,
    new_vol: Volume,
    item: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    uid = str((item.get("new") or {}).get("lesson_uid") or "").strip()
    if not uid:
        return [], False
    old_b: dict[str, Any] = {}
    new_b: dict[str, Any] = {}
    try:
        from .xiaoke_ocr import lesson_page_lists, load_lesson_bundle
        from .xiaoke_view import resolve_xiaoke_ocr_volume_for_old_lesson

        _, old_les, old_pages, new_pages = lesson_page_lists(
            old_vol=old_vol, new_vol=new_vol, new_lesson_uid=uid
        )
        ocr_old = resolve_xiaoke_ocr_volume_for_old_lesson(old_vol, old_les)
        old_uid = (old_les.lesson_uid or "").strip() if old_les else ""
        load_kw = dict(
            ocr_old_vol=ocr_old,
            new_vol=new_vol,
            new_lesson_uid=uid,
            old_lesson_uid=old_uid,
            old_pages=old_pages,
            new_pages=new_pages,
        )
        new_b = load_lesson_bundle(side="new", **load_kw) or {}
        old_b = load_lesson_bundle(side="old", **load_kw) or {}
    except Exception:
        old_b, new_b = {}, {}

    def _prefer(exact: dict[str, Any], latest: dict[str, Any] | None) -> dict[str, Any]:
        a = exact or {}
        b = latest or {}
        sa = (len(a.get("phase_changes") or []), len(a.get("phases") or []))
        sb = (len(b.get("phase_changes") or []), len(b.get("phases") or []))
        return a if sa >= sb else b

    try:
        from .xiaoke_lesson_compare import load_latest_lesson_bundle

        new_b = _prefer(new_b, load_latest_lesson_bundle(uid, "new"))
        old_b = _prefer(old_b, load_latest_lesson_bundle(uid, "old"))
    except Exception:
        pass
    from .xiaoke_phase_segment import phase_changes_for_advice

    return phase_changes_for_advice(old_b, new_b)


def update_coarse_pair(
    *,
    pair_id: str,
    old_lesson_uid: str | None = None,
    clear_old: bool = False,
    pair_status: str | None = None,
    change_advice: str | None = None,
) -> dict[str, Any]:
    row = DiffLessonPair.query.get(pair_id)
    if not row:
        raise ValueError("粗分记录不存在")
    if clear_old:
        row.old_lesson_id = None
        row.match_method = "manual"
    elif old_lesson_uid is not None:
        uid = (old_lesson_uid or "").strip()
        if not uid:
            row.old_lesson_id = None
        else:
            les = Lesson.query.filter_by(lesson_uid=uid).first()
            allowed_old_ids = {row.old_volume_id}
            old_v = Volume.query.get(row.old_volume_id)
            if old_v and (old_v.subject or "").strip() == "小科":
                from .xiaoke_old_catalog import resolve_old_library_volume

                lib_v = resolve_old_library_volume(old_v)
                if lib_v:
                    allowed_old_ids.add(lib_v.id)
            if not les or les.volume_id not in allowed_old_ids:
                raise ValueError("旧课时不属于本册旧教材")
            row.old_lesson_id = les.id
        row.match_method = "manual"
    if pair_status is not None:
        st = pair_status.strip()
        if st not in ("suggested", "pending_review", "confirmed", "rejected"):
            raise ValueError("pair_status 无效")
        row.pair_status = st
    if change_advice is not None:
        from .xiaoke_phase_segment import set_user_change_advice

        new_les = Lesson.query.get(row.new_lesson_id)
        old_v = Volume.query.get(row.old_volume_id)
        new_v = Volume.query.get(row.new_volume_id)
        if not new_les or not old_v or not new_v:
            raise ValueError("课对课时不完整")
        set_user_change_advice(
            old_vol=old_v,
            new_vol=new_v,
            new_lesson_uid=new_les.lesson_uid,
            value=change_advice,
        )
    db.session.commit()
    old_code = Volume.query.get(row.old_volume_id)
    new_code = Volume.query.get(row.new_volume_id)
    return {
        "ok": True,
        "coarse": list_stored_pairs(
            old_vol_id=row.old_volume_id,
            new_vol_id=row.new_volume_id,
            old_code=old_code.volume_code if old_code else "",
            new_code=new_code.volume_code if new_code else "",
        ),
    }


def confirm_all_pairs(*, old_code: str, new_code: str) -> dict[str, Any]:
    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    rows = DiffLessonPair.query.filter_by(
        old_volume_id=old_vol.id, new_volume_id=new_vol.id
    ).all()
    n = 0
    for r in rows:
        if r.pair_status == "rejected":
            continue
        if not r.old_lesson_id:
            continue
        # 仅将「待确认」提升为已确认；待对比仍需先跑完比对
        if r.pair_status == "pending_review":
            r.pair_status = "confirmed"
            n += 1
    db.session.commit()
    return {
        "ok": True,
        "confirmed": n,
        "coarse": list_stored_pairs(
            old_vol_id=old_vol.id,
            new_vol_id=new_vol.id,
            old_code=old_code,
            new_code=new_code,
        ),
    }


def _get_pair_row_by_new_lesson(
    *,
    old_code: str,
    new_code: str,
    new_lesson_uid: str,
) -> tuple[DiffLessonPair, Lesson]:
    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    new_les = Lesson.query.filter_by(lesson_uid=(new_lesson_uid or "").strip()).first()
    if not new_les or new_les.volume_id != new_vol.id:
        raise ValueError("未找到新教材课时")
    row = DiffLessonPair.query.filter_by(
        old_volume_id=old_vol.id,
        new_volume_id=new_vol.id,
        new_lesson_id=new_les.id,
    ).first()
    if not row:
        raise ValueError("尚无粗分课对，请先在本册建设运行粗分")
    if not row.old_lesson_id:
        raise ValueError("该课尚无旧教材对应，无法确认")
    if row.pair_status == "rejected":
        raise ValueError("该课对已标记为无对应")
    return row, new_les


def mark_pair_ready_for_review(
    *,
    old_code: str,
    new_code: str,
    new_lesson_uid: str,
) -> dict[str, Any]:
    """比对跑完后进入「待确认」；不覆盖人工已确认。"""
    row, new_les = _get_pair_row_by_new_lesson(
        old_code=old_code, new_code=new_code, new_lesson_uid=new_lesson_uid
    )
    if row.pair_status != "confirmed":
        row.pair_status = "pending_review"
        db.session.commit()
    return {
        "ok": True,
        "pair_id": row.id,
        "pair_status": row.pair_status,
        "new_lesson_uid": new_les.lesson_uid,
    }


def confirm_pair_by_new_lesson(
    *,
    old_code: str,
    new_code: str,
    new_lesson_uid: str,
) -> dict[str, Any]:
    """教研看完对比结果后手工确认课对。"""
    row, new_les = _get_pair_row_by_new_lesson(
        old_code=old_code, new_code=new_code, new_lesson_uid=new_lesson_uid
    )
    row.pair_status = "confirmed"
    db.session.commit()
    return {
        "ok": True,
        "pair_id": row.id,
        "pair_status": row.pair_status,
        "new_lesson_uid": new_les.lesson_uid,
    }
