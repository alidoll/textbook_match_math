"""双轨建块实验：教材轨 A、课件轨 B，融合后与现网块对照（只读，不写库）。"""
from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from ....models import Block, CoursewareSlide, Lesson, TextbookAtom
from ...lesson_lookup import get_lesson_by_uid
from ...old_library.annotate.workspace import _is_placeholder_atom
from .pair_review import get_primary_lesson_match
from .dual_track_ctx_snap import build_dual_track_ctx_snap
from .seed_from_old_page import (
    _block_query_and_ymid,
    _score_new_atom_for_old_block,
    _suggest_new_block_name_from_old,
)

_CW_TB_SPLIT = re.compile(r"^课件：(.+?)(?:；教材：(.+))?$|^教材：(.+)$")
_NO_TEXTBOOK = re.compile(r"无对应|无习题|无小结|无练习|无原题")

GRANULARITY_NOTE = (
    "主键为新教材单元 ID（栏目聚类 cluster_id，如 P1-C01）；"
    "一块/二块为全课两张汇总表同行对齐；对照与融合按 unit_id 关联，"
    "不再以旧块 block_code 为纽带。"
)
PRODUCTION_NOTE = (
    "网页现网「运行建块与锚定」走 old_mirror：旧块名含课件+教材混合摘要，"
    "不是双轨融合结果；右侧列表默认展示的是已写入数据库的现网块。"
)

# 得分在 [min_score, WEAK_MATCH_CEILING) 的已分配原子，建议人工复核
WEAK_MATCH_CEILING = 0.22

UNASSIGNED_REASONS: dict[str, str] = {
    "below_threshold": "全场最高分仍低于匹配阈值",
    "no_old_profiles": "本轨无可用旧侧参照单元",
    "empty_atom_text": "原子无 OCR 正文，难以文本匹配",
    "fusion_no_track": "教材轨与课件轨均未达阈值",
    "close_candidates": "多个旧块得分接近，未达明确归属",
}

FOLLOWUP_PIPELINE: list[dict[str, str]] = [
    {
        "step": "list",
        "label": "① 清单",
        "detail": "列出未匹配原子、页码、摘录与最高分旧块候选",
    },
    {
        "step": "classify",
        "label": "② 归类",
        "detail": "below_threshold → 倾向新课新增；empty_atom_text → 插图待命名",
    },
    {
        "step": "cluster_by_page",
        "label": "③ 按页聚类",
        "detail": "同页未匹配原子合并为建议「新课新增」预览块（不写库）",
    },
    {
        "step": "production_seed",
        "label": "④ 现网收口",
        "detail": "正式建块时走 seed_unassigned_atoms_as_new_blocks（按栏目聚类+无锚定新块）",
    },
    {
        "step": "manual",
        "label": "⑤ 人工",
        "detail": "跨课找旧块、手动拖入区块，或调高/调低 min_score 后重跑预览",
    },
]


def _split_block_name_sides(name: str) -> tuple[str, str]:
    """返回 (课件摘要, 教材摘要)，不含前缀。"""
    text = (name or "").strip()
    if not text:
        return "", ""
    m = _CW_TB_SPLIT.match(text)
    if not m:
        return "", text
    if m.group(3):
        return "", m.group(3).strip()
    cw = (m.group(1) or "").strip()
    tb = (m.group(2) or "").strip()
    return cw, tb


def _atom_plain(atom: TextbookAtom) -> str:
    return (atom.content or atom.ocr_text or "").strip()


def _textbook_query_for_block(
    block: Block,
    atoms_by_code: dict[str, TextbookAtom],
    *,
    old_lesson_uid: str | None = None,
    prefer_doubao: bool = True,
) -> tuple[str, float | None]:
    """仅用教材原子 + 块名教材侧构造查询；双轨可优先豆包缓存正文。"""
    from .doubao_old_textbook_cache import doubao_text_for_pages, page_indices_for_block

    _, tb_name = _split_block_name_sides(block.block_name or "")
    parts: list[str] = []
    if tb_name and not _NO_TEXTBOOK.search(tb_name):
        parts.append(tb_name)

    page_indices = page_indices_for_block(block)
    doubao_body = ""
    if prefer_doubao and old_lesson_uid and page_indices:
        doubao_body = doubao_text_for_pages(old_lesson_uid, page_indices)

    ys: list[float] = []
    if doubao_body:
        parts.append(doubao_body)
        for code in block.atom_codes or []:
            atom = atoms_by_code.get(str(code).strip())
            if not atom:
                continue
            from .seed_from_old_page import _bbox_ymid

            ys.append(_bbox_ymid(atom.bbox_json))
    else:
        for code in block.atom_codes or []:
            atom = atoms_by_code.get(str(code).strip())
            if not atom:
                continue
            text = _atom_plain(atom)
            if text:
                parts.append(text)
            from .seed_from_old_page import _bbox_ymid

            ys.append(_bbox_ymid(atom.bbox_json))
    query = " ".join(parts).strip()
    y_mid = sum(ys) / len(ys) if ys else None
    return query, y_mid


def _courseware_query_for_block(
    block: Block,
    slides_by_index: dict[int, CoursewareSlide],
) -> str:
    cw_name, _ = _split_block_name_sides(block.block_name or "")
    parts: list[str] = []
    if cw_name:
        parts.append(cw_name)
    for idx in block.course_slide_indices or []:
        slide = slides_by_index.get(int(idx))
        if not slide:
            continue
        text = (slide.ocr_text or "").strip()
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts).strip()


def _slide_query(slide: CoursewareSlide) -> str:
    return (slide.ocr_text or "").strip()


def _has_textbook_content(block: Block, atoms_by_code: dict[str, TextbookAtom]) -> bool:
    _, tb = _split_block_name_sides(block.block_name or "")
    if tb and not _NO_TEXTBOOK.search(tb):
        return True
    if block.textbook_page_start is not None:
        return True
    for code in block.atom_codes or []:
        if atoms_by_code.get(str(code).strip()):
            return True
    return False


def _has_courseware_content(block: Block) -> bool:
    if block.course_slide_indices:
        return True
    cw, _ = _split_block_name_sides(block.block_name or "")
    return bool(cw)


def _is_image_atom(atom: TextbookAtom) -> bool:
    if (atom.atom_type or "").strip().lower() == "image":
        return True
    text = _atom_plain(atom)
    return bool(text and text.startswith("["))


def _atom_snapshot(atom: TextbookAtom) -> dict[str, Any]:
    text = _atom_plain(atom)
    excerpt = re.sub(r"\s+", " ", text).strip()[:72]
    if len(text.strip()) > 72:
        excerpt += "…"
    return {
        "atom_code": atom.atom_code,
        "page_index": int(atom.page_index),
        "atom_type": (atom.atom_type or "").strip() or "text",
        "excerpt": excerpt or "(无 OCR 正文)",
        "is_image": _is_image_atom(atom),
    }


def _rank_atom_candidates(
    atom: TextbookAtom,
    profiles: list[dict],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for prof in profiles:
        score = _score_new_atom_for_old_block(
            atom,
            query_text=prof["query"],
            old_y_mid=prof.get("y_mid"),
        )
        if score <= 0:
            continue
        ranked.append(
            {
                "old_block_code": prof["block_code"],
                "label": prof.get("label") or prof["block_code"],
                "score": round(score, 4),
            }
        )
    ranked.sort(key=lambda x: -x["score"])
    return ranked


def _unassigned_reason_code(
    atom: TextbookAtom,
    *,
    best_score: float,
    min_score: float,
    profiles: list[dict],
    ranked: list[dict],
) -> str:
    if not profiles:
        return "no_old_profiles"
    if not _atom_plain(atom) and _is_image_atom(atom):
        return "empty_atom_text"
    if len(ranked) >= 2 and ranked[0]["score"] < min_score:
        gap = ranked[0]["score"] - ranked[1]["score"]
        if gap < 0.02:
            return "close_candidates"
    if best_score < min_score:
        return "below_threshold"
    return "below_threshold"


def _suggested_actions_for_unassigned(
    *,
    reason: str,
    best_score: float,
    min_score: float,
    atom: TextbookAtom,
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if reason in ("below_threshold", "close_candidates", "fusion_no_track"):
        actions.append(
            {
                "action": "new_content_cluster",
                "label": "建议归入「新课新增」块",
                "detail": "按页+栏目与邻近未匹配原子聚类（预览④融合或现网 seed_unassigned）",
            }
        )
    if reason == "empty_atom_text" or _is_image_atom(atom):
        actions.append(
            {
                "action": "image_curate",
                "label": "插图待补标签/整理",
                "detail": "先完成图片 OCR 或 AI 整理命名，再重跑匹配",
            }
        )
    if best_score > 0 and best_score < min_score:
        actions.append(
            {
                "action": "threshold_review",
                "label": f"边界复核（最高 {best_score:.2f} < 阈值 {min_score}）",
                "detail": "可人工确认是否应划入最高分旧块，或维持新课新增",
            }
        )
    actions.append(
        {
            "action": "cross_lesson_search",
            "label": "跨课找旧块",
            "detail": "现网 annotate「跨课找旧块」检索全册旧库",
        }
    )
    actions.append(
        {
            "action": "manual_assign",
            "label": "手动拖入区块",
            "detail": "在右侧现网块编辑中勾选原子创建/并入区块",
        }
    )
    return actions


def _section_hint_on_page(
    page_index: int,
    atoms_by_code: dict[str, TextbookAtom],
) -> str:
    from .seed_from_old_page import _is_section_header_atom

    headers: list[str] = []
    for atom in atoms_by_code.values():
        if int(atom.page_index) != int(page_index):
            continue
        if not _is_section_header_atom(atom):
            continue
        text = _atom_plain(atom)
        if text and text not in headers:
            headers.append(text[:24])
    return headers[0] if headers else ""


def _build_unassigned_followup(
    *,
    leftover_codes: list[str],
    atom_scores: dict[str, list[dict]],
    new_atoms_by_code: dict[str, TextbookAtom],
    profiles: list[dict],
    min_score: float,
    track: str,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for code in leftover_codes:
        atom = new_atoms_by_code.get(code)
        if not atom:
            continue
        ranked = _rank_atom_candidates(atom, profiles)
        best_score = ranked[0]["score"] if ranked else 0.0
        reason = _unassigned_reason_code(
            atom,
            best_score=best_score,
            min_score=min_score,
            profiles=profiles,
            ranked=ranked,
        )
        items.append(
            {
                **_atom_snapshot(atom),
                "reason": reason,
                "reason_label": UNASSIGNED_REASONS.get(reason, reason),
                "best_score": round(best_score, 4),
                "min_score": min_score,
                "top_candidates": ranked[:3],
                "suggested_actions": _suggested_actions_for_unassigned(
                    reason=reason,
                    best_score=best_score,
                    min_score=min_score,
                    atom=atom,
                ),
            }
        )

    by_page: dict[int, list[dict]] = defaultdict(list)
    for item in items:
        by_page[int(item["page_index"])].append(item)

    page_clusters: list[dict[str, Any]] = []
    for pg in sorted(by_page.keys()):
        pg_items = by_page[pg]
        codes = [x["atom_code"] for x in pg_items]
        section = _section_hint_on_page(pg, new_atoms_by_code)
        name = section or f"第{pg}页新课内容"
        page_clusters.append(
            {
                "page_index": pg,
                "suggested_block_name": name[:64],
                "atom_codes": codes,
                "atom_count": len(codes),
                "track": track,
                "badge": "新课新增·预览",
            }
        )

    return {
        "track": track,
        "count": len(items),
        "items": items,
        "by_page": [
            {"page_index": pg, "count": len(by_page[pg]), "atom_codes": [x["atom_code"] for x in by_page[pg]]}
            for pg in sorted(by_page.keys())
        ],
        "suggested_new_blocks": page_clusters,
        "pipeline": FOLLOWUP_PIPELINE,
    }


def _build_borderline_followup(
    *,
    assignments: dict[str, list[str]],
    profiles: list[dict],
    new_atoms_by_code: dict[str, TextbookAtom],
    min_score: float,
    track: str,
) -> dict[str, Any]:
    """已分配但得分偏低、建议人工复核的原子。"""
    prof_by_code = {p["block_code"]: p for p in profiles}
    items: list[dict[str, Any]] = []
    for old_code, codes in assignments.items():
        prof = prof_by_code.get(old_code)
        if not prof:
            continue
        for ac in codes:
            atom = new_atoms_by_code.get(ac)
            if not atom:
                continue
            score = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof.get("y_mid"),
            )
            if min_score <= score < WEAK_MATCH_CEILING:
                items.append(
                    {
                        **_atom_snapshot(atom),
                        "assigned_old_block": old_code,
                        "score": round(score, 4),
                        "note": f"已划入 {old_code}，但得分偏低，建议复核",
                    }
                )
    return {
        "track": track,
        "ceiling": WEAK_MATCH_CEILING,
        "count": len(items),
        "items": items,
    }


def _build_fusion_unassigned_followup(
    fusion: dict,
    track_a: dict,
    track_b: dict,
    new_atoms_by_code: dict[str, TextbookAtom],
    *,
    min_score: float,
) -> dict[str, Any]:
    unassigned_codes = fusion.get("unassigned_atoms") or []
    items: list[dict[str, Any]] = []
    a_profiles = track_a.get("profiles") or []
    b_profiles = track_b.get("profiles") or []
    fused_atoms = fusion.get("fused_atoms") or {}

    for code in unassigned_codes:
        atom = new_atoms_by_code.get(code)
        if not atom:
            continue
        meta = fused_atoms.get(code) or {}
        tb_score = float(meta.get("textbook_score") or 0)
        cw_score = float(meta.get("courseware_score") or 0)
        best_score = max(tb_score, cw_score)
        ranked_tb = _rank_atom_candidates(atom, a_profiles)[:2]
        ranked_cw = _rank_atom_candidates(atom, b_profiles)[:2]
        reason = "fusion_no_track"
        if not a_profiles and not b_profiles:
            reason = "no_old_profiles"
        elif _is_image_atom(atom) and not _atom_plain(atom):
            reason = "empty_atom_text"
        elif ranked_tb and ranked_cw:
            top_codes = {ranked_tb[0]["old_block_code"], ranked_cw[0]["old_block_code"]}
            if len(top_codes) > 1 and abs(tb_score - cw_score) < 0.02:
                reason = "close_candidates"
        items.append(
            {
                **_atom_snapshot(atom),
                "reason": reason,
                "reason_label": UNASSIGNED_REASONS.get(reason, reason),
                "best_score": round(best_score, 4),
                "textbook_score": round(tb_score, 4),
                "courseware_score": round(cw_score, 4),
                "top_candidates_textbook": ranked_tb,
                "top_candidates_courseware": ranked_cw,
                "suggested_actions": _suggested_actions_for_unassigned(
                    reason=reason,
                    best_score=best_score,
                    min_score=min_score,
                    atom=atom,
                ),
            }
        )

    by_page: dict[int, list[str]] = defaultdict(list)
    for item in items:
        by_page[int(item["page_index"])].append(item["atom_code"])

    page_clusters: list[dict[str, Any]] = []
    for pg in sorted(by_page.keys()):
        codes = by_page[pg]
        section = _section_hint_on_page(pg, new_atoms_by_code)
        page_clusters.append(
            {
                "page_index": pg,
                "suggested_block_name": (section or f"第{pg}页新课内容")[:64],
                "atom_codes": codes,
                "atom_count": len(codes),
                "track": "new_content",
                "badge": "融合·新课新增",
            }
        )

    return {
        "count": len(items),
        "items": items,
        "by_page": [
            {"page_index": pg, "count": len(by_page[pg]), "atom_codes": by_page[pg]}
            for pg in sorted(by_page.keys())
        ],
        "suggested_new_blocks": page_clusters,
        "pipeline": FOLLOWUP_PIPELINE,
        "preview_note": (
            "可将 suggested_new_blocks 视为「新课新增」候选；"
            "现网写入需走建块流水线 seed_unassigned_atoms_as_new_blocks"
        ),
    }


def _assign_track(
    new_atoms: list[TextbookAtom],
    profiles: list[dict],
    *,
    min_score: float,
    new_atoms_by_code: dict[str, TextbookAtom],
) -> tuple[dict[str, list[str]], list[str], dict[str, list[str]]]:
    """规则分配 + 去重，返回 assignments、leftover、atom_scores。"""
    if not profiles:
        return {}, [a.atom_code for a in new_atoms], {}

    assignments: dict[str, list[str]] = {
        p["block_code"]: [] for p in profiles
    }
    leftover: list[str] = []
    for atom in new_atoms:
        best_code = ""
        best_score = 0.0
        for prof in profiles:
            score = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof.get("y_mid"),
            )
            if score > best_score:
                best_score = score
                best_code = prof["block_code"]
        if best_code and best_score >= min_score:
            assignments[best_code].append(atom.atom_code)
        else:
            leftover.append(atom.atom_code)

    # 每原子仅保留最高分旧源（与 seed_from_old_page 去重一致）
    atom_best: dict[str, tuple[str, float]] = {}
    for code, atom_codes in assignments.items():
        prof = next((p for p in profiles if p["block_code"] == code), None)
        if not prof:
            continue
        for ac in atom_codes:
            atom = new_atoms_by_code.get(ac)
            if not atom:
                continue
            score = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof.get("y_mid"),
            )
            prev = atom_best.get(ac)
            if prev is None or score > prev[1]:
                atom_best[ac] = (code, score)
    deduped: dict[str, list[str]] = {p["block_code"]: [] for p in profiles}
    for ac, (code, _) in atom_best.items():
        deduped.setdefault(code, []).append(ac)
    assigned = {ac for codes in deduped.values() for ac in codes}
    leftover = [a.atom_code for a in new_atoms if a.atom_code not in assigned]

    atom_scores: dict[str, list[dict]] = defaultdict(list)
    for atom in new_atoms:
        for prof in profiles:
            score = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof.get("y_mid"),
            )
            if score > 0:
                atom_scores[atom.atom_code].append(
                    {
                        "source": prof["block_code"],
                        "score": round(score, 4),
                    }
                )
        atom_scores[atom.atom_code].sort(key=lambda x: -x["score"])
    return deduped, leftover, dict(atom_scores)


def _candidate_blocks_from_assignments(
    assignments: dict[str, list[str]],
    profiles: list[dict],
    *,
    track: str,
    suggest_name_fn,
) -> list[dict]:
    prof_by_code = {p["block_code"]: p for p in profiles}
    out: list[dict] = []
    for code in sorted(assignments.keys()):
        atoms = assignments.get(code) or []
        prof = prof_by_code.get(code, {})
        label = prof.get("label") or code
        out.append(
            {
                "track": track,
                "source_code": code,
                "source_label": label,
                "suggested_name": suggest_name_fn(prof),
                "atom_codes": atoms,
                "atom_count": len(atoms),
            }
        )
    return out


def _fuse_tracks(
    track_a: dict,
    track_b: dict,
    new_atoms_by_code: dict[str, TextbookAtom],
    *,
    min_score: float,
    tie_epsilon: float = 0.03,
) -> dict:
    """按原子取得分更高的一侧；同分优先教材轨。"""
    a_profiles = {p["block_code"]: p for p in track_a.get("profiles") or []}
    b_profiles = {p["block_code"]: p for p in track_b.get("profiles") or []}
    fused_atoms: dict[str, dict] = {}
    conflicts: list[dict] = []

    for code, atom in new_atoms_by_code.items():
        best_a = 0.0
        best_a_src = ""
        for prof in track_a.get("profiles") or []:
            s = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof.get("y_mid"),
            )
            if s > best_a:
                best_a = s
                best_a_src = prof["block_code"]

        best_b = 0.0
        best_b_src = ""
        for prof in track_b.get("profiles") or []:
            s = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof.get("y_mid"),
            )
            if s > best_b:
                best_b = s
                best_b_src = prof["block_code"]

        winner = "none"
        source = ""
        score = 0.0
        if best_a >= min_score or best_b >= min_score:
            if best_a >= best_b - tie_epsilon:
                winner = "textbook"
                source = best_a_src
                score = best_a
            else:
                winner = "courseware"
                source = best_b_src
                score = best_b

        if (
            best_a >= min_score
            and best_b >= min_score
            and best_a_src != best_b_src
            and abs(best_a - best_b) <= tie_epsilon
        ):
            conflicts.append(
                {
                    "atom_code": code,
                    "textbook_source": best_a_src,
                    "textbook_score": round(best_a, 4),
                    "courseware_source": best_b_src,
                    "courseware_score": round(best_b, 4),
                }
            )

        fused_atoms[code] = {
            "winner": winner,
            "source": source,
            "score": round(score, 4),
            "textbook_score": round(best_a, 4),
            "courseware_score": round(best_b, 4),
        }

    # 按融合来源聚合成候选块
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for atom_code, meta in fused_atoms.items():
        if meta["winner"] == "none":
            continue
        groups[(meta["winner"], meta["source"])].append(atom_code)

    merged_blocks: list[dict] = []
    for (winner, source), atoms in sorted(groups.items()):
        if winner == "textbook":
            prof = a_profiles.get(source, {})
            name = _suggest_new_block_name_from_old(prof.get("label") or source)
        else:
            prof = b_profiles.get(source, {})
            name = (prof.get("label") or source)[:64]
        merged_blocks.append(
            {
                "fusion_key": f"{winner}:{source}",
                "winner_track": winner,
                "source_code": source,
                "suggested_name": name,
                "atom_codes": sorted(atoms),
                "atom_count": len(atoms),
            }
        )

    unassigned = [
        c for c, m in fused_atoms.items() if m["winner"] == "none"
    ]
    return {
        "rules": (
            f"每原子取得分更高的一侧（min_score={min_score}，"
            f"同分±{tie_epsilon}优先教材轨）；按 (track, source) 聚合"
        ),
        "fused_atoms": fused_atoms,
        "merged_blocks": merged_blocks,
        "unassigned_atoms": unassigned,
        "conflicts": conflicts,
    }


def _name_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _diff_vs_current(
    *,
    current_blocks: list[Block],
    fused: dict,
    old_blocks_by_code: dict[str, Block],
    new_atoms_by_code: dict[str, TextbookAtom],
) -> dict:
    current_snap: list[dict] = []
    atom_to_current: dict[str, str] = {}
    for b in current_blocks:
        refs = (b.metadata_json or {}).get("anchor_old_refs") or []
        current_snap.append(
            {
                "block_code": b.block_code,
                "block_name": b.block_name,
                "atom_codes": list(b.atom_codes or []),
                "anchor_old_refs": refs,
            }
        )
        for code in b.atom_codes or []:
            atom_to_current[str(code).strip()] = b.block_code

    fused_blocks = fused.get("merged_blocks") or []
    atom_to_fused: dict[str, str] = {}
    for i, fb in enumerate(fused_blocks):
        key = fb.get("fusion_key") or f"F{i+1:02d}"
        for code in fb.get("atom_codes") or []:
            atom_to_fused[code] = key

    reassignments: list[dict] = []
    for code in sorted(new_atoms_by_code.keys()):
        cur = atom_to_current.get(code)
        fus = atom_to_fused.get(code)
        if cur != fus:
            reassignments.append(
                {
                    "atom_code": code,
                    "current_block": cur,
                    "fused_block": fus,
                }
            )

    anchor_rows: list[dict] = []
    for b in current_blocks:
        refs = (b.metadata_json or {}).get("anchor_old_refs") or []
        primary = refs[0] if refs else {}
        old_code = str(primary.get("old_block_code") or "").strip()
        old_block = old_blocks_by_code.get(old_code)
        fused_match = None
        best_sim = 0.0
        for fb in fused_blocks:
            if fb.get("source_code") == old_code:
                fused_match = fb.get("fusion_key")
                best_sim = 1.0
                break
            sim = _name_sim(b.block_name or "", fb.get("suggested_name") or "")
            if sim > best_sim:
                best_sim = sim
                fused_match = fb.get("fusion_key")
        anchor_rows.append(
            {
                "new_block": b.block_code,
                "new_name": b.block_name,
                "anchor_old_block": old_code,
                "anchor_old_name": (old_block.block_name if old_block else None),
                "fused_match": fused_match,
                "name_sim_to_fused": round(best_sim, 3),
            }
        )

    # detect atoms in multiple current blocks
    owner_count: dict[str, list[str]] = defaultdict(list)
    for b in current_blocks:
        for code in b.atom_codes or []:
            owner_count[str(code).strip()].append(b.block_code)
    dup_in_current = {
        c: owners for c, owners in owner_count.items() if len(owners) > 1
    }

    return {
        "current_blocks": current_snap,
        "fused_block_count": len(fused_blocks),
        "current_block_count": len(current_blocks),
        "atom_reassignments": reassignments,
        "anchor_alignment": anchor_rows,
        "duplicate_atoms_in_current": dup_in_current,
    }


def _load_dual_track_context(lesson_uid: str) -> dict[str, Any]:
    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    match = get_primary_lesson_match(new_les.id)
    if not match or not match.old_lesson_id:
        raise ValueError(f"{lesson_uid} 无主参照旧课配对")

    old_les = Lesson.query.get(match.old_lesson_id)
    if not old_les:
        raise ValueError("旧课记录不存在")

    new_atoms_raw = TextbookAtom.query.filter_by(lesson_id=new_les.id).all()
    new_atoms = [a for a in new_atoms_raw if not _is_placeholder_atom(a)]
    new_atoms_by_code = {a.atom_code: a for a in new_atoms}

    old_atoms = TextbookAtom.query.filter_by(lesson_id=old_les.id).all()
    old_atoms_by_code = {a.atom_code: a for a in old_atoms}

    old_blocks = (
        Block.query.filter_by(lesson_id=old_les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    old_blocks_by_code = {b.block_code: b for b in old_blocks}

    slides = (
        CoursewareSlide.query.filter_by(lesson_id=old_les.id)
        .order_by(CoursewareSlide.slide_index)
        .all()
    )
    slides_by_index = {int(s.slide_index): s for s in slides}

    from ....models import LessonPage

    old_pages = (
        LessonPage.query.filter_by(lesson_id=old_les.id)
        .order_by(LessonPage.page_index)
        .all()
    )

    from .doubao_old_textbook_cache import build_old_textbook_doubao_payload

    old_doubao = build_old_textbook_doubao_payload(
        lesson_uid=old_les.lesson_uid,
        pages=old_pages,
    )

    current_blocks = (
        Block.query.filter_by(lesson_id=new_les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )

    return {
        "new_les": new_les,
        "old_les": old_les,
        "new_atoms": new_atoms,
        "new_atoms_by_code": new_atoms_by_code,
        "old_atoms_by_code": old_atoms_by_code,
        "old_blocks": old_blocks,
        "old_blocks_by_code": old_blocks_by_code,
        "slides": slides,
        "slides_by_index": slides_by_index,
        "old_pages": old_pages,
        "old_textbook_doubao": old_doubao,
        "current_blocks": current_blocks,
    }


def _build_track_textbook(ctx: dict[str, Any], *, min_score: float) -> dict:
    old_blocks = ctx["old_blocks"]
    old_atoms_by_code = ctx["old_atoms_by_code"]
    new_atoms = ctx["new_atoms"]
    new_atoms_by_code = ctx["new_atoms_by_code"]
    old_lesson_uid = str(ctx["old_les"].lesson_uid)

    tb_profiles: list[dict] = []
    for ob in old_blocks:
        if not _has_textbook_content(ob, old_atoms_by_code):
            continue
        query, y_mid = _textbook_query_for_block(
            ob,
            old_atoms_by_code,
            old_lesson_uid=old_lesson_uid,
            prefer_doubao=True,
        )
        if not query:
            continue
        tb_profiles.append(
            {
                "block_code": ob.block_code,
                "label": _split_block_name_sides(ob.block_name or "")[1]
                or ob.block_name,
                "query": query,
                "y_mid": y_mid,
                "old_block_name": ob.block_name,
                "textbook_pages": [ob.textbook_page_start, ob.textbook_page_end],
            }
        )

    tb_assignments, tb_leftover, tb_atom_scores = _assign_track(
        new_atoms,
        tb_profiles,
        min_score=min_score,
        new_atoms_by_code=new_atoms_by_code,
    )

    def _tb_name(prof: dict) -> str:
        return _suggest_new_block_name_from_old(prof.get("label") or "")

    return {
        "label": "教材轨（旧教材原子 + 教材侧块名）",
        "profiles": tb_profiles,
        "assignments": tb_assignments,
        "leftover_atoms": tb_leftover,
        "candidate_blocks": _candidate_blocks_from_assignments(
            tb_assignments,
            tb_profiles,
            track="textbook",
            suggest_name_fn=_tb_name,
        ),
        "coverage": {
            "profile_count": len(tb_profiles),
            "assigned_atoms": sum(len(v) for v in tb_assignments.values()),
            "leftover_count": len(tb_leftover),
        },
        "unassigned_followup": _build_unassigned_followup(
            leftover_codes=tb_leftover,
            atom_scores=tb_atom_scores,
            new_atoms_by_code=new_atoms_by_code,
            profiles=tb_profiles,
            min_score=min_score,
            track="textbook",
        ),
        "borderline_followup": _build_borderline_followup(
            assignments=tb_assignments,
            profiles=tb_profiles,
            new_atoms_by_code=new_atoms_by_code,
            min_score=min_score,
            track="textbook",
        ),
    }


def _build_track_courseware(
    ctx: dict[str, Any],
    *,
    min_score: float,
    include_per_slide: bool,
) -> dict:
    old_blocks = ctx["old_blocks"]
    old_atoms_by_code = ctx["old_atoms_by_code"]
    slides = ctx["slides"]
    slides_by_index = ctx["slides_by_index"]
    new_atoms = ctx["new_atoms"]
    new_atoms_by_code = ctx["new_atoms_by_code"]

    cw_profiles: list[dict] = []
    for ob in old_blocks:
        if not _has_courseware_content(ob):
            continue
        query = _courseware_query_for_block(ob, slides_by_index)
        if not query:
            continue
        _, y_mid = _block_query_and_ymid(ob, old_atoms_by_code)
        cw_profiles.append(
            {
                "block_code": ob.block_code,
                "label": _split_block_name_sides(ob.block_name or "")[0]
                or ob.block_name,
                "query": query,
                "y_mid": y_mid,
                "old_block_name": ob.block_name,
                "course_slide_indices": list(ob.course_slide_indices or []),
            }
        )

    if include_per_slide:
        covered_slides = {
            int(x)
            for p in cw_profiles
            for x in (p.get("course_slide_indices") or [])
        }
        for slide in slides:
            idx = int(slide.slide_index)
            if idx in covered_slides:
                continue
            q = _slide_query(slide)
            if not q:
                continue
            cw_profiles.append(
                {
                    "block_code": f"CW{idx:02d}",
                    "label": f"课件页{idx}",
                    "query": q,
                    "y_mid": None,
                    "old_block_name": None,
                    "course_slide_indices": [idx],
                }
            )

    cw_assignments, cw_leftover, cw_atom_scores = _assign_track(
        new_atoms,
        cw_profiles,
        min_score=min_score,
        new_atoms_by_code=new_atoms_by_code,
    )

    return {
        "label": "课件轨（旧课件 OCR + 课件侧块名）",
        "profiles": cw_profiles,
        "assignments": cw_assignments,
        "leftover_atoms": cw_leftover,
        "candidate_blocks": _candidate_blocks_from_assignments(
            cw_assignments,
            cw_profiles,
            track="courseware",
            suggest_name_fn=lambda p: (p.get("label") or "课件模块")[:64],
        ),
        "coverage": {
            "profile_count": len(cw_profiles),
            "assigned_atoms": sum(len(v) for v in cw_assignments.values()),
            "leftover_count": len(cw_leftover),
        },
        "unassigned_followup": _build_unassigned_followup(
            leftover_codes=cw_leftover,
            atom_scores=cw_atom_scores,
            new_atoms_by_code=new_atoms_by_code,
            profiles=cw_profiles,
            min_score=min_score,
            track="courseware",
        ),
        "borderline_followup": _build_borderline_followup(
            assignments=cw_assignments,
            profiles=cw_profiles,
            new_atoms_by_code=new_atoms_by_code,
            min_score=min_score,
            track="courseware",
        ),
    }


def _new_content_preview_blocks(followup: dict) -> list[dict]:
    out: list[dict] = []
    for i, cl in enumerate(followup.get("suggested_new_blocks") or []):
        pg = cl.get("page_index")
        out.append(
            {
                "block_code": f"NC-P{pg}",
                "block_name": cl.get("suggested_block_name") or f"第{pg}页新课",
                "atom_codes": cl.get("atom_codes") or [],
                "source_old_block": None,
                "track": "new_content",
                "badge": cl.get("badge") or "新课新增",
                "sort_order": 900 + i,
                "metadata_json": {"dual_track_preview": True, "track": "new_content"},
            }
        )
    return out


def _attach_fusion_unassigned(fusion: dict, track_a: dict, track_b: dict, ctx: dict, *, min_score: float) -> dict:
    followup = _build_fusion_unassigned_followup(
        fusion,
        track_a,
        track_b,
        ctx["new_atoms_by_code"],
        min_score=min_score,
    )
    fusion = dict(fusion)
    fusion["unassigned_followup"] = followup
    return fusion


def _build_compare_step(track_a: dict, track_b: dict) -> dict:
    """同一旧块编码下，教材轨 vs 课件轨的原子分配差异。"""
    codes = sorted(
        set(track_a.get("assignments") or {})
        | set(track_b.get("assignments") or {})
    )
    rows: list[dict] = []
    agree_total = 0
    disagree_total = 0
    for code in codes:
        a_set = set(track_a.get("assignments", {}).get(code) or [])
        b_set = set(track_b.get("assignments", {}).get(code) or [])
        overlap = sorted(a_set & b_set)
        only_a = sorted(a_set - b_set)
        only_b = sorted(b_set - a_set)
        agree_total += len(overlap)
        disagree_total += len(only_a) + len(only_b)
        if not a_set and not b_set:
            continue
        rows.append(
            {
                "old_block_code": code,
                "textbook_atoms": sorted(a_set),
                "courseware_atoms": sorted(b_set),
                "agree_atoms": overlap,
                "textbook_only": only_a,
                "courseware_only": only_b,
            }
        )

    block_count_a = len(
        [c for c in track_a.get("candidate_blocks") or [] if c.get("atom_count")]
    )
    block_count_b = len(
        [c for c in track_b.get("candidate_blocks") or [] if c.get("atom_count")]
    )
    return {
        "by_old_block": rows,
        "summary": {
            "old_block_rows": len(rows),
            "agree_atom_assignments": agree_total,
            "disagree_atom_slots": disagree_total,
            "textbook_nonempty_blocks": block_count_a,
            "courseware_nonempty_blocks": block_count_b,
        },
    }


def _to_preview_blocks(
    candidate_blocks: list[dict],
    *,
    prefix: str,
    track: str,
    badge: str,
    include_empty: bool = False,
) -> list[dict]:
    out: list[dict] = []
    n = 0
    for cb in candidate_blocks:
        atoms = cb.get("atom_codes") or []
        if not atoms and not include_empty:
            continue
        n += 1
        src = cb.get("source_code") or f"{n:02d}"
        out.append(
            {
                "block_code": f"{prefix}{src}",
                "block_name": cb.get("suggested_name") or src,
                "atom_codes": atoms,
                "source_old_block": src,
                "track": track,
                "badge": badge,
                "sort_order": n,
                "metadata_json": {
                    "dual_track_preview": True,
                    "track": track,
                    "source_old_block": src,
                },
            }
        )
    return out


def _fusion_preview_blocks(fusion: dict) -> list[dict]:
    out: list[dict] = []
    for i, fb in enumerate(fusion.get("merged_blocks") or []):
        key = fb.get("fusion_key") or f"F{i+1:02d}"
        track = fb.get("winner_track") or "fusion"
        badge = "教材轨" if track == "textbook" else "课件轨"
        out.append(
            {
                "block_code": f"F-{key}",
                "block_name": fb.get("suggested_name") or key,
                "atom_codes": fb.get("atom_codes") or [],
                "source_old_block": fb.get("source_code"),
                "track": track,
                "badge": f"融合·{badge}",
                "sort_order": i + 1,
                "metadata_json": {
                    "dual_track_preview": True,
                    "track": track,
                    "fusion_key": key,
                    "source_old_block": fb.get("source_code"),
                },
            }
        )
    return out


def _lesson_header(ctx: dict[str, Any]) -> dict[str, Any]:
    new_les = ctx["new_les"]
    old_les = ctx["old_les"]
    return {
        "lesson_uid": new_les.lesson_uid,
        "lesson_name": new_les.lesson_name,
        "old_lesson_uid": old_les.lesson_uid,
        "old_lesson_name": old_les.lesson_name,
        "counts": {
            "new_atoms": len(ctx["new_atoms"]),
            "old_blocks": len(ctx["old_blocks"]),
            "old_courseware_slides": len(ctx["slides"]),
            "current_new_blocks": len(ctx["current_blocks"]),
        },
    }


def _resolve_track_textbook(
    ctx: dict[str, Any],
    *,
    min_score: float,
    use_llm: bool,
) -> dict[str, Any]:
    if use_llm:
        from .dual_track_textbook_llm import build_track_textbook_with_llm

        return build_track_textbook_with_llm(ctx, min_score=min_score)
    track = _build_track_textbook(ctx, min_score=min_score)
    track["ranker"] = "rules"
    return track


DUAL_TRACK_STEP_GROUPS: list[dict[str, Any]] = [
    {
        "group": "0",
        "label": "前置工序",
        "steps": [
            {"key": "text_ocr", "label": "1 文字OCR", "hint": "豆包新教材文字原子 + 旧库检视 + 豆包课件"},
            {"key": "image_ocr", "label": "2 图片OCR", "hint": "执行插图 OCR 并显示 image 原子框"},
            {"key": "block_match", "label": "3 栏目·聚类", "hint": "prepare 栏目聚类 → unit_id + 新教材建议块名"},
            {"key": "prescan", "label": "4 对照预判", "hint": "新↔旧预匹配初稿（独立链路）"},
            {"key": "pair_review", "label": "5 确认对照", "hint": "对照确认后再进四轨"},
        ],
    },
    {
        "group": "1",
        "label": "① 教材轨",
        "steps": [
            {"key": "tb_units", "label": "1-1 新单元", "hint": "prepare 聚类 unit_id 基准"},
            {"key": "tb_sources", "label": "1-2 旧教材源", "hint": "旧教材块/页参照源（不含课件）"},
            {"key": "tb_scores", "label": "1-3 打分", "hint": "每 unit 与旧教材源相似度"},
            {"key": "tb_classify", "label": "1-4 分类", "hint": "match/new + 三要素 + 插图标签"},
            {"key": "textbook", "label": "1-5 一块表", "hint": "一块汇总；可勾豆包视觉分配"},
        ],
    },
    {
        "group": "2",
        "label": "② 课件轨",
        "steps": [
            {"key": "cw_sources", "label": "2-1 参照源", "hint": "归档旧课件页/块，核心与附属分离"},
            {"key": "cw_scores", "label": "2-2 单元打分", "hint": "每 unit 与课件源文本相似度 + Top3"},
            {"key": "cw_illustration", "label": "2-3 插图标记", "hint": "插图 atom + 知识点冲突标记"},
            {"key": "cw_status", "label": "2-4 适配状态", "hint": "adapt / missing / cw_only 判定"},
            {"key": "courseware", "label": "2-5 二块总表", "hint": "汇总为二块完整表（含附属附表）"},
        ],
    },
    {
        "group": "3",
        "label": "③ 两轨对照",
        "steps": [
            {"key": "cmp_join", "label": "3-1 关联", "hint": "unit_id 左关联一块+二块原始字段"},
            {"key": "cmp_mark", "label": "3-2 对照标记", "hint": "agree/conflict/unique_* + 判定依据"},
            {"key": "cmp_summary", "label": "3-3 统计", "hint": "各对照类型数量汇总"},
            {"key": "compare", "label": "3-4 对照总表", "hint": "完整对照表，右侧可点选单元"},
        ],
    },
    {
        "group": "4",
        "label": "④ 融合",
        "steps": [
            {"key": "fusion", "label": "融合结果", "hint": "fusion_action + 插图复用方案"},
        ],
    },
]

_PREP_SUBSTEPS = frozenset({
    "text_ocr", "image_ocr", "block_match", "prescan", "pair_review",
})
_TB_SUBSTEPS = frozenset({"tb_units", "tb_sources", "tb_scores", "tb_classify"})
_CW_SUBSTEPS = frozenset(
    {"cw_sources", "cw_scores", "cw_illustration", "cw_status", "courseware", "cw_table"}
)
_CMP_SUBSTEPS = frozenset({"cmp_join", "cmp_mark", "cmp_summary", "compare", "cmp_table"})


def _normalize_step(step: str) -> str:
    s = (step or "").strip().lower()
    if s == "cw_table":
        return "courseware"
    if s == "cmp_table":
        return "compare"
    return s


def _resolve_yikuai_table(
    ctx: dict[str, Any],
    *,
    min_score: float,
    use_llm: bool,
    cached_yikuai: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """返回 (yikuai, yikuai_rules, yikuai_diff)。"""
    from .dual_track_tables import (
        build_yikuai_table,
        merge_llm_into_yikuai,
        summarize_yikuai_diff,
    )

    if cached_yikuai and cached_yikuai.get("rows"):
        yikuai = cached_yikuai
        rules = build_yikuai_table(ctx, min_score=min_score)
        rules["ranker"] = "rules"
        diff = summarize_yikuai_diff(rules, yikuai) if yikuai.get("ranker") == "doubao" else None
        return yikuai, rules, diff

    yikuai_rules = build_yikuai_table(ctx, min_score=min_score)
    yikuai_rules["ranker"] = "rules"
    yikuai = yikuai_rules
    diff = None

    if use_llm:
        track_a = _resolve_track_textbook(ctx, min_score=min_score, use_llm=True)
        if track_a.get("ranker") == "doubao":
            import copy

            yikuai = copy.deepcopy(yikuai_rules)
            merge_llm_into_yikuai(yikuai, track_a, ctx, min_score=min_score)
            diff = summarize_yikuai_diff(yikuai_rules, yikuai)

    return yikuai, yikuai_rules, diff


def run_dual_track_step(
    *,
    lesson_uid: str,
    step: str,
    min_score: float = 0.10,
    include_per_slide_track_b: bool = True,
    use_llm: bool = False,
    cached_yikuai: dict[str, Any] | None = None,
    execute: bool = True,
    force_textbook: bool = False,
    force_slides: bool = False,
) -> dict[str, Any]:
    """分步双轨实验（dry-run），供网页逐步点击。"""
    step = _normalize_step(step)
    ctx = _load_dual_track_context(lesson_uid)
    header = _lesson_header(ctx)
    base = {
        **header,
        "ok": True,
        "step": step,
        "granularity_note": GRANULARITY_NOTE,
        "production_note": PRODUCTION_NOTE,
        "params": {
            "min_score": min_score,
            "include_per_slide_track_b": include_per_slide_track_b,
            "use_llm": use_llm,
            "used_cached_yikuai": bool(cached_yikuai and cached_yikuai.get("rows")),
        },
    }

    if step == "meta":
        flat = [
            {"key": "live", "label": "现网块", "group": "0", "hint": "数据库 N01…"},
        ]
        for grp in DUAL_TRACK_STEP_GROUPS:
            for st in grp["steps"]:
                flat.append({**st, "group": grp["group"], "group_label": grp["label"]})
        return {
            **base,
            "mode_label": "说明",
            "preview_blocks": [],
            "detail": {"step_groups": DUAL_TRACK_STEP_GROUPS, "steps": flat},
        }

    if step == "live":
        raise ValueError("现网块由前端本地切换，无需 POST")

    from .dual_track_prep_steps import build_prep_step_detail
    from .dual_track_tables import (
        build_compare_join_table,
        build_compare_mark_table,
        build_compare_summary_table,
        build_cw_illustration_table,
        build_cw_scores_table,
        build_cw_status_table,
        build_erkuai_table,
        build_tb_classify_table,
        build_tb_scores_table,
        build_tb_units_table,
        compare_rows_to_preview_blocks,
        erkuai_rows_to_preview_blocks,
        fusion_rows_to_preview_blocks,
        yikuai_rows_to_preview_blocks,
        _cw_sources_snapshot,
        _tb_sources_snapshot,
    )

    # —— 前置 5 道工序（1/2 执行 OCR 并写库或缓存，其余只读检视）——
    if step in _PREP_SUBSTEPS:
        run_meta: dict[str, Any] = {}
        reload_workspace = False
        if execute:
            exec_ctx = dict(ctx)
            exec_ctx["snap"] = build_dual_track_ctx_snap(ctx)
            if step == "text_ocr":
                from .dual_track_prep_run import execute_prep_text_ocr

                run_meta = execute_prep_text_ocr(
                    exec_ctx,
                    force_textbook=force_textbook,
                    force_slides=force_slides,
                )
                ctx = _load_dual_track_context(lesson_uid)
                reload_workspace = True
            elif step == "image_ocr":
                from .dual_track_prep_run import execute_prep_image_ocr

                run_meta = execute_prep_image_ocr(exec_ctx)
                ctx = _load_dual_track_context(lesson_uid)
                reload_workspace = True
        prep = build_prep_step_detail(ctx, step)
        if run_meta:
            prep["run"] = run_meta
        elif not execute and step in ("text_ocr", "image_ocr"):
            prep["inspect_only"] = True
        seg = prep.get("segment_status") or "pending"
        return {
            **base,
            "mode_label": prep.get("step_label") or step,
            "segment_status": seg,
            "preview_blocks": [],
            "reload_workspace": reload_workspace,
            "detail": {"prep": prep},
        }

    # —— ① 教材轨分步 ——
    if step in _TB_SUBSTEPS:
        if step == "tb_units":
            table = build_tb_units_table(ctx)
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": [],
                "detail": {"tb_units": table},
            }
        if step == "tb_sources":
            table = _tb_sources_snapshot(ctx)
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": [],
                "detail": {"tb_sources": table},
            }
        if step == "tb_scores":
            table = build_tb_scores_table(ctx, min_score=min_score)
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": [],
                "detail": {"tb_scores": table},
            }
        table = build_tb_classify_table(ctx, min_score=min_score)
        return {
            **base,
            "mode_label": table["table_label"],
            "preview_blocks": [],
            "detail": {"tb_classify": table},
        }

    # —— ② 课件轨分步（不跑 LLM）——
    if step in _CW_SUBSTEPS:
        if step == "cw_sources":
            table = _cw_sources_snapshot(ctx)
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": [],
                "detail": {"cw_sources": table},
            }
        if step == "cw_scores":
            table = build_cw_scores_table(ctx, min_score=min_score)
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": [],
                "detail": {"cw_scores": table},
            }
        if step == "cw_illustration":
            table = build_cw_illustration_table(ctx, min_score=min_score)
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": [],
                "detail": {"cw_illustration": table},
            }
        if step == "cw_status":
            table = build_cw_status_table(ctx, min_score=min_score)
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": [],
                "detail": {"cw_status": table},
            }
        erkuai = build_erkuai_table(ctx, min_score=min_score)
        preview = erkuai_rows_to_preview_blocks(erkuai)
        return {
            **base,
            "mode_label": "②-5 二块·课件轨汇总表",
            "preview_blocks": preview,
            "detail": {
                "erkuai": erkuai,
                "ancillary_cw": erkuai.get("ancillary_cw") or [],
                "unit_count": erkuai.get("row_count"),
            },
        }

    # —— ③ 对照分步（需要一块+二块；一块可来自前端缓存）——
    if step in _CMP_SUBSTEPS:
        yikuai, yikuai_rules, yikuai_diff = _resolve_yikuai_table(
            ctx,
            min_score=min_score,
            use_llm=False,
            cached_yikuai=cached_yikuai,
        )
        erkuai = build_erkuai_table(ctx, min_score=min_score)
        yikuai_note = (
            "一块来自上次 ① 缓存（含豆包）"
            if cached_yikuai and cached_yikuai.get("rows")
            else "一块为规则基线（请先跑 ① 或勾选豆包后跑 ①，③ 会沿用缓存）"
        )
        if step == "cmp_join":
            table = build_compare_join_table(yikuai, erkuai)
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": [],
                "detail": {
                    "cmp_join": table,
                    "yikuai_source_note": yikuai_note,
                    "yikuai": yikuai,
                    "erkuai": erkuai,
                },
            }
        if step == "cmp_mark":
            table = build_compare_mark_table(yikuai, erkuai)
            preview = compare_rows_to_preview_blocks(
                {"rows": table["rows"], "summary": table["summary"]},
                yikuai,
            )
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": preview,
                "detail": {
                    "cmp_mark": table,
                    "yikuai_source_note": yikuai_note,
                    "yikuai": yikuai,
                    "erkuai": erkuai,
                },
            }
        if step == "cmp_summary":
            table = build_compare_summary_table(yikuai, erkuai)
            marked = build_compare_mark_table(yikuai, erkuai)
            return {
                **base,
                "mode_label": table["table_label"],
                "preview_blocks": [],
                "detail": {
                    "cmp_summary": table,
                    "cmp_mark": marked,
                    "yikuai_source_note": yikuai_note,
                },
            }
        compare_table = build_compare_mark_table(yikuai, erkuai)
        compare_table["table_key"] = "compare"
        compare_table["table_label"] = "两轨对照表"
        preview = compare_rows_to_preview_blocks(compare_table, yikuai)
        return {
            **base,
            "mode_label": "③-4 两轨对照总表",
            "preview_blocks": preview,
            "detail": {
                "compare": compare_table,
                "yikuai_source_note": yikuai_note,
                "yikuai": yikuai,
                "erkuai": erkuai,
            },
        }

    # —— ① 教材轨 / ④ 融合（可走豆包）——
    llm_for_tables = None
    yikuai_rules = None
    yikuai_diff = None

    if step == "textbook":
        yikuai, yikuai_rules, yikuai_diff = _resolve_yikuai_table(
            ctx,
            min_score=min_score,
            use_llm=use_llm,
            cached_yikuai=None,
        )
        erkuai = build_erkuai_table(ctx, min_score=min_score)
        compare_table = build_compare_mark_table(yikuai, erkuai)
        compare_table["table_key"] = "compare"
        from .dual_track_tables import build_fusion_unit_table

        fusion_table = build_fusion_unit_table(ctx, yikuai, erkuai, compare_table)
        preview = yikuai_rows_to_preview_blocks(yikuai)
        ranker = yikuai.get("ranker") or ("doubao" if use_llm else "rules")
        mode = (
            "① 一块·教材轨（豆包方案）"
            if ranker == "doubao"
            else "① 一块·教材轨（规则基线）"
        )
        return {
            **base,
            "mode_label": mode,
            "ranker": ranker,
            "preview_blocks": preview,
            "detail": {
                "yikuai": yikuai,
                "yikuai_rules": yikuai_rules,
                "yikuai_diff": yikuai_diff,
                "unit_count": yikuai.get("row_count"),
                "erkuai": erkuai,
                "compare": compare_table,
                "fusion": fusion_table,
            },
        }

    if step == "fusion":
        yikuai, yikuai_rules, yikuai_diff = _resolve_yikuai_table(
            ctx,
            min_score=min_score,
            use_llm=use_llm,
            cached_yikuai=cached_yikuai,
        )
        erkuai = build_erkuai_table(ctx, min_score=min_score)
        compare_table = build_compare_mark_table(yikuai, erkuai)
        compare_table["table_key"] = "compare"
        from .dual_track_tables import build_fusion_unit_table

        fusion_table = build_fusion_unit_table(ctx, yikuai, erkuai, compare_table)
        adapted_fusion = {
            "merged_blocks": [
                {
                    "fusion_key": r["unit_id"],
                    "source_code": r["unit_id"],
                    "suggested_name": r.get("section_name") or r["unit_id"],
                    "atom_codes": r.get("atom_codes") or [],
                }
                for r in fusion_table.get("rows") or []
            ]
        }
        diff = _diff_vs_current(
            current_blocks=ctx["current_blocks"],
            fused=adapted_fusion,
            old_blocks_by_code=ctx["old_blocks_by_code"],
            new_atoms_by_code=ctx["new_atoms_by_code"],
        )
        preview = fusion_rows_to_preview_blocks(fusion_table)
        return {
            **base,
            "mode_label": "④ 融合·标准原子区块",
            "ranker": yikuai.get("ranker") or "rules",
            "preview_blocks": preview,
            "detail": {
                "fusion": fusion_table,
                "yikuai": yikuai,
                "yikuai_rules": yikuai_rules,
                "yikuai_diff": yikuai_diff,
                "erkuai": erkuai,
                "compare": compare_table,
                "diff_vs_current": diff,
            },
        }

    raise ValueError(f"未知步骤：{step}")


def run_dual_track_experiment(
    *,
    lesson_uid: str,
    min_score: float = 0.10,
    include_per_slide_track_b: bool = True,
) -> dict[str, Any]:
    """双轨实验主入口（dry-run，一次返回全部）。"""
    ctx = _load_dual_track_context(lesson_uid)
    header = _lesson_header(ctx)
    track_a = _build_track_textbook(ctx, min_score=min_score)
    track_b = _build_track_courseware(
        ctx,
        min_score=min_score,
        include_per_slide=include_per_slide_track_b,
    )
    fusion = _fuse_tracks(
        track_a,
        track_b,
        ctx["new_atoms_by_code"],
        min_score=min_score,
    )
    fusion = _attach_fusion_unassigned(
        fusion, track_a, track_b, ctx, min_score=min_score
    )
    diff = _diff_vs_current(
        current_blocks=ctx["current_blocks"],
        fused=fusion,
        old_blocks_by_code=ctx["old_blocks_by_code"],
        new_atoms_by_code=ctx["new_atoms_by_code"],
    )

    return {
        **header,
        "granularity_note": GRANULARITY_NOTE,
        "production_note": PRODUCTION_NOTE,
        "params": {
            "min_score": min_score,
            "include_per_slide_track_b": include_per_slide_track_b,
        },
        "track_a_textbook": track_a,
        "track_b_courseware": track_b,
        "fusion": fusion,
        "diff_vs_current": diff,
        "compare": _build_compare_step(track_a, track_b),
    }
