"""双轨分层：一块 / 二块 / 对照 / 融合总表（主键 = 新教材 unit_id）。"""
from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from ....models import TextbookAtom
from .dual_track_experiment import (
    _courseware_query_for_block,
    _has_courseware_content,
    _has_textbook_content,
    _is_image_atom,
    _slide_query,
    _split_block_name_sides,
    _textbook_query_for_block,
)
from .dual_track_units import (
    list_lesson_new_units,
    unit_has_experiment_section,
    unit_image_atom_codes,
    unit_query_text,
)
from .seed_from_old_page import (
    _atom_plain_text,
    _score_new_atom_for_old_block,
)

TB_MATCH_CLASSES = ("match", "new", "delete", "no_match")
CW_ADAPT_STATUSES = ("adapt", "missing", "cw_only")
ILLUSTRATION_TAGS = ("adapt", "missing", "replace", "conflict", "none")
FUSION_ACTIONS = (
    "reuse_as_is",
    "optimize",
    "remake_from_ref",
    "create_new",
    "delete",
)
COMPARE_MARKS = (
    "agree",
    "conflict",
    "unique_new",
    "unique_tb",
    "unique_cw",
)

_ANCILLARY_CW_KEYWORDS = ("封面", "练习", "尾页", "例题页", "名称页", "一起来做练习")
_EXPERIMENT_KEYWORDS = ("探究", "实验", "活动操作", "活动")


def _old_tb_sources(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """旧教材参照源（内部评分用；输出不含 block_code）。"""
    sources: list[dict[str, Any]] = []
    for ob in ctx["old_blocks"]:
        if not _has_textbook_content(ob, ctx["old_atoms_by_code"]):
            continue
        query, y_mid = _textbook_query_for_block(
            ob,
            ctx["old_atoms_by_code"],
            old_lesson_uid=str(ctx["old_les"].lesson_uid),
            prefer_doubao=True,
        )
        if not query:
            continue
        _, tb_label = _split_block_name_sides(ob.block_name or "")
        pages: list[int] = []
        if ob.textbook_page_start is not None:
            pages.append(int(ob.textbook_page_start))
        if ob.textbook_page_end is not None and ob.textbook_page_end != ob.textbook_page_start:
            pages.append(int(ob.textbook_page_end))
        sources.append(
            {
                "query": query,
                "y_mid": y_mid,
                "label": tb_label or (ob.block_name or ""),
                "pages": sorted(set(pages)),
            }
        )
    return sources


def _old_cw_sources(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    slides_by_index = ctx["slides_by_index"]
    for ob in ctx["old_blocks"]:
        if not _has_courseware_content(ob):
            continue
        query = _courseware_query_for_block(ob, slides_by_index)
        if not query:
            continue
        cw_label, _ = _split_block_name_sides(ob.block_name or "")
        slides = [int(x) for x in (ob.course_slide_indices or [])]
        sources.append(
            {
                "query": query,
                "label": cw_label or (ob.block_name or ""),
                "slide_indices": slides,
                "is_ancillary": any(k in (cw_label or "") for k in _ANCILLARY_CW_KEYWORDS),
            }
        )
    if not sources:
        for slide in ctx["slides"]:
            q = _slide_query(slide)
            if not q:
                continue
            idx = int(slide.slide_index)
            sources.append(
                {
                    "query": q,
                    "label": f"课件页{idx}",
                    "slide_indices": [idx],
                    "is_ancillary": any(k in q for k in _ANCILLARY_CW_KEYWORDS),
                }
            )
    return sources


def _best_source_score(
    unit: dict[str, Any],
    sources: list[dict[str, Any]],
    atoms_by_code: dict[str, TextbookAtom],
) -> tuple[dict[str, Any] | None, float]:
    query = unit_query_text(unit, atoms_by_code)
    if not query or not sources:
        return None, 0.0
    best_src: dict[str, Any] | None = None
    best_score = 0.0
    pseudo = TextbookAtom(
        atom_code="__unit__",
        lesson_id="",
        page_index=int(unit.get("page_index") or 1),
        content=query,
        ocr_text=query,
    )
    for src in sources:
        score = _score_new_atom_for_old_block(
            pseudo,
            query_text=src["query"],
            old_y_mid=src.get("y_mid"),
        )
        if score > best_score:
            best_score = score
            best_src = src
    return best_src, best_score


def _three_element_check(
    unit: dict[str, Any],
    atoms_by_code: dict[str, TextbookAtom],
    *,
    matched: bool,
) -> dict[str, bool]:
    codes = unit.get("atom_codes") or []
    has_text = any(
        _atom_plain_text(atoms_by_code[c])
        for c in codes
        if c in atoms_by_code and not _is_image_atom(atoms_by_code[c])
    )
    has_image = bool(unit_image_atom_codes(unit, atoms_by_code))
    has_exp = unit_has_experiment_section(unit) or any(
        any(k in _atom_plain_text(atoms_by_code[c]) for k in _EXPERIMENT_KEYWORDS)
        for c in codes
        if c in atoms_by_code
    )
    return {
        "text": has_text and matched,
        "image": has_image,
        "experiment": has_exp,
    }


def _illustration_tag(
    unit: dict[str, Any],
    atoms_by_code: dict[str, TextbookAtom],
    *,
    score: float,
    min_score: float,
) -> str:
    imgs = unit_image_atom_codes(unit, atoms_by_code)
    if not imgs:
        return "none"
    if score >= min_score:
        return "adapt"
    if score >= min_score * 0.5:
        return "replace"
    if score > 0:
        return "conflict"
    return "missing"


def _experiment_integrity(unit: dict[str, Any], atoms_by_code: dict[str, TextbookAtom]) -> str:
    if not unit_has_experiment_section(unit):
        return "na"
    codes = unit.get("atom_codes") or []
    if len(codes) >= 3:
        return "intact"
    if len(codes) >= 1:
        return "fragmented"
    return "na"


def _text_match_status(score: float, min_score: float) -> str:
    if score >= min_score:
        return "matched"
    if score >= min_score * 0.5:
        return "partial"
    return "none"


def _tb_match_class(score: float, min_score: float, *, has_atoms: bool) -> str:
    if not has_atoms:
        return "delete"
    if score >= min_score:
        return "match"
    if score > 0:
        return "no_match"
    return "new"


def _old_tb_ref_from_profile(prof: dict[str, Any], *, confidence: float) -> dict[str, Any]:
    pages_raw = prof.get("textbook_pages") or []
    pages = sorted({int(p) for p in pages_raw if p is not None})
    return {
        "label": prof.get("label") or prof.get("old_block_name") or "",
        "pages": pages,
        "confidence": round(confidence, 4),
    }


def merge_llm_into_yikuai(
    yikuai: dict[str, Any],
    llm_track: dict[str, Any],
    ctx: dict[str, Any],
    *,
    min_score: float,
) -> dict[str, Any]:
    """豆包按原子分配结果回写一块表（测试页可对比规则 vs 豆包）。"""
    assignments = llm_track.get("assignments") or {}
    if not assignments:
        return yikuai

    atom_to_old: dict[str, str] = {}
    for ob_code, codes in assignments.items():
        for ac in codes or []:
            c = str(ac).strip()
            if c:
                atom_to_old[c] = str(ob_code).strip()

    profiles = {p["block_code"]: p for p in llm_track.get("profiles") or []}
    atoms_by_code = ctx["new_atoms_by_code"]

    for row in yikuai.get("rows") or []:
        codes = row.get("atom_codes") or []
        if not codes:
            continue
        votes: Counter[str] = Counter()
        for ac in codes:
            ob = atom_to_old.get(ac)
            if ob:
                votes[ob] += 1
        if not votes:
            row["ranker"] = "rules"
            continue

        best_ob, hit = votes.most_common(1)[0]
        prof = profiles.get(best_ob)
        coverage = hit / len(codes)
        confidence = max(0.55, min(0.98, 0.55 + coverage * 0.4))
        row["ranker"] = "doubao"
        row["llm_coverage"] = round(coverage, 3)
        row["llm_assigned_atoms"] = hit

        if prof:
            row["old_tb_ref"] = _old_tb_ref_from_profile(prof, confidence=confidence)
            row["match_score"] = round(confidence, 4)
        if coverage >= 0.5:
            row["tb_match_class"] = "match"
            row["text_match_status"] = "matched"
        elif coverage > 0:
            row["tb_match_class"] = "no_match"
            row["text_match_status"] = "partial"
        row["illustration_tag"] = _illustration_tag(
            row,
            atoms_by_code,
            score=confidence,
            min_score=min_score,
        )
        row["three_element_check"] = _three_element_check(
            row,
            atoms_by_code,
            matched=row.get("tb_match_class") == "match",
        )

    yikuai["ranker"] = "doubao"
    return yikuai


def summarize_yikuai_diff(
    rules_table: dict[str, Any],
    merged_table: dict[str, Any],
) -> dict[str, Any]:
    """规则一块 vs 豆包一块：便于测试页展示差异。"""
    rules_by = {r["unit_id"]: r for r in rules_table.get("rows") or []}
    merged_by = {r["unit_id"]: r for r in merged_table.get("rows") or []}
    changed: list[dict[str, Any]] = []
    for uid in sorted(set(rules_by) | set(merged_by)):
        a = rules_by.get(uid) or {}
        b = merged_by.get(uid) or {}
        if (a.get("tb_match_class"), a.get("match_score")) == (
            b.get("tb_match_class"),
            b.get("match_score"),
        ):
            continue
        changed.append(
            {
                "unit_id": uid,
                "section_name": b.get("section_name") or a.get("section_name"),
                "rules_class": a.get("tb_match_class"),
                "rules_score": a.get("match_score"),
                "doubao_class": b.get("tb_match_class"),
                "doubao_score": b.get("match_score"),
            }
        )
    return {
        "changed_units": len(changed),
        "total_units": len(merged_by),
        "samples": changed[:12],
    }


def build_dual_track_bundle(
    ctx: dict[str, Any],
    *,
    min_score: float,
    llm_track: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成一块/二块/对照/融合全套表；可选豆包回写一块。"""
    yikuai_rules = build_yikuai_table(ctx, min_score=min_score)
    yikuai_rules["ranker"] = "rules"
    yikuai = copy.deepcopy(yikuai_rules)

    if llm_track and llm_track.get("ranker") == "doubao":
        merge_llm_into_yikuai(yikuai, llm_track, ctx, min_score=min_score)

    erkuai = build_erkuai_table(ctx, min_score=min_score)
    compare_table = build_unit_compare_table(yikuai, erkuai)
    fusion_table = build_fusion_unit_table(ctx, yikuai, erkuai, compare_table)
    diff = summarize_yikuai_diff(yikuai_rules, yikuai)

    return {
        "yikuai": yikuai,
        "yikuai_rules": yikuai_rules,
        "yikuai_diff": diff,
        "erkuai": erkuai,
        "compare": compare_table,
        "fusion": fusion_table,
    }


def build_yikuai_table(
    ctx: dict[str, Any],
    *,
    min_score: float,
) -> dict[str, Any]:
    """一块：教材轨完整汇总表。"""
    units = list_lesson_new_units(ctx, ensure_prepare=False)
    atoms_by_code = ctx["new_atoms_by_code"]
    sources = _old_tb_sources(ctx)
    rows: list[dict[str, Any]] = []

    for unit in units:
        src, score = _best_source_score(unit, sources, atoms_by_code)
        codes = unit.get("atom_codes") or []
        tb_class = _tb_match_class(score, min_score, has_atoms=bool(codes))
        matched = tb_class == "match"
        rows.append(
            {
                "unit_id": unit["unit_id"],
                "section_name": unit["section_name"],
                "page_index": unit["page_index"],
                "atom_codes": codes,
                "atom_count": len(codes),
                "text_match_status": _text_match_status(score, min_score),
                "experiment_integrity": _experiment_integrity(unit, atoms_by_code),
                "illustration_tag": _illustration_tag(
                    unit, atoms_by_code, score=score, min_score=min_score
                ),
                "three_element_check": _three_element_check(
                    unit, atoms_by_code, matched=matched
                ),
                "tb_match_class": tb_class,
                "match_score": round(score, 4),
                "old_tb_ref": (
                    {
                        "label": src.get("label") or "",
                        "pages": src.get("pages") or [],
                        "confidence": round(score, 4),
                    }
                    if src
                    else None
                ),
            }
        )

    return {
        "table_key": "yikuai",
        "table_label": "一块·教材轨汇总表",
        "primary_key": "unit_id",
        "row_count": len(rows),
        "rows": rows,
    }


def _top_source_scores(
    unit: dict[str, Any],
    sources: list[dict[str, Any]],
    atoms_by_code: dict[str, TextbookAtom],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    query = unit_query_text(unit, atoms_by_code)
    if not query or not sources:
        return []
    pseudo = TextbookAtom(
        atom_code="__unit__",
        lesson_id="",
        page_index=int(unit.get("page_index") or 1),
        content=query,
        ocr_text=query,
    )
    scored: list[tuple[float, dict[str, Any]]] = []
    for src in sources:
        score = _score_new_atom_for_old_block(
            pseudo,
            query_text=src["query"],
            old_y_mid=src.get("y_mid"),
        )
        scored.append((score, src))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[dict[str, Any]] = []
    for score, src in scored[:limit]:
        out.append(
            {
                "label": src.get("label") or "",
                "score": round(score, 4),
                "slide_indices": src.get("slide_indices") or src.get("pages") or [],
                "is_ancillary": bool(src.get("is_ancillary")),
            }
        )
    return out


def _tb_sources_snapshot(ctx: dict[str, Any]) -> dict[str, Any]:
    sources = _old_tb_sources(ctx)
    rows: list[dict[str, Any]] = []
    for src in sources:
        q = (src.get("query") or "")[:120]
        rows.append(
            {
                "label": src.get("label") or "",
                "pages": src.get("pages") or [],
                "query_excerpt": q + ("…" if len(src.get("query") or "") > 120 else ""),
            }
        )
    return {
        "table_key": "tb_sources",
        "table_label": "①-2 旧教材参照源",
        "row_count": len(rows),
        "rows": rows,
        "sources": sources,
    }


def build_tb_units_table(ctx: dict[str, Any]) -> dict[str, Any]:
    """①-1：新教材 unit_id 基准单元。"""
    units = list_lesson_new_units(ctx, ensure_prepare=False)
    rows = [
        {
            "unit_id": u["unit_id"],
            "section_name": u["section_name"],
            "page_index": u["page_index"],
            "atom_count": len(u.get("atom_codes") or []),
            "atom_codes_preview": (u.get("atom_codes") or [])[:5],
        }
        for u in units
    ]
    return {
        "table_key": "tb_units",
        "table_label": "①-1 新教材单元",
        "primary_key": "unit_id",
        "row_count": len(rows),
        "rows": rows,
    }


def build_tb_scores_table(
    ctx: dict[str, Any],
    *,
    min_score: float,
) -> dict[str, Any]:
    """①-3：每 unit 与旧教材源打分。"""
    units = list_lesson_new_units(ctx, ensure_prepare=False)
    atoms_by_code = ctx["new_atoms_by_code"]
    snap = _tb_sources_snapshot(ctx)
    pool = snap["sources"]
    rows: list[dict[str, Any]] = []
    for unit in units:
        query = unit_query_text(unit, atoms_by_code)
        src, score = _best_source_score(unit, pool, atoms_by_code)
        rows.append(
            {
                "unit_id": unit["unit_id"],
                "section_name": unit["section_name"],
                "query_excerpt": (query or "")[:80] + ("…" if len(query or "") > 80 else ""),
                "best_label": (src or {}).get("label") or "—",
                "best_score": round(score, 4),
                "pages": (src or {}).get("pages") or [],
                "above_threshold": score >= min_score,
                "top_candidates": _top_source_scores(unit, pool, atoms_by_code, limit=3),
            }
        )
    return {
        "table_key": "tb_scores",
        "table_label": "①-3 单元↔旧教材打分",
        "min_score": min_score,
        "source_count": snap["row_count"],
        "row_count": len(rows),
        "rows": rows,
    }


def build_tb_classify_table(
    ctx: dict[str, Any],
    *,
    min_score: float,
) -> dict[str, Any]:
    """①-4：教材分类 + 三要素 + 插图（未汇总为一块表）。"""
    yk = build_yikuai_table(ctx, min_score=min_score)
    rows = [
        {
            "unit_id": r["unit_id"],
            "section_name": r["section_name"],
            "tb_match_class": r["tb_match_class"],
            "text_match_status": r["text_match_status"],
            "illustration_tag": r["illustration_tag"],
            "experiment_integrity": r["experiment_integrity"],
            "three_element_check": r["three_element_check"],
            "match_score": r["match_score"],
        }
        for r in yk.get("rows") or []
    ]
    return {
        "table_key": "tb_classify",
        "table_label": "①-4 教材分类判定",
        "row_count": len(rows),
        "rows": rows,
    }


def _cw_sources_snapshot(ctx: dict[str, Any]) -> dict[str, Any]:
    sources = _old_cw_sources(ctx)
    core = [s for s in sources if not s.get("is_ancillary")]
    ancillary = [s for s in sources if s.get("is_ancillary")]
    rows: list[dict[str, Any]] = []
    for src in sources:
        q = (src.get("query") or "")[:120]
        rows.append(
            {
                "label": src.get("label") or "",
                "kind": "附属" if src.get("is_ancillary") else "核心",
                "slide_indices": src.get("slide_indices") or [],
                "query_excerpt": q + ("…" if len(src.get("query") or "") > 120 else ""),
            }
        )
    return {
        "table_key": "cw_sources",
        "table_label": "②-1 旧课件参照源",
        "core_count": len(core),
        "ancillary_count": len(ancillary),
        "row_count": len(rows),
        "rows": rows,
        "core_sources": core,
        "ancillary_sources": ancillary,
    }


def _cw_status_for_score(score: float, min_score: float, slides: list[int]) -> str:
    if score >= min_score and slides:
        return "adapt"
    if score > 0 and slides:
        return "cw_only"
    return "missing"


def build_cw_scores_table(
    ctx: dict[str, Any],
    *,
    min_score: float,
) -> dict[str, Any]:
    """②-2：每 unit 与课件源打分（不生成最终状态）。"""
    units = list_lesson_new_units(ctx, ensure_prepare=False)
    atoms_by_code = ctx["new_atoms_by_code"]
    snap = _cw_sources_snapshot(ctx)
    pool = snap["core_sources"] or _old_cw_sources(ctx)
    rows: list[dict[str, Any]] = []
    for unit in units:
        query = unit_query_text(unit, atoms_by_code)
        src, score = _best_source_score(unit, pool, atoms_by_code)
        rows.append(
            {
                "unit_id": unit["unit_id"],
                "section_name": unit["section_name"],
                "page_index": unit["page_index"],
                "query_excerpt": (query or "")[:80] + ("…" if len(query or "") > 80 else ""),
                "best_label": (src or {}).get("label") or "—",
                "best_score": round(score, 4),
                "slide_indices": list((src or {}).get("slide_indices") or []),
                "above_threshold": score >= min_score,
                "top_candidates": _top_source_scores(unit, pool, atoms_by_code, limit=3),
            }
        )
    return {
        "table_key": "cw_scores",
        "table_label": "②-2 单元↔课件打分",
        "min_score": min_score,
        "source_count": snap["row_count"],
        "core_source_count": snap["core_count"],
        "row_count": len(rows),
        "rows": rows,
    }


def build_cw_illustration_table(
    ctx: dict[str, Any],
    *,
    min_score: float,
) -> dict[str, Any]:
    """②-3：插图冲突标记（独立于适配状态）。"""
    units = list_lesson_new_units(ctx, ensure_prepare=False)
    atoms_by_code = ctx["new_atoms_by_code"]
    pool = _cw_sources_snapshot(ctx)["core_sources"] or _old_cw_sources(ctx)
    rows: list[dict[str, Any]] = []
    for unit in units:
        src, score = _best_source_score(unit, pool, atoms_by_code)
        imgs = unit_image_atom_codes(unit, atoms_by_code)
        ill_conflict = bool(imgs) and score > 0 and score < min_score
        tag = _illustration_tag(unit, atoms_by_code, score=score, min_score=min_score)
        rows.append(
            {
                "unit_id": unit["unit_id"],
                "section_name": unit["section_name"],
                "image_atom_count": len(imgs),
                "image_atom_codes": imgs[:5],
                "illustration_tag": tag,
                "illustration_conflict": ill_conflict,
                "match_score": round(score, 4),
                "best_cw_label": (src or {}).get("label") or "—",
            }
        )
    conflict_count = sum(1 for r in rows if r["illustration_conflict"])
    return {
        "table_key": "cw_illustration",
        "table_label": "②-3 插图冲突标记",
        "row_count": len(rows),
        "conflict_count": conflict_count,
        "rows": rows,
    }


def build_cw_status_table(
    ctx: dict[str, Any],
    *,
    min_score: float,
) -> dict[str, Any]:
    """②-4：课件适配状态判定（adapt/missing/cw_only）。"""
    units = list_lesson_new_units(ctx, ensure_prepare=False)
    atoms_by_code = ctx["new_atoms_by_code"]
    pool = _cw_sources_snapshot(ctx)["core_sources"] or _old_cw_sources(ctx)
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for unit in units:
        src, score = _best_source_score(unit, pool, atoms_by_code)
        slides = list((src or {}).get("slide_indices") or [])
        status = _cw_status_for_score(score, min_score, slides)
        counts[status] += 1
        rows.append(
            {
                "unit_id": unit["unit_id"],
                "section_name": unit["section_name"],
                "cw_adapt_status": status,
                "match_score": round(score, 4),
                "slide_indices": slides,
                "best_cw_label": (src or {}).get("label") or "—",
            }
        )
    return {
        "table_key": "cw_status",
        "table_label": "②-4 课件适配状态",
        "row_count": len(rows),
        "summary": dict(counts),
        "rows": rows,
    }


def _explain_compare_mark(
    *,
    tb_class: str,
    cw_status: str,
    ill_tb: str,
    ill_conflict: bool,
) -> tuple[str, str]:
    if tb_class == "new" and cw_status == "missing":
        return "unique_new", "教材侧为新增/无匹配，课件侧也无适配"
    if tb_class == "match" and cw_status == "missing":
        return "unique_tb", "教材侧已匹配，课件侧缺失"
    if cw_status == "cw_only" and tb_class != "match":
        return "unique_cw", "课件侧有内容但教材侧未匹配"
    if (
        tb_class == "match"
        and cw_status == "adapt"
        and not ill_conflict
        and ill_tb not in ("conflict", "missing")
    ):
        return "agree", "教材匹配且课件适配，插图无冲突"
    reasons: list[str] = []
    if tb_class != "match":
        reasons.append(f"教材分类={tb_class}")
    if cw_status != "adapt":
        reasons.append(f"课件状态={cw_status}")
    if ill_conflict:
        reasons.append("课件插图冲突")
    if ill_tb in ("conflict", "missing"):
        reasons.append(f"教材插图={ill_tb}")
    return "conflict", "；".join(reasons) or "两轨结论不一致"


def build_compare_join_table(
    yikuai: dict[str, Any],
    erkuai: dict[str, Any],
) -> dict[str, Any]:
    """③-1：unit_id 左关联一块+二块原始字段。"""
    tb_by_id = {r["unit_id"]: r for r in yikuai.get("rows") or []}
    cw_by_id = {r["unit_id"]: r for r in erkuai.get("rows") or []}
    unit_ids = sorted(set(tb_by_id) | set(cw_by_id))
    rows: list[dict[str, Any]] = []
    for uid in unit_ids:
        tb = tb_by_id.get(uid) or {}
        cw = cw_by_id.get(uid) or {}
        rows.append(
            {
                "unit_id": uid,
                "section_name": tb.get("section_name") or cw.get("section_name") or "",
                "tb_match_class": tb.get("tb_match_class") or "new",
                "cw_adapt_status": cw.get("cw_adapt_status") or "missing",
                "illustration_tag": tb.get("illustration_tag") or "none",
                "illustration_conflict": cw.get("illustration_conflict") or False,
                "tb_match_score": tb.get("match_score"),
                "cw_match_score": cw.get("match_score"),
                "tb_old_label": (tb.get("old_tb_ref") or {}).get("label") or "—",
                "cw_old_label": (cw.get("old_cw_ref") or {}).get("label") or "—",
                "slide_indices": cw.get("slide_indices") or [],
                "atom_count": tb.get("atom_count") or cw.get("atom_count") or 0,
            }
        )
    return {
        "table_key": "cmp_join",
        "table_label": "③-1 unit_id 关联",
        "primary_key": "unit_id",
        "row_count": len(rows),
        "rows": rows,
    }


def build_compare_mark_table(
    yikuai: dict[str, Any],
    erkuai: dict[str, Any],
) -> dict[str, Any]:
    """③-2：对照标记 + 判定依据。"""
    joined = build_compare_join_table(yikuai, erkuai)
    rows: list[dict[str, Any]] = []
    counts = {m: 0 for m in COMPARE_MARKS}
    for row in joined["rows"]:
        mark, reason = _explain_compare_mark(
            tb_class=row["tb_match_class"],
            cw_status=row["cw_adapt_status"],
            ill_tb=row["illustration_tag"],
            ill_conflict=row["illustration_conflict"],
        )
        counts[mark] = counts.get(mark, 0) + 1
        rows.append({**row, "compare_mark": mark, "mark_reason": reason})
    return {
        "table_key": "cmp_mark",
        "table_label": "③-2 对照标记",
        "primary_key": "unit_id",
        "row_count": len(rows),
        "summary": counts,
        "rows": rows,
    }


def build_compare_summary_table(
    yikuai: dict[str, Any],
    erkuai: dict[str, Any],
) -> dict[str, Any]:
    """③-3：对照统计汇总。"""
    marked = build_compare_mark_table(yikuai, erkuai)
    return {
        "table_key": "cmp_summary",
        "table_label": "③-3 对照统计",
        "row_count": marked["row_count"],
        "summary": marked["summary"],
        "rows": [
            {"compare_mark": k, "count": marked["summary"].get(k, 0)}
            for k in COMPARE_MARKS
        ],
    }


def build_erkuai_table(
    ctx: dict[str, Any],
    *,
    min_score: float,
) -> dict[str, Any]:
    """二块：课件轨完整汇总表（与一块同 unit_id 对齐）。"""
    units = list_lesson_new_units(ctx, ensure_prepare=False)
    atoms_by_code = ctx["new_atoms_by_code"]
    snap = _cw_sources_snapshot(ctx)
    pool = snap["core_sources"] or _old_cw_sources(ctx)
    ancillary_rows: list[dict[str, Any]] = []
    for src in snap["ancillary_sources"]:
        ancillary_rows.append(
            {
                "label": src.get("label") or "",
                "slide_indices": src.get("slide_indices") or [],
                "note": "附属课件素材，不混入核心教学单元行",
            }
        )

    rows: list[dict[str, Any]] = []
    for unit in units:
        src, score = _best_source_score(unit, pool, atoms_by_code)
        slides = list(src.get("slide_indices") or []) if src else []
        imgs = unit_image_atom_codes(unit, atoms_by_code)
        cw_status = _cw_status_for_score(score, min_score, slides)
        ill_conflict = bool(imgs) and score > 0 and score < min_score
        rows.append(
            {
                "unit_id": unit["unit_id"],
                "section_name": unit["section_name"],
                "page_index": unit["page_index"],
                "atom_codes": unit.get("atom_codes") or [],
                "atom_count": len(unit.get("atom_codes") or []),
                "slide_indices": slides,
                "illustration_conflict": ill_conflict,
                "ancillary_only": False,
                "cw_adapt_status": cw_status,
                "match_score": round(score, 4),
                "old_cw_ref": (
                    {
                        "label": src.get("label") or "",
                        "slide_indices": slides,
                        "confidence": round(score, 4),
                    }
                    if src
                    else None
                ),
            }
        )

    return {
        "table_key": "erkuai",
        "table_label": "二块·课件轨汇总表",
        "primary_key": "unit_id",
        "row_count": len(rows),
        "rows": rows,
        "ancillary_cw": ancillary_rows,
    }


def build_unit_compare_table(
    yikuai: dict[str, Any],
    erkuai: dict[str, Any],
) -> dict[str, Any]:
    """步骤③-4：完整对照总表。"""
    marked = build_compare_mark_table(yikuai, erkuai)
    return {
        "table_key": "compare",
        "table_label": "两轨对照表",
        "primary_key": "unit_id",
        "row_count": marked["row_count"],
        "rows": marked["rows"],
        "summary": marked["summary"],
    }


def _pick_fusion_action(
    *,
    compare_mark: str,
    tb_class: str,
    cw_status: str,
    ill_tag: str,
    ill_conflict: bool,
) -> str:
    if tb_class == "delete":
        return "delete"
    if compare_mark == "agree":
        return "reuse_as_is"
    if compare_mark == "unique_new":
        return "create_new"
    if ill_conflict or ill_tag == "conflict":
        return "remake_from_ref"
    if compare_mark == "unique_tb" and tb_class == "match":
        return "optimize"
    if compare_mark == "unique_cw" and cw_status == "cw_only":
        return "optimize"
    if compare_mark == "conflict":
        return "remake_from_ref"
    if tb_class == "match" and cw_status == "adapt":
        return "reuse_as_is"
    return "optimize"


def _illustration_reuse_plan(
    *,
    fusion_action: str,
    ill_tag: str,
    ill_conflict: bool,
) -> str:
    if fusion_action == "delete":
        return "单元删除，插图一并移除"
    if fusion_action == "create_new":
        return "新版新增插图需求，标记全新制作配图"
    if ill_conflict or ill_tag == "conflict":
        return "教材插图可保留；课件插图知识点冲突，弃用并重制"
    if ill_tag == "adapt":
        return "教材插图匹配且课件插图适配，可参考保留"
    if ill_tag == "missing":
        return "课件无对应图，标记全新制作配图"
    if ill_tag == "replace":
        return "插图需替换为新版本配套图"
    return "按文字匹配结果处理插图"


def build_fusion_unit_table(
    ctx: dict[str, Any],
    yikuai: dict[str, Any],
    erkuai: dict[str, Any],
    compare: dict[str, Any],
) -> dict[str, Any]:
    """步骤④：每 unit_id 一条融合原子数据。"""
    tb_by_id = {r["unit_id"]: r for r in yikuai.get("rows") or []}
    cw_by_id = {r["unit_id"]: r for r in erkuai.get("rows") or []}
    rows: list[dict[str, Any]] = []

    for cmp_row in compare.get("rows") or []:
        uid = cmp_row["unit_id"]
        tb = tb_by_id.get(uid) or {}
        cw = cw_by_id.get(uid) or {}
        mark = cmp_row.get("compare_mark") or "conflict"
        tb_class = tb.get("tb_match_class") or "new"
        cw_status = cw.get("cw_adapt_status") or "missing"
        ill_tag = tb.get("illustration_tag") or "none"
        ill_conflict = bool(cw.get("illustration_conflict"))

        action = _pick_fusion_action(
            compare_mark=mark,
            tb_class=tb_class,
            cw_status=cw_status,
            ill_tag=ill_tag,
            ill_conflict=ill_conflict,
        )
        reuse = _illustration_reuse_plan(
            fusion_action=action,
            ill_tag=ill_tag,
            ill_conflict=ill_conflict,
        )
        atom_codes = tb.get("atom_codes") or cw.get("atom_codes") or []
        rows.append(
            {
                "unit_id": uid,
                "section_name": tb.get("section_name") or cw.get("section_name") or "",
                "page_index": tb.get("page_index") or cw.get("page_index"),
                "atom_codes": atom_codes,
                "atom_count": len(atom_codes),
                "fusion_action": action,
                "illustration_reuse": reuse,
                "compare_mark": mark,
                "tb_match_class": tb_class,
                "cw_adapt_status": cw_status,
            }
        )

    unassigned = [
        r["unit_id"]
        for r in rows
        if r.get("fusion_action") == "create_new" and not r.get("atom_codes")
    ]

    return {
        "table_key": "fusion",
        "table_label": "融合·标准原子区块总表",
        "primary_key": "unit_id",
        "row_count": len(rows),
        "rows": rows,
        "unassigned_unit_ids": unassigned,
        "ancillary_cw": erkuai.get("ancillary_cw") or [],
    }


def fusion_rows_to_preview_blocks(fusion: dict[str, Any]) -> list[dict]:
    """UI 右侧列表：按 unit_id 展示（不再用旧块 code）。"""
    action_label = {
        "reuse_as_is": "直接沿用",
        "optimize": "优化调整",
        "remake_from_ref": "参考重制",
        "create_new": "全新制作",
        "delete": "完全删除",
    }
    out: list[dict] = []
    for i, row in enumerate(fusion.get("rows") or []):
        uid = row["unit_id"]
        action = row.get("fusion_action") or "optimize"
        out.append(
            {
                "block_code": f"U-{uid}",
                "block_name": row.get("section_name") or uid,
                "atom_codes": row.get("atom_codes") or [],
                "unit_id": uid,
                "source_old_block": None,
                "track": "fusion",
                "badge": action_label.get(action, action),
                "sort_order": i + 1,
                "metadata_json": {
                    "dual_track_preview": True,
                    "track": "fusion",
                    "unit_id": uid,
                    "fusion_action": action,
                    "illustration_reuse": row.get("illustration_reuse"),
                    "compare_mark": row.get("compare_mark"),
                },
            }
        )
    return out


def yikuai_rows_to_preview_blocks(yikuai: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    for i, row in enumerate(yikuai.get("rows") or []):
        uid = row["unit_id"]
        if not row.get("atom_codes"):
            continue
        ranker = row.get("ranker") or "rules"
        rank_label = "豆包" if ranker == "doubao" else "规则"
        out.append(
            {
                "block_code": f"TB-{uid}",
                "block_name": row.get("section_name") or uid,
                "atom_codes": row.get("atom_codes") or [],
                "unit_id": uid,
                "source_old_block": None,
                "track": "textbook",
                "badge": f"{rank_label}·{row.get('tb_match_class', '—')}",
                "sort_order": i + 1,
                "metadata_json": {
                    "dual_track_preview": True,
                    "track": "textbook",
                    "unit_id": uid,
                    "ranker": ranker,
                    "tb_match_class": row.get("tb_match_class"),
                    "illustration_tag": row.get("illustration_tag"),
                },
            }
        )
    return out


_COMPARE_BADGE = {
    "agree": "重合",
    "conflict": "冲突",
    "unique_new": "新课",
    "unique_tb": "仅教材",
    "unique_cw": "仅课件",
}


def compare_rows_to_preview_blocks(
    compare: dict[str, Any],
    yikuai: dict[str, Any],
) -> list[dict]:
    """③ 对照：右侧可点选单元，颜色随 compare_mark。"""
    atoms_by_unit = {
        r["unit_id"]: r.get("atom_codes") or []
        for r in (yikuai.get("rows") or [])
    }
    out: list[dict] = []
    for i, row in enumerate(compare.get("rows") or []):
        uid = row["unit_id"]
        mark = row.get("compare_mark") or "conflict"
        out.append(
            {
                "block_code": f"CMP-{uid}",
                "block_name": row.get("section_name") or uid,
                "atom_codes": atoms_by_unit.get(uid) or [],
                "unit_id": uid,
                "track": "compare",
                "badge": _COMPARE_BADGE.get(mark, mark),
                "sort_order": i + 1,
                "metadata_json": {
                    "dual_track_preview": True,
                    "track": "compare",
                    "unit_id": uid,
                    "compare_mark": mark,
                },
            }
        )
    return out


def erkuai_rows_to_preview_blocks(erkuai: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    for i, row in enumerate(erkuai.get("rows") or []):
        uid = row["unit_id"]
        if not row.get("atom_codes") and row.get("cw_adapt_status") == "missing":
            continue
        out.append(
            {
                "block_code": f"CW-{uid}",
                "block_name": row.get("section_name") or uid,
                "atom_codes": row.get("atom_codes") or [],
                "unit_id": uid,
                "source_old_block": None,
                "track": "courseware",
                "badge": f"课件·{row.get('cw_adapt_status', '—')}",
                "sort_order": i + 1,
                "metadata_json": {
                    "dual_track_preview": True,
                    "track": "courseware",
                    "unit_id": uid,
                    "cw_adapt_status": row.get("cw_adapt_status"),
                },
            }
        )
    return out
