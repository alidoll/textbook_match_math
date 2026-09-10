"""锚定推荐：按新侧原子文本在同版本旧块库中打分（规则引擎，对齐旧项目 suggest-anchors）。"""
from __future__ import annotations

from ....extensions import db
from ....models import Block, Lesson, TextbookAtom, Volume
from ...lesson_lookup import get_lesson_by_uid
from .workspace import build_new_annotate_workspace

BLOCK_KIND_KEYWORDS: dict[str, list[str]] = {
    "cover": ["课件名称", "课头封面", "封面", "名称页"],
    "intro": ["导入", "情境", "问题情境", "激趣", "引入"],
    "explain": ["知识点", "讲解", "新知", "概念", "讲授"],
    "activity": ["实验", "活动", "探究", "操作", "研究"],
    "practice": ["练习", "典例", "习题", "课堂练习"],
    "summary": ["小结", "归纳", "知识小结", "总结"],
    "end": ["尾页", "结束页", "练习提醒", "结束"],
}

_NAME_KEYWORDS = [
    "导入", "探究", "实验", "观察", "记录", "反思", "总结", "练习",
    "拓展", "引入", "讲解", "讨论", "归纳", "应用", "情境",
]


def _infer_block_kind(name: str) -> str:
    n = (name or "").strip()
    for kind, keywords in BLOCK_KIND_KEYWORDS.items():
        if any(kw in n for kw in keywords):
            return kind
    return "other"


def _kind_match_score(hint_name: str, candidate_name: str) -> float:
    h = _infer_block_kind(hint_name)
    c = _infer_block_kind(candidate_name)
    if h == "other" or c == "other":
        return 0.0
    return 1.0 if h == c else 0.0


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    kw_a = {k for k in _NAME_KEYWORDS if k in a}
    kw_b = {k for k in _NAME_KEYWORDS if k in b}
    kw_overlap = len(kw_a & kw_b)
    kw_union = len(kw_a | kw_b)
    chars_a = set(a.replace(" ", ""))
    chars_b = set(b.replace(" ", ""))
    char_overlap = len(chars_a & chars_b)
    char_union = len(chars_a | chars_b)
    kw_score = kw_overlap / max(kw_union, 1)
    char_score = char_overlap / max(char_union, 1)
    return kw_score * 0.7 + char_score * 0.3


def _text_similarity(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    q = query.replace(" ", "").lower()
    c = candidate.replace(" ", "").lower()
    if not q or not c:
        return 0.0
    if q in c or c in q:
        return 0.85
    qs, cs = set(q), set(c)
    char_score = len(qs & cs) / max(len(qs | cs), 1)
    return char_score * 0.45 + _name_similarity(query, candidate) * 0.55


def _corpus_key(lesson_id: str, block_code: str) -> str:
    return f"{lesson_id}:{block_code}"


def _tb_pages(block: Block, atoms_by_code: dict[str, TextbookAtom]) -> list[int]:
    if block.textbook_page_start is not None:
        start = int(block.textbook_page_start)
        end = int(block.textbook_page_end or start)
        return list(range(start, end + 1))
    pages: set[int] = set()
    for code in block.atom_codes or []:
        atom = atoms_by_code.get(str(code).strip())
        if atom:
            pages.add(int(atom.page_index))
    return sorted(pages)


def _atom_excerpt(
    lesson_id: str, atom_codes: list, *, max_len: int = 80
) -> str:
    if not atom_codes:
        return ""
    codes = [str(c).strip() for c in atom_codes if str(c).strip()][:8]
    rows = TextbookAtom.query.filter(
        TextbookAtom.lesson_id == lesson_id,
        TextbookAtom.atom_code.in_(codes),
    ).all()
    by_code = {a.atom_code: a for a in rows}
    parts: list[str] = []
    for code in codes:
        atom = by_code.get(code)
        if not atom:
            continue
        text = (atom.content or atom.ocr_text or "").strip()
        if text:
            parts.append(text)
    blob = " ".join(parts)
    return blob[:max_len]


def _collect_pre_reform_blocks(
    *,
    edition: str,
    subject: str,
    lesson_ids: list[str] | None = None,
    include_excerpt: bool = True,
) -> list[dict]:
    if lesson_ids:
        lessons = (
            Lesson.query.filter(Lesson.id.in_(list(lesson_ids)))
            .order_by(Lesson.unit_no, Lesson.lesson_no)
            .all()
        )
        vol_ids = {les.volume_id for les in lessons if les.volume_id}
        vol_by_id = {
            v.id: v
            for v in Volume.query.filter(Volume.id.in_(vol_ids)).all()
        }
    else:
        volumes = Volume.query.filter_by(
            edition=edition, subject=subject, book_type="old"
        ).all()
        vol_by_id = {v.id: v for v in volumes}
        vol_ids = list(vol_by_id)
        if not vol_ids:
            return []
        lessons = (
            Lesson.query.filter(Lesson.volume_id.in_(vol_ids))
            .order_by(Lesson.volume_id, Lesson.unit_no, Lesson.lesson_no)
            .all()
        )
    rows: list[dict] = []
    for les in lessons:
        vol = vol_by_id.get(les.volume_id)
        vol_label = vol.display_title if vol else ""
        blocks = (
            Block.query.filter_by(lesson_id=les.id)
            .order_by(Block.sort_order, Block.block_code)
            .all()
        )
        atoms_by_code: dict[str, TextbookAtom] = {}
        if include_excerpt:
            atoms = TextbookAtom.query.filter_by(lesson_id=les.id).all()
            atoms_by_code = {a.atom_code: a for a in atoms}
        for block in blocks:
            old_pgs = (
                _tb_pages(block, atoms_by_code)
                if include_excerpt
                else []
            )
            cw_pgs = sorted(int(x) for x in (block.course_slide_indices or []))
            excerpt = (
                _atom_excerpt(les.id, block.atom_codes or [])
                if include_excerpt
                else ""
            )
            rows.append(
                {
                    "lesson_id": les.id,
                    "lesson_uid": les.lesson_uid,
                    "lesson_no": les.lesson_no,
                    "lesson_name": les.lesson_name,
                    "unit_title": les.unit_title or "",
                    "unit_no": les.unit_no,
                    "volume_id": les.volume_id,
                    "volume_label": vol_label,
                    "edition": edition,
                    "block_id": block.id,
                    "block_code": block.block_code,
                    "block_name": block.block_name,
                    "old_tb_pgs": old_pgs,
                    "cw_pgs": cw_pgs,
                    "atom_count": len(block.atom_codes or []),
                    "excerpt": excerpt,
                    "corpus_key": _corpus_key(les.id, block.block_code),
                }
            )
    return rows


def collect_blocks_for_lessons(
    *,
    lesson_ids: list[str],
    edition: str = "",
    subject: str = "",
    include_excerpt: bool = True,
) -> list[dict]:
    """按旧课 ID 拉块语料（预扫描/锚定用，避免全库扫描）。"""
    if not lesson_ids:
        return []
    return _collect_pre_reform_blocks(
        edition=edition,
        subject=subject,
        lesson_ids=list(lesson_ids),
        include_excerpt=include_excerpt,
    )


def _collect_consumed_anchor_refs(*, edition: str, subject: str) -> dict[str, dict]:
    volumes = Volume.query.filter_by(
        edition=edition, subject=subject, book_type="new"
    ).all()
    vol_ids = [v.id for v in volumes]
    if not vol_ids:
        return {}

    lessons = Lesson.query.filter(Lesson.volume_id.in_(vol_ids)).all()
    consumed: dict[str, dict] = {}
    for les in lessons:
        blocks = Block.query.filter_by(lesson_id=les.id).all()
        for block in blocks:
            refs = (block.metadata_json or {}).get("anchor_old_refs") or []
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                key = str(ref.get("corpus_key") or "").strip()
                if not key:
                    lid = ref.get("old_lesson_id")
                    code = ref.get("old_block_code")
                    if lid and code:
                        key = _corpus_key(str(lid), str(code))
                if not key or key in consumed:
                    continue
                consumed[key] = {
                    "corpus_key": key,
                    "by_lesson_id": les.id,
                    "by_lesson_name": les.lesson_name,
                    "by_new_block_id": block.block_code,
                    "by_new_block_name": block.block_name,
                }
    return consumed


def _enrich_corpus_row(
    row: dict,
    *,
    paired_old_lesson_id: str | None,
    current_new_lesson_id: str,
    consumed: dict[str, dict],
) -> dict:
    key = row.get("corpus_key") or _corpus_key(
        str(row.get("lesson_id") or ""), str(row.get("block_code") or "")
    )
    same_lesson = (
        paired_old_lesson_id is not None
        and str(row.get("lesson_id") or "") == str(paired_old_lesson_id)
    )
    use_info = consumed.get(key)
    locked = False
    locked_reason = ""
    if use_info and str(use_info.get("by_lesson_id") or "") != str(current_new_lesson_id):
        locked = True
        locked_reason = (
            f"已在「{use_info.get('by_lesson_name', '')}」"
            f" {use_info.get('by_new_block_id', '')} 锚定"
        )
    out = dict(row)
    out.update(
        {
            "is_same_lesson": same_lesson,
            "same_lesson_label": "本课" if same_lesson else "",
            "block_kind": _infer_block_kind(str(row.get("block_name") or "")),
            "anchor_locked": locked,
            "anchor_locked_reason": locked_reason,
            "anchor_consumed_by": use_info if use_info else None,
        }
    )
    return out


REASON_LABELS = {
    "same_primary_lesson": "主参照课",
    "same_unit": "同单元",
    "same_block_kind": "同模块类型",
    "cross_lesson": "跨课",
    "used_in_current_lesson": "本课已用",
    "locked_other_lesson": "已被其他课占用",
}


def _format_reason_labels(reasons: list[str]) -> str:
    parts = [REASON_LABELS.get(r, r) for r in reasons if r in REASON_LABELS]
    return " · ".join(parts)


def _score_anchor_suggestion(
    row: dict,
    *,
    query_text: str,
    block_name_hint: str,
    current_new_lesson_id: str,
    new_unit_no: int | None,
) -> tuple[float, list[str]]:
    if row.get("anchor_locked"):
        return 0.0, ["locked_other_lesson"]

    blob = " ".join(
        [
            str(row.get("block_name") or ""),
            str(row.get("lesson_name") or ""),
            str(row.get("unit_title") or ""),
            str(row.get("excerpt") or ""),
        ]
    )
    score = _text_similarity(query_text, blob)
    reasons: list[str] = []

    if row.get("is_same_lesson"):
        score += 0.35
        reasons.append("same_primary_lesson")
    elif new_unit_no is not None and row.get("unit_no") == new_unit_no:
        score += 0.18
        reasons.append("same_unit")
        reasons.append("cross_lesson")
    elif not row.get("is_same_lesson"):
        reasons.append("cross_lesson")

    if block_name_hint:
        if _kind_match_score(block_name_hint, str(row.get("block_name") or "")) >= 1.0:
            score += 0.22
            reasons.append("same_block_kind")

    use_info = row.get("anchor_consumed_by")
    if use_info and str(use_info.get("by_lesson_id") or "") == str(current_new_lesson_id):
        score *= 0.35
        reasons.append("used_in_current_lesson")

    return round(min(1.0, score), 3), reasons


def _lesson_context(lesson_uid: str) -> tuple[Lesson, Volume, str | None]:
    les = get_lesson_by_uid(lesson_uid, book_type="new")
    vol = Volume.query.get(les.volume_id)
    if not vol:
        raise ValueError("课时未关联册次")
    from .pair_review import get_primary_lesson_match

    match = get_primary_lesson_match(les.id)
    paired_old_id = match.old_lesson_id if match else None
    return les, vol, paired_old_id


def list_pre_reform_blocks(
    *,
    lesson_uid: str,
    q: str = "",
    filter_lesson_id: str | None = None,
    limit: int = 500,
) -> dict:
    les, vol, paired_old_id = _lesson_context(lesson_uid)
    consumed = _collect_consumed_anchor_refs(edition=vol.edition, subject=vol.subject)
    rows = [
        _enrich_corpus_row(
            r,
            paired_old_lesson_id=paired_old_id,
            current_new_lesson_id=les.id,
            consumed=consumed,
        )
        for r in _collect_pre_reform_blocks(edition=vol.edition, subject=vol.subject)
    ]
    if filter_lesson_id:
        rows = [r for r in rows if r.get("lesson_id") == filter_lesson_id]

    q_lower = (q or "").strip().lower()
    if q_lower:

        def hit(r: dict) -> bool:
            blob = " ".join(
                [
                    str(r.get("block_name") or ""),
                    str(r.get("lesson_name") or ""),
                    str(r.get("unit_title") or ""),
                    str(r.get("block_code") or ""),
                    str(r.get("excerpt") or ""),
                    str(r.get("volume_label") or ""),
                ]
            ).lower()
            return q_lower in blob

        rows = [r for r in rows if hit(r)]

    rows = rows[: min(limit, 1000)]
    locked_count = sum(1 for r in rows if r.get("anchor_locked"))
    return {
        "ok": True,
        "edition": vol.edition,
        "subject": vol.subject,
        "current_lesson_id": les.id,
        "current_lesson_uid": les.lesson_uid,
        "paired_old_lesson_id": paired_old_id,
        "consumed_anchor_count": len(consumed),
        "locked_excluded_count": locked_count,
        "count": len(rows),
        "blocks": rows,
    }


def suggest_anchors(
    *,
    lesson_uid: str,
    atom_codes: list[str],
    block_name_hint: str = "",
    limit: int = 8,
    search_scope: str = "unit_first",
) -> dict:
    les, vol, paired_old_id = _lesson_context(lesson_uid)
    codes = [str(c).strip() for c in (atom_codes or []) if str(c).strip()]
    atoms = (
        TextbookAtom.query.filter(
            TextbookAtom.lesson_id == les.id,
            TextbookAtom.atom_code.in_(codes),
        ).all()
        if codes
        else []
    )
    by_code = {a.atom_code: a for a in atoms}
    parts: list[str] = []
    for code in codes:
        atom = by_code.get(code)
        if not atom:
            continue
        text = (atom.content or atom.ocr_text or "").strip()
        if text:
            parts.append(text)
    query_text = " ".join(parts).strip()
    if not query_text:
        raise ValueError("请先选择课改后教材原子")

    hint = (block_name_hint or "").strip()
    limit = min(int(limit or 8), 20)
    scope = (search_scope or "unit_first").strip().lower()

    consumed = _collect_consumed_anchor_refs(edition=vol.edition, subject=vol.subject)
    all_rows = [
        _enrich_corpus_row(
            r,
            paired_old_lesson_id=paired_old_id,
            current_new_lesson_id=les.id,
            consumed=consumed,
        )
        for r in _collect_pre_reform_blocks(edition=vol.edition, subject=vol.subject)
    ]
    unit_rows = [r for r in all_rows if r.get("unit_no") == les.unit_no]
    search_rows = unit_rows if scope == "unit" else all_rows
    search_note = "同单元"

    def score_pool(pool: list[dict]) -> list[tuple[float, dict, list[str]]]:
        out: list[tuple[float, dict, list[str]]] = []
        for row in pool:
            score, reasons = _score_anchor_suggestion(
                row,
                query_text=query_text,
                block_name_hint=hint,
                current_new_lesson_id=les.id,
                new_unit_no=les.unit_no,
            )
            if "locked_other_lesson" in reasons:
                continue
            if score >= 0.15:
                out.append((score, row, reasons))
        return out

    scored = score_pool(search_rows)
    if scope == "unit_first" and len(scored) < limit:
        unit_keys = {r.get("corpus_key") for r in unit_rows}
        rest = [r for r in all_rows if r.get("corpus_key") not in unit_keys]
        extra = score_pool(rest)
        seen = {(s[1].get("corpus_key")) for s in scored}
        for item in extra:
            if item[1].get("corpus_key") not in seen:
                scored.append(item)
                seen.add(item[1].get("corpus_key"))
        search_note = "同单元优先，已扩至全册"

    def sort_key(item: tuple[float, dict, list[str]]) -> tuple:
        score, row, reasons = item
        same = 1 if row.get("is_same_lesson") else 0
        unit = 1 if "same_unit" in reasons else 0
        kind = 1 if "same_block_kind" in reasons else 0
        return (
            -same,
            -unit,
            -kind,
            -score,
            str(row.get("lesson_id") or ""),
            str(row.get("block_code") or ""),
        )

    scored.sort(key=sort_key)
    suggestions = []
    for score, row, reasons in scored[:limit]:
        lesson_label = f"{row.get('lesson_no') or ''} {row.get('lesson_name') or ''}".strip()
        suggestions.append(
            {
                **row,
                "score": score,
                "score_reasons": reasons,
                "reason": _format_reason_labels(reasons) or "语义相近",
                "source_lesson_label": lesson_label,
                "is_cross_lesson": not row.get("is_same_lesson"),
            }
        )

    ranker = "rules"
    if suggestions:
        from ...llm.corpus_anchor_suggest import rerank_suggestions_with_llm

        page_index = None
        if atoms:
            page_index = int(atoms[0].page_index)
        new_page = None
        if page_index is not None:
            from ....models import LessonPage

            lp = LessonPage.query.filter_by(
                lesson_id=les.id,
                page_index=page_index,
            ).first()
            if lp:
                new_page = {
                    "page_index": page_index,
                    "blob_id": lp.blob_id,
                }
        pool = scored[: min(15, len(scored))]
        pool_suggestions = []
        for score, row, reasons in pool:
            lesson_label = f"{row.get('lesson_no') or ''} {row.get('lesson_name') or ''}".strip()
            pool_suggestions.append(
                {
                    **row,
                    "score": score,
                    "score_reasons": reasons,
                    "reason": _format_reason_labels(reasons) or "语义相近",
                    "source_lesson_label": lesson_label,
                    "is_cross_lesson": not row.get("is_same_lesson"),
                }
            )
        try:
            reranked, ranker = rerank_suggestions_with_llm(
                pool_suggestions,
                lesson_name=les.lesson_name or "",
                query_preview=query_text,
                block_name_hint=hint,
                new_page=new_page,
            )
            if ranker == "anchor_llm" and reranked:
                seen_keys = {s.get("corpus_key") for s in reranked}
                tail = [s for s in suggestions if s.get("corpus_key") not in seen_keys]
                suggestions = reranked + tail
                suggestions = suggestions[:limit]
        except Exception:
            ranker = "rules"

    return {
        "ok": True,
        "edition": vol.edition,
        "query_preview": query_text[:120],
        "block_name_hint": hint,
        "search_scope": scope,
        "search_note": search_note,
        "ranker": ranker,
        "count": len(suggestions),
        "suggestions": suggestions,
    }


def set_block_anchor_from_suggestion(
    *,
    lesson_uid: str,
    block_code: str,
    suggestion: dict,
) -> dict:
    from ...old_library.annotate.blocks import _ensure_blocks_editable

    from .anchors import corpus_row_to_anchor_ref

    les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(les)
    block = Block.query.filter_by(lesson_id=les.id, block_code=block_code).first()
    if not block:
        raise ValueError(f"未找到区块 {block_code}")
    if suggestion.get("anchor_locked"):
        raise ValueError(suggestion.get("anchor_locked_reason") or "该旧块已被其他课锚定")

    ref = corpus_row_to_anchor_ref(suggestion)
    meta = dict(block.metadata_json or {})
    existing = list(meta.get("anchor_old_refs") or [])
    keys = {
        str(r.get("corpus_key") or "")
        for r in existing
        if isinstance(r, dict)
    }
    if ref["corpus_key"] not in keys:
        existing.append(ref)
    meta["anchor_old_refs"] = existing[:3]
    block.metadata_json = meta
    db.session.commit()
    return build_new_annotate_workspace(lesson_uid=lesson_uid)
