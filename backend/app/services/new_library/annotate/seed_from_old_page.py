"""对照粗分旧课区块，在新教材页上自动建块（对齐 annotate-v3 seed-from-old-page）。"""
from __future__ import annotations

import math
import re
from collections import Counter

from ....extensions import db
from ....models import Block, Lesson, LessonMatch, LessonPage, TextbookAtom, Volume
from ....parsers.lesson_reuse_match import raw_ref_similarity_score
from ...lesson_lookup import get_lesson_by_uid
from ...old_library.annotate.blocks import (
    _block_code_prefix,
    _ensure_blocks_editable,
    block_occupancy,
)
from .pair_review import (
    allows_in_lesson_auto_pairing,
    get_primary_lesson_match,
    pair_review_status,
)

OLD_TO_NEW_BLOCK_NAME_HINTS: list[tuple[str, str]] = [
    ("知识点讲解", "核心概念"),
    ("实验/活动操作", "科学探究"),
    ("实验", "科学探究"),
    ("活动操作", "科学探究"),
    ("导入", "问题情境"),
    ("情境", "问题情境"),
    ("知识小结", "归纳总结"),
    ("小结", "归纳总结"),
    ("典例", "拓展迁移"),
    ("课件名称", "封面标题"),
    ("封面", "封面标题"),
    ("安全警示", "安全警示"),
]

SECTION_HEADER_KEYWORDS: tuple[str, ...] = (
    "问题情境",
    "科学探究",
    "拓展迁移",
    "反思评价",
    "归纳总结",
    "安全警示",
    "核心概念",
    "应用迁移",
    "想一想",
    "试一试",
    "讨论交流",
    "阅读",
    "活动",
    "实验",
    "搜集",
)

ANCHORED_BLOCK_DISPLAY_NAMES: dict[str, str] = {
    "B02": "问题情境·冰雪消融导入",
    "B07": "科学探究·加热蜡块实验",
    "B08": "科学探究·制作蜡星星",
}

NEW_SECTION_BLOCK_NAMES: dict[str, str] = {
    "反思评价": "反思评价",
    "拓展迁移": "拓展迁移·巧克力试一试",
}


def _atom_plain_text(atom: TextbookAtom) -> str:
    return (atom.content or atom.ocr_text or "").strip()


def _bbox_ymid(bbox: dict | None) -> float:
    b = bbox or {}
    return (float(b.get("y_start", 0)) + float(b.get("y_end", 0))) / 2.0


def _bbox_xmid(bbox: dict | None) -> float:
    b = bbox or {}
    return (float(b.get("x_start", 0)) + float(b.get("x_end", 0))) / 2.0


def _bbox_area(bbox: dict | None) -> float:
    b = bbox or {}
    w = max(0.0, float(b.get("x_end", 0)) - float(b.get("x_start", 0)))
    h = max(0.0, float(b.get("y_end", 0)) - float(b.get("y_start", 0)))
    return w * h


def _bbox_2d_dist(a: dict | None, b: dict | None) -> float:
    ay, ax = _bbox_ymid(a), _bbox_xmid(a)
    by, bx = _bbox_ymid(b), _bbox_xmid(b)
    return math.hypot((ay - by) * 1.35, ax - bx)


def _image_label_text(atom: TextbookAtom) -> str:
    text = _atom_plain_text(atom)
    if not text:
        return ""
    for prefix in ("[插图]", "[image]", "[图片"):
        if text.startswith(prefix):
            rest = text[len(prefix) :].strip()
            if rest.startswith("]"):
                rest = rest[1:].strip()
            return rest[:500]
    return ""


def _is_section_header_atom(atom: TextbookAtom) -> bool:
    if (atom.atom_type or "").strip().lower() == "title":
        return True
    text = _atom_plain_text(atom)
    if not text or text.startswith("["):
        return False
    compact = re.sub(r"\s+", "", text)
    if len(compact) > 14:
        return False
    return any(kw in text for kw in SECTION_HEADER_KEYWORDS)


def _block_touches_page(
    block: Block,
    page_index: int,
    atoms_by_code: dict[str, TextbookAtom],
) -> bool:
    for code in block.atom_codes or []:
        atom = atoms_by_code.get(str(code).strip())
        if atom and int(atom.page_index) == int(page_index):
            return True
    start = block.textbook_page_start
    end = block.textbook_page_end
    if start is not None and end is not None:
        return int(start) <= int(page_index) <= int(end)
    return False


def _block_query_and_ymid(
    block: Block,
    atoms_by_code: dict[str, TextbookAtom],
) -> tuple[str, float | None]:
    parts: list[str] = [(block.block_name or "").strip()]
    ys: list[float] = []
    for code in block.atom_codes or []:
        atom = atoms_by_code.get(str(code).strip())
        if not atom:
            continue
        text = _atom_plain_text(atom)
        if text:
            parts.append(text)
        ys.append(_bbox_ymid(atom.bbox_json))
    query = " ".join(parts).strip()
    y_mid = sum(ys) / len(ys) if ys else None
    return query, y_mid


def _suggest_new_block_name_from_old(old_name: str) -> str:
    name = (old_name or "").strip()
    if not name:
        return "未命名模块"
    for kw, new_label in OLD_TO_NEW_BLOCK_NAME_HINTS:
        if kw in name:
            return new_label
    return name


def _text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return raw_ref_similarity_score(a, b)


def _profile_block_match_score(
    atom_codes: list[str],
    prof: dict,
    new_atoms_by_code: dict[str, TextbookAtom],
) -> float:
    scores: list[float] = []
    for code in atom_codes:
        atom = new_atoms_by_code.get(code)
        if not atom:
            continue
        scores.append(
            _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof["y_mid"],
            )
        )
    return sum(scores) / len(scores) if scores else 0.0


def _is_image_like_atom(atom: TextbookAtom) -> bool:
    if (atom.atom_type or "").strip().lower() == "image":
        return True
    text = _atom_plain_text(atom)
    if text and not text.startswith("["):
        return False
    return False


def _assignments_atom_to_block(assignments: dict[str, list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for old_code, codes in assignments.items():
        for code in codes:
            out[str(code).strip()] = old_code
    return out


def _dedupe_atom_assignments(
    assignments: dict[str, list[str]],
    old_profiles: list[dict],
    new_atoms_by_code: dict[str, TextbookAtom],
    *,
    min_score: float = 0.0,
) -> tuple[dict[str, list[str]], list[str]]:
    """每个新原子仅保留得分最高的旧块分配，避免 LLM/规则重复绑定。"""
    prof_by_code = {
        prof["block"].block_code: prof
        for prof in old_profiles
        if not prof.get("skip_reason")
    }
    atom_best: dict[str, tuple[str, float]] = {}
    for old_code, codes in assignments.items():
        prof = prof_by_code.get(old_code)
        if not prof:
            continue
        for code in codes:
            atom = new_atoms_by_code.get(str(code).strip())
            if not atom:
                continue
            score = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof["y_mid"],
            )
            prev = atom_best.get(atom.atom_code)
            if prev is None or score > prev[1]:
                atom_best[atom.atom_code] = (old_code, score)

    deduped: dict[str, list[str]] = {
        prof["block"].block_code: [] for prof in old_profiles
    }
    for code, (old_code, score) in atom_best.items():
        if score >= min_score:
            deduped.setdefault(old_code, []).append(code)
    return deduped, []


def _absorb_leftover_atoms(
    assignments: dict[str, list[str]],
    leftover_atom_codes: list[str],
    old_profiles: list[dict],
    new_atoms_by_code: dict[str, TextbookAtom],
    *,
    fallback_min_score: float = 0.03,
) -> list[str]:
    """用较低阈值把剩余原子分配到最相近的旧块。"""
    still_leftover: list[str] = []
    for code in leftover_atom_codes:
        atom = new_atoms_by_code.get(str(code).strip())
        if not atom:
            continue
        best_code = ""
        best_score = 0.0
        for prof in old_profiles:
            if prof.get("skip_reason"):
                continue
            ob = prof["block"]
            score = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof["y_mid"],
            )
            if score > best_score:
                best_score = score
                best_code = ob.block_code
        if best_code and best_score >= fallback_min_score:
            assignments.setdefault(best_code, []).append(atom.atom_code)
        else:
            still_leftover.append(atom.atom_code)
    return still_leftover


def clear_lesson_blocks_for_pipeline(*, lesson_uid: str) -> int:
    """整课建块前清空已有新区块（须未锁定）。"""
    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)
    blocks = Block.query.filter_by(lesson_id=new_les.id).all()
    count = len(blocks)
    for block in blocks:
        db.session.delete(block)
    db.session.flush()
    return count


def compute_lesson_atom_coverage(*, lesson_id: str) -> dict:
    """统计课内可见原子的区块绑定覆盖率与重复绑定。"""
    from ...old_library.annotate.workspace import _is_placeholder_atom

    atoms = [
        a
        for a in TextbookAtom.query.filter_by(lesson_id=lesson_id).all()
        if not _is_placeholder_atom(a)
    ]
    visible_codes = {a.atom_code for a in atoms}
    blocks = Block.query.filter_by(lesson_id=lesson_id).all()
    code_counts: Counter[str] = Counter()
    for block in blocks:
        for code in block.atom_codes or []:
            key = str(code).strip()
            if key not in visible_codes:
                continue
            code_counts[key] += 1
    duplicates = {code: n for code, n in code_counts.items() if n > 1}
    assigned_unique = len(code_counts)
    total = len(atoms)
    rate = assigned_unique / total if total else 0.0
    empty_blocks = sum(1 for b in blocks if not (b.atom_codes or []))
    anchored = sum(
        1 for b in blocks if (b.metadata_json or {}).get("anchor_old_refs")
    )
    return {
        "total_atoms": total,
        "assigned_unique": assigned_unique,
        "assigned_total_refs": sum(code_counts.values()),
        "coverage_rate": round(rate, 4),
        "duplicate_atoms": duplicates,
        "duplicate_count": len(duplicates),
        "block_count": len(blocks),
        "empty_block_count": empty_blocks,
        "anchored_block_count": anchored,
    }


def reconcile_lesson_atom_coverage(
    *,
    lesson_uid: str,
    fallback_min_score: float = 0.03,
) -> dict:
    """整课后处理：去重、补绑剩余原子、回填空块。"""
    from ...old_library.annotate.workspace import _is_placeholder_atom

    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)
    paired = _primary_old_lesson(new_les.id)
    old_les = paired[0] if paired else None
    old_atoms_by_code: dict[str, TextbookAtom] = {}
    if old_les:
        old_atoms_by_code = {
            a.atom_code: a
            for a in TextbookAtom.query.filter_by(lesson_id=old_les.id).all()
        }

    blocks = (
        Block.query.filter_by(lesson_id=new_les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    atoms = [
        a
        for a in TextbookAtom.query.filter_by(lesson_id=new_les.id).all()
        if not _is_placeholder_atom(a)
    ]
    atoms_by_code = {a.atom_code: a for a in atoms}
    visible_codes = set(atoms_by_code.keys())

    seen: set[str] = set()
    deduped_refs = 0
    pruned_placeholders = 0
    old_blocks_by_code: dict[str, Block] = {}
    if old_les:
        old_blocks_by_code = {
            b.block_code: b
            for b in Block.query.filter_by(lesson_id=old_les.id).all()
        }

    for block in blocks:
        kept: list[str] = []
        for code in block.atom_codes or []:
            key = str(code).strip()
            if key not in visible_codes:
                pruned_placeholders += 1
                continue
            atom = atoms_by_code.get(key)
            if atom and not _atom_allowed_for_block(
                atom, block, old_blocks_by_code=old_blocks_by_code
            ):
                pruned_placeholders += 1
                continue
            if key in seen:
                deduped_refs += 1
                continue
            seen.add(key)
            kept.append(key)
        block.atom_codes = kept

    atom_owners, _ = block_occupancy(new_les.id)
    unassigned = [a for a in atoms if a.atom_code not in atom_owners]
    backfilled = 0

    for block in blocks:
        if block.atom_codes:
            continue
        page = int(block.textbook_page_start or block.textbook_page_end or 0)
        if page <= 0:
            continue
        refs = (block.metadata_json or {}).get("anchor_old_refs") or []
        old_code = str((refs[0] or {}).get("old_block_code") or "").strip() if refs else ""
        query = (block.block_name or "").strip()
        y_mid: float | None = None
        if old_les and old_code:
            old_block = Block.query.filter_by(
                lesson_id=old_les.id, block_code=old_code
            ).first()
            if old_block:
                query, y_mid = _block_query_and_ymid(old_block, old_atoms_by_code)
        page_atoms = [
            a for a in unassigned if int(a.page_index) == page
        ]
        scored: list[tuple[float, TextbookAtom]] = []
        for atom in page_atoms:
            score = _score_new_atom_for_old_block(
                atom, query_text=query, old_y_mid=y_mid
            )
            scored.append((score, atom))
        scored.sort(key=lambda item: item[0], reverse=True)
        picked: list[str] = []
        for score, atom in scored:
            if score < fallback_min_score:
                break
            picked.append(atom.atom_code)
            unassigned = [a for a in unassigned if a.atom_code != atom.atom_code]
        if picked:
            block.atom_codes = picked
            backfilled += len(picked)
            for code in picked:
                seen.add(code)

    for atom in list(unassigned):
        page = int(atom.page_index)
        page_blocks = [
            b
            for b in blocks
            if _block_touches_page(b, page, atoms_by_code)
        ]
        if not page_blocks:
            continue
        best_block = _best_block_for_unassigned_atom(
            atom,
            page_blocks,
            atoms_by_code=atoms_by_code,
            old_les=old_les,
            old_atoms_by_code=old_atoms_by_code,
        )
        if best_block:
            best_block.atom_codes = list(best_block.atom_codes or []) + [atom.atom_code]
            unassigned = [a for a in unassigned if a.atom_code != atom.atom_code]
            backfilled += 1

    db.session.flush()
    blocks = Block.query.filter_by(lesson_id=new_les.id).all()
    sync_block_page_ranges_from_atoms(blocks, atoms_by_code)
    coverage = compute_lesson_atom_coverage(lesson_id=new_les.id)
    return {
        "deduped_refs": deduped_refs,
        "pruned_placeholders": pruned_placeholders,
        "backfilled_atoms": backfilled,
        "coverage": coverage,
    }


def _old_profile_for_anchored_block(
    block: Block,
    *,
    old_les: Lesson | None,
    old_atoms_by_code: dict[str, TextbookAtom],
) -> tuple[str, float | None] | None:
    if not old_les:
        return None
    refs = (block.metadata_json or {}).get("anchor_old_refs") or []
    if not refs:
        return None
    old_code = str((refs[0] or {}).get("old_block_code") or "").strip()
    if not old_code:
        return None
    old_block = Block.query.filter_by(
        lesson_id=old_les.id, block_code=old_code
    ).first()
    if not old_block:
        return None
    return _block_query_and_ymid(old_block, old_atoms_by_code)


def _best_block_for_unassigned_atom(
    atom: TextbookAtom,
    page_blocks: list[Block],
    *,
    atoms_by_code: dict[str, TextbookAtom],
    old_les: Lesson | None,
    old_atoms_by_code: dict[str, TextbookAtom],
    min_score: float = 0.02,
) -> Block | None:
    best_block: Block | None = None
    best_score = 0.0
    for block in page_blocks:
        profile = _old_profile_for_anchored_block(
            block,
            old_les=old_les,
            old_atoms_by_code=old_atoms_by_code,
        )
        if profile:
            query, y_mid = profile
            score = _score_new_atom_for_old_block(
                atom, query_text=query, old_y_mid=y_mid
            )
        else:
            name = (block.block_name or "").strip()
            score = _text_similarity(_atom_plain_text(atom), name) * 0.6
            if block.atom_codes:
                ys = [
                    _bbox_ymid(atoms_by_code[c].bbox_json)
                    for c in block.atom_codes
                    if c in atoms_by_code
                ]
                if ys:
                    ay = _bbox_ymid(atom.bbox_json)
                    score += max(0.0, 1.0 - min(abs(ay - y) for y in ys) * 3.0) * 0.4
        if score > best_score:
            best_score = score
            best_block = block
    if best_block and best_score >= min_score:
        return best_block
    if not page_blocks:
        return None
    ay = _bbox_ymid(atom.bbox_json)
    nearest: Block | None = None
    nearest_dist = float("inf")
    for block in page_blocks:
        ys = [
            _bbox_ymid(atoms_by_code[c].bbox_json)
            for c in (block.atom_codes or [])
            if c in atoms_by_code
        ]
        dist = min((abs(ay - y) for y in ys), default=0.5)
        if dist < nearest_dist:
            nearest_dist = dist
            nearest = block
    return nearest


def _block_new_page_range(
    block: Block,
    *,
    old_blocks_by_code: dict[str, Block] | None = None,
) -> tuple[int | None, int | None]:
    """新区块页码范围：优先 block 字段，否则锚定旧块页 ±1（紧凑版式）。"""
    start = block.textbook_page_start
    end = block.textbook_page_end or start
    if start is not None:
        return int(start), int(end or start)
    if not old_blocks_by_code:
        return None, None
    refs = (block.metadata_json or {}).get("anchor_old_refs") or []
    pages: set[int] = set()
    for ref in refs:
        code = str((ref or {}).get("old_block_code") or "").strip()
        ob = old_blocks_by_code.get(code)
        if ob and ob.textbook_page_start is not None:
            p = int(ob.textbook_page_start)
            pages.update({max(1, p - 1), p, p + 1})
    if not pages:
        return None, None
    return min(pages), max(pages)


def _atom_allowed_for_block(
    atom: TextbookAtom,
    block: Block,
    *,
    old_blocks_by_code: dict[str, Block],
) -> bool:
    from .atom_ocr_cleanup import is_low_value_image_atom

    lo, hi = _block_new_page_range(block, old_blocks_by_code=old_blocks_by_code)
    page = int(atom.page_index)
    if lo is None or hi is None:
        return True
    if is_low_value_image_atom(atom):
        return page == lo
    return lo <= page <= hi


def _page_affinity_for_old_block(
    atom_page: int,
    old_block: Block | None,
    *,
    new_page_count: int = 0,
) -> float:
    """旧块所在页与原子页越近分越高；无页码的旧块降权。"""
    if not old_block:
        return 0.25
    start = old_block.textbook_page_start
    end = old_block.textbook_page_end or start
    if start is None:
        return 0.2
    old_pages = {int(start)}
    if end is not None:
        old_pages.update(range(int(start), int(end) + 1))
    best = min(abs(atom_page - p) for p in old_pages)
    if new_page_count > 0:
        extra = new_page_count - max(old_pages)
        if extra > 0:
            shifted = {p + extra for p in old_pages}
            best = min(best, min(abs(atom_page - p) for p in shifted))
    if best == 0:
        return 1.0
    if best == 1:
        return 0.6
    return max(0.08, 0.5 - best * 0.18)


def rebalance_lesson_atoms_by_anchor_profile(
    *,
    lesson_uid: str,
    min_score: float = 0.02,
) -> dict:
    """课级按旧块 profile 重分原子（带旧块页码约束，允许 ±1 页偏移）。"""
    from ...old_library.annotate.workspace import _is_placeholder_atom

    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)
    paired = _primary_old_lesson(new_les.id)
    if not paired:
        return {"reassigned": 0}
    old_les, _ = paired
    old_atoms_by_code = {
        a.atom_code: a
        for a in TextbookAtom.query.filter_by(lesson_id=old_les.id).all()
    }
    old_blocks_by_code = {
        b.block_code: b
        for b in Block.query.filter_by(lesson_id=old_les.id).all()
    }
    new_page_count = LessonPage.query.filter_by(lesson_id=new_les.id).count()
    atoms = [
        a
        for a in TextbookAtom.query.filter_by(lesson_id=new_les.id).all()
        if not _is_placeholder_atom(a)
    ]
    atoms_by_code = {a.atom_code: a for a in atoms}
    blocks = list(Block.query.filter_by(lesson_id=new_les.id).all())
    if not blocks or not atoms:
        return {"reassigned": 0}

    atom_to_block: dict[str, Block] = {}
    for atom in sorted(atoms, key=lambda a: (int(a.page_index), _bbox_ymid(a.bbox_json))):
        best_block: Block | None = None
        best_score = 0.0
        page = int(atom.page_index)
        for block in blocks:
            refs = (block.metadata_json or {}).get("anchor_old_refs") or []
            old_code = str((refs[0] or {}).get("old_block_code") or "").strip() if refs else ""
            old_block = old_blocks_by_code.get(old_code) if old_code else None
            if not _atom_allowed_for_block(
                atom, block, old_blocks_by_code=old_blocks_by_code
            ):
                continue
            profile = _old_profile_for_anchored_block(
                block,
                old_les=old_les,
                old_atoms_by_code=old_atoms_by_code,
            )
            if profile and page >= new_page_count and new_page_count > 0:
                continue
            if profile:
                query, y_mid = profile
                score = _score_new_atom_for_old_block(
                    atom, query_text=query, old_y_mid=y_mid
                )
                affinity = _page_affinity_for_old_block(
                    page, old_block, new_page_count=new_page_count
                )
                if affinity < 0.35:
                    continue
                score *= affinity
            else:
                if int(atom.page_index) != page:
                    continue
                name = (block.block_name or "").strip()
                score = _text_similarity(_atom_plain_text(atom), name) * 0.65
                ys = [
                    _bbox_ymid(atoms_by_code[c].bbox_json)
                    for c in (block.atom_codes or [])
                    if c in atoms_by_code
                ]
                if ys:
                    ay = _bbox_ymid(atom.bbox_json)
                    score += max(0.0, 1.0 - min(abs(ay - y) for y in ys) * 2.5) * 0.35
            if score > best_score:
                best_score = score
                best_block = block
        if best_block and best_score >= min_score:
            atom_to_block[atom.atom_code] = best_block

    for block in blocks:
        block.atom_codes = []
    for code, block in atom_to_block.items():
        block.atom_codes = list(block.atom_codes or []) + [code]

    sync_block_page_ranges_from_atoms(blocks, atoms_by_code)
    db.session.flush()
    return {"reassigned": len(atom_to_block)}


def rebalance_mirrored_pages_by_anchor_profile(
    *,
    lesson_uid: str,
    page_indices: list[int] | None = None,
    min_score: float = 0.02,
) -> dict:
    """兼容旧调用：课级重分（page_indices 忽略）。"""
    del page_indices
    out = rebalance_lesson_atoms_by_anchor_profile(
        lesson_uid=lesson_uid,
        min_score=min_score,
    )
    return {"pages": {}, "reassigned": out.get("reassigned", 0)}


def sync_block_page_ranges_from_atoms(
    blocks: list[Block],
    atoms_by_code: dict[str, TextbookAtom],
) -> None:
    for block in blocks:
        pages = sorted(
            {
                int(atoms_by_code[c].page_index)
                for c in (block.atom_codes or [])
                if c in atoms_by_code
            }
        )
        if pages:
            block.textbook_page_start = min(pages)
            block.textbook_page_end = max(pages)


def _new_content_block_name(
    section_name: str,
    atom_codes: list[str],
    atoms_by_code: dict[str, TextbookAtom],
) -> str:
    if section_name in NEW_SECTION_BLOCK_NAMES:
        return NEW_SECTION_BLOCK_NAMES[section_name]
    texts = " ".join(
        _atom_plain_text(atoms_by_code[c])
        for c in atom_codes
        if c in atoms_by_code
    )
    if "巧克力" in texts or "试一试" in texts:
        return NEW_SECTION_BLOCK_NAMES["拓展迁移"]
    if "反思" in texts or "评价量" in texts:
        return NEW_SECTION_BLOCK_NAMES["反思评价"]
    return section_name or "未命名模块"


def seed_unassigned_atoms_as_new_blocks(
    *,
    lesson_uid: str,
    page_index: int,
    min_atoms: int = 1,
) -> dict:
    """为无旧块镜像的页（如 p.11）按栏目聚类创建 new 区块。"""
    from ..block_pipeline import apply_block_pipeline_metadata
    from ..block_pipeline_prepare import ensure_page_prepare, get_page_prepare
    from ...old_library.annotate.workspace import _is_placeholder_atom

    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)
    page = int(page_index)

    ensure_page_prepare(
        lesson_uid=new_les.lesson_uid,
        lesson_id=new_les.id,
        page_index=page,
        lesson_name=new_les.lesson_name or "",
    )
    page_ctx = get_page_prepare(new_les.lesson_uid, page) or {}

    atoms = [
        a
        for a in TextbookAtom.query.filter_by(lesson_id=new_les.id).all()
        if not _is_placeholder_atom(a)
    ]
    atoms_by_code = {a.atom_code: a for a in atoms}
    atom_owners, _ = block_occupancy(new_les.id)
    unassigned_codes = {
        a.atom_code
        for a in atoms
        if int(a.page_index) == page and a.atom_code not in atom_owners
    }
    if not unassigned_codes:
        return {"ok": True, "created": [], "updated": [], "created_count": 0}

    blocks = list(Block.query.filter_by(lesson_id=new_les.id).all())
    created: list[dict] = []
    updated: list[dict] = []
    next_num = Block.query.filter_by(lesson_id=new_les.id).count() + 1
    sort_order = Block.query.filter_by(lesson_id=new_les.id).count() + 1

    for cl in page_ctx.get("clusters") or []:
        codes = [
            str(c).strip()
            for c in (cl.get("atom_codes") or [])
            if str(c).strip() in unassigned_codes
        ]
        if len(codes) < min_atoms:
            continue
        section_name = str(cl.get("section_name") or "未命名模块").strip()
        name = _new_content_block_name(section_name, codes, atoms_by_code)
        existing = next(
            (
                b
                for b in blocks
                if _block_touches_page(b, page, atoms_by_code)
                and not (b.metadata_json or {}).get("anchor_old_refs")
                and (
                    section_name in (b.block_name or "")
                    or name in (b.block_name or "")
                )
            ),
            None,
        )
        if existing:
            merged = list(existing.atom_codes or [])
            for code in codes:
                if code not in merged:
                    merged.append(code)
            existing.atom_codes = merged
            page_indices = sorted(
                {
                    int(atoms_by_code[c].page_index)
                    for c in merged
                    if c in atoms_by_code
                }
            )
            if page_indices:
                existing.textbook_page_start = min(page_indices)
                existing.textbook_page_end = max(page_indices)
            for code in codes:
                unassigned_codes.discard(code)
            updated.append(
                {
                    "block_code": existing.block_code,
                    "block_name": existing.block_name,
                    "atom_codes": codes,
                    "section_name": section_name,
                }
            )
            continue

        block_code = f"{_block_code_prefix('new')}{next_num:02d}"
        next_num += 1
        page_indices = sorted(
            {int(atoms_by_code[c].page_index) for c in codes if c in atoms_by_code}
        )
        meta = apply_block_pipeline_metadata(
            {},
            source_path="new_content",
            ai_step="attributes",
            match_score=0.0,
            stage_ref=name,
        )
        meta["match_type"] = "new"
        block = Block(
            lesson_id=new_les.id,
            block_code=block_code,
            block_name=name,
            atom_codes=codes,
            course_slide_indices=None,
            textbook_page_start=min(page_indices) if page_indices else page,
            textbook_page_end=max(page_indices) if page_indices else page,
            sort_order=sort_order,
            metadata_json=meta,
        )
        db.session.add(block)
        blocks.append(block)
        sort_order += 1
        for code in codes:
            unassigned_codes.discard(code)
        created.append(
            {
                "block_code": block_code,
                "block_name": name,
                "atom_codes": codes,
                "section_name": section_name,
                "match_type": "new",
            }
        )

    db.session.flush()
    return {
        "ok": True,
        "new_page_index": page,
        "created": created,
        "updated": updated,
        "created_count": len(created),
        "leftover_unassigned": sorted(unassigned_codes),
    }


def apply_anchored_block_display_names(*, lesson_uid: str) -> dict:
    """补全锚定块的展示名称（如 N02/N06/N07）。"""
    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)
    updated: list[dict] = []
    for block in Block.query.filter_by(lesson_id=new_les.id).all():
        refs = (block.metadata_json or {}).get("anchor_old_refs") or []
        if not refs:
            continue
        old_code = str((refs[0] or {}).get("old_block_code") or "").strip()
        target = ANCHORED_BLOCK_DISPLAY_NAMES.get(old_code)
        if not target:
            continue
        current = (block.block_name or "").strip()
        generic = current in {"", "未命名模块", "科学探究", "问题情境", "拓展迁移"}
        if generic or len(current) < len(target) * 0.5:
            block.block_name = target
            meta = dict(block.metadata_json or {})
            meta["category"] = target
            block.metadata_json = meta
            updated.append({"block_code": block.block_code, "block_name": target})
    db.session.flush()
    return {"updated": updated, "updated_count": len(updated)}


def _move_image_between_blocks(
    *,
    assignments: dict[str, list[str]],
    atom_to_block: dict[str, str],
    img_code: str,
    target: str,
) -> bool:
    current = atom_to_block.get(img_code)
    if not current or current == target:
        return False
    assignments[current] = [c for c in assignments[current] if c != img_code]
    assignments.setdefault(target, []).append(img_code)
    atom_to_block[img_code] = target
    return True


def _section_bands_on_page(
    text_atoms: list[TextbookAtom],
) -> list[tuple[float, float, TextbookAtom | None]]:
    """按栏目头切 y 区间：(y0, y1, header_atom)。"""
    headers = sorted(
        [a for a in text_atoms if _is_section_header_atom(a)],
        key=lambda a: float((a.bbox_json or {}).get("y_start", 0)),
    )
    if not headers:
        return [(0.0, 1.0, None)]
    bands: list[tuple[float, float, TextbookAtom | None]] = []
    for idx, header in enumerate(headers):
        y0 = float((header.bbox_json or {}).get("y_start", 0))
        y1 = (
            float((headers[idx + 1].bbox_json or {}).get("y_start", 1.0))
            if idx + 1 < len(headers)
            else 1.0
        )
        bands.append((y0, y1, header))
    first_y = float((headers[0].bbox_json or {}).get("y_start", 0))
    if first_y > 0.02:
        bands.insert(0, (0.0, first_y, None))
    return bands


def _band_for_image(
    img: TextbookAtom,
    bands: list[tuple[float, float, TextbookAtom | None]],
) -> tuple[float, float, TextbookAtom | None] | None:
    img_y = _bbox_ymid(img.bbox_json)
    for y0, y1, header in bands:
        if y0 - 0.01 <= img_y <= y1 + 0.01:
            return (y0, y1, header)
    return None


def _nearest_section_header_above(
    img: TextbookAtom,
    text_atoms: list[TextbookAtom],
) -> TextbookAtom | None:
    img_y = _bbox_ymid(img.bbox_json)
    headers = [
        t
        for t in text_atoms
        if _is_section_header_atom(t) and _bbox_ymid(t.bbox_json) <= img_y + 0.03
    ]
    if not headers:
        return None
    return max(headers, key=lambda t: _bbox_ymid(t.bbox_json))


def _target_block_for_image(
    img: TextbookAtom,
    text_atoms: list[TextbookAtom],
    atom_to_block: dict[str, str],
    bands: list[tuple[float, float, TextbookAtom | None]],
) -> str | None:
    """栏目头 > label 语义 > 2D 邻近文字投票。"""
    header = _nearest_section_header_above(img, text_atoms)
    if header:
        blk = atom_to_block.get(header.atom_code)
        if blk:
            return blk

    band = _band_for_image(img, bands)
    band_texts = text_atoms
    if band:
        y0, y1, band_header = band
        band_texts = [
            t
            for t in text_atoms
            if y0 - 0.01 <= _bbox_ymid(t.bbox_json) <= y1 + 0.01
        ]
        if band_header and band_header.atom_code in atom_to_block:
            return atom_to_block[band_header.atom_code]

    label = _image_label_text(img)
    if label and band_texts:
        best_blk = ""
        best_sim = 0.0
        for t in band_texts:
            sim = _text_similarity(label, _atom_plain_text(t))
            blk = atom_to_block.get(t.atom_code)
            if blk and sim > best_sim:
                best_sim = sim
                best_blk = blk
        if best_blk and best_sim >= 0.06:
            return best_blk

    votes: Counter[str] = Counter()
    ranked = sorted(band_texts, key=lambda t: _bbox_2d_dist(img.bbox_json, t.bbox_json))
    for t in ranked[:3]:
        blk = atom_to_block.get(t.atom_code)
        if blk:
            votes[blk] += 1
    if votes:
        return votes.most_common(1)[0][0]
    return None


def _cluster_image_codes_on_page(
    image_codes: list[str],
    atoms_by_code: dict[str, TextbookAtom],
) -> list[list[str]]:
    """同页尺寸相近、横向并排或纵向紧挨的插图归为一组。"""
    if len(image_codes) < 2:
        return [[c] for c in image_codes]

    def _row_key(code: str) -> tuple[float, float]:
        b = atoms_by_code[code].bbox_json or {}
        return (_bbox_ymid(b), _bbox_xmid(b))

    sorted_codes = sorted(image_codes, key=_row_key)
    groups: list[list[str]] = []
    current: list[str] = [sorted_codes[0]]

    for code in sorted_codes[1:]:
        prev = atoms_by_code[current[-1]]
        cur = atoms_by_code[code]
        pb, cb = prev.bbox_json or {}, cur.bbox_json or {}
        y_gap = abs(_bbox_ymid(cb) - _bbox_ymid(pb))
        x_gap = abs(_bbox_xmid(cb) - _bbox_xmid(pb))
        pa, ca = _bbox_area(pb), _bbox_area(cb)
        size_ok = pa > 0 and ca > 0 and 0.25 <= (ca / pa) <= 4.0
        row_aligned = y_gap <= 0.07 and x_gap <= 0.45
        stack_aligned = y_gap <= 0.10 and x_gap <= 0.22
        if size_ok and (row_aligned or stack_aligned):
            current.append(code)
        else:
            groups.append(current)
            current = [code]
    groups.append(current)
    return groups


def _reassign_image_atoms_by_text_proximity(
    *,
    assignments: dict[str, list[str]],
    atoms_by_code: dict[str, TextbookAtom],
    page_index: int,
    top_k: int = 3,
) -> int:
    """P0+：图片跟栏目头 / label 语义 / 2D 邻近文字走，组图绑定同块。"""
    _ = top_k  # 保留签名兼容；邻近投票固定 top 3
    atom_to_block = _assignments_atom_to_block(assignments)
    text_atoms = [
        a
        for a in atoms_by_code.values()
        if int(a.page_index) == int(page_index)
        and not _is_image_like_atom(a)
        and _atom_plain_text(a)
        and a.atom_code in atom_to_block
    ]
    if not text_atoms:
        return 0

    bands = _section_bands_on_page(text_atoms)
    image_codes = [
        code
        for codes in assignments.values()
        for code in codes
        if (a := atoms_by_code.get(code))
        and _is_image_like_atom(a)
        and int(a.page_index) == int(page_index)
    ]

    moved = 0
    per_image_target: dict[str, str] = {}
    for img_code in image_codes:
        img = atoms_by_code.get(img_code)
        if not img:
            continue
        target = _target_block_for_image(img, text_atoms, atom_to_block, bands)
        if target:
            per_image_target[img_code] = target

    for img_code, target in per_image_target.items():
        if _move_image_between_blocks(
            assignments=assignments,
            atom_to_block=atom_to_block,
            img_code=img_code,
            target=target,
        ):
            moved += 1

    groups = _cluster_image_codes_on_page(image_codes, atoms_by_code)
    for group in groups:
        if len(group) < 2:
            continue
        votes: Counter[str] = Counter()
        for code in group:
            blk = atom_to_block.get(code)
            if blk:
                votes[blk] += 1
        if not votes:
            continue
        group_target = votes.most_common(1)[0][0]
        for code in group:
            if _move_image_between_blocks(
                assignments=assignments,
                atom_to_block=atom_to_block,
                img_code=code,
                target=group_target,
            ):
                moved += 1

    return moved


def _score_new_atom_for_old_block(
    atom: TextbookAtom,
    *,
    query_text: str,
    old_y_mid: float | None,
) -> float:
    atom_text = _atom_plain_text(atom)
    label = _image_label_text(atom) if _is_image_like_atom(atom) else ""
    if label:
        atom_text = label
    text_s = _text_similarity(atom_text, query_text) if query_text else 0.0
    if not atom_text and query_text:
        text_s = 0.05
    pos_s = 0.0
    if old_y_mid is not None:
        atom_y = _bbox_ymid(atom.bbox_json)
        pos_s = max(0.0, 1.0 - abs(atom_y - old_y_mid) * 2.5)
    if not query_text:
        return pos_s * 0.5
    if _is_image_like_atom(atom):
        if label:
            return text_s * 0.40 + pos_s * 0.60
        return text_s * 0.15 + pos_s * 0.85
    return text_s * 0.72 + pos_s * 0.28


def _old_blocks_already_anchored(
    new_blocks: list[Block],
    old_lesson_id: str,
) -> set[str]:
    anchored: set[str] = set()
    for block in new_blocks:
        refs = (block.metadata_json or {}).get("anchor_old_refs") or []
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            if ref.get("old_lesson_id") != old_lesson_id:
                continue
            code = str(ref.get("old_block_code") or "").strip()
            if code:
                anchored.add(code)
    return anchored


def _primary_old_lesson(new_lesson_id: str) -> tuple[Lesson, LessonMatch] | None:
    match = get_primary_lesson_match(new_lesson_id)
    if not match or not match.old_lesson_id:
        return None
    status = pair_review_status(match)
    if status == "no_old":
        return None
    old_les = Lesson.query.get(match.old_lesson_id)
    if not old_les:
        return None
    return old_les, match


def _paired_old_lesson(new_lesson_id: str) -> tuple[Lesson, LessonMatch] | None:
    """课内一键建块：须已确认主参照对照。"""
    match = get_primary_lesson_match(new_lesson_id)
    if not match or not match.old_lesson_id:
        return None
    if not allows_in_lesson_auto_pairing(pair_review_status(match)):
        return None
    old_les = Lesson.query.get(match.old_lesson_id)
    if not old_les:
        return None
    return old_les, match


def compute_old_mirror_preflight_metrics(
    *,
    new_lesson_id: str,
    old_lesson_id: str,
    min_score: float = 0.10,
) -> dict:
    """旧块镜像路径预检：覆盖率与 Top1 均值（dry-run，不写库）。"""
    from ...old_library.annotate.workspace import _is_placeholder_atom

    old_blocks = (
        Block.query.filter_by(lesson_id=old_lesson_id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    new_atoms_raw = TextbookAtom.query.filter_by(lesson_id=new_lesson_id).all()
    new_atoms = [a for a in new_atoms_raw if not _is_placeholder_atom(a)]
    old_atoms = TextbookAtom.query.filter_by(lesson_id=old_lesson_id).all()
    old_atoms_by_code = {a.atom_code: a for a in old_atoms}

    if not old_blocks:
        return {
            "old_blocks_exist": False,
            "coverage_rate": 0.0,
            "avg_top1_score": 0.0,
            "detail": "旧侧无教学区块",
        }
    if not new_atoms:
        return {
            "old_blocks_exist": False,
            "coverage_rate": 0.0,
            "avg_top1_score": 0.0,
            "detail": "新侧无 OCR 原子",
        }

    top1_scores: list[float] = []
    matched = 0
    for ob in old_blocks:
        query, y_mid = _block_query_and_ymid(ob, old_atoms_by_code)
        best = 0.0
        for atom in new_atoms:
            score = _score_new_atom_for_old_block(
                atom,
                query_text=query,
                old_y_mid=y_mid,
            )
            if score > best:
                best = score
        top1_scores.append(best)
        if best >= min_score:
            matched += 1

    coverage = matched / len(old_blocks) if old_blocks else 0.0
    avg_top1 = sum(top1_scores) / len(top1_scores) if top1_scores else 0.0

    atoms_assigned = 0
    for atom in new_atoms:
        best_atom = 0.0
        for ob in old_blocks:
            query, y_mid = _block_query_and_ymid(ob, old_atoms_by_code)
            score = _score_new_atom_for_old_block(
                atom,
                query_text=query,
                old_y_mid=y_mid,
            )
            if score > best_atom:
                best_atom = score
        if best_atom >= min_score:
            atoms_assigned += 1
    atom_assignment_rate = atoms_assigned / len(new_atoms) if new_atoms else 0.0

    return {
        "old_blocks_exist": True,
        "coverage_rate": coverage,
        "avg_top1_score": avg_top1,
        "atom_assignment_rate": round(atom_assignment_rate, 4),
        "old_block_count": len(old_blocks),
        "matched_block_count": matched,
        "new_atom_count": len(new_atoms),
        "assigned_atom_count": atoms_assigned,
    }


def _require_auto_pair_or_raise(new_les: Lesson) -> tuple[Lesson, LessonMatch]:
    paired = _paired_old_lesson(new_les.id)
    if paired:
        return paired
    match = get_primary_lesson_match(new_les.id)
    if match and not allows_in_lesson_auto_pairing(pair_review_status(match)):
        raise ValueError(
            "当前为「同课改动大」或「无对应旧课」，请用手动建块或「跨课找旧块」，"
            "勿使用课内一键建块"
        )
    raise ValueError("本课尚无粗分旧课配对，请先运行粗分")


def _build_rule_based_assignments(
    free_new_atoms: list[TextbookAtom],
    old_profiles: list[dict],
    *,
    min_score: float,
) -> tuple[dict[str, list[str]], list[str]]:
    assignments: dict[str, list[str]] = {
        prof["block"].block_code: [] for prof in old_profiles
    }
    leftover_atom_codes: list[str] = []
    for atom in free_new_atoms:
        best_code = ""
        best_score = 0.0
        for prof in old_profiles:
            if prof["skip_reason"]:
                continue
            ob = prof["block"]
            score = _score_new_atom_for_old_block(
                atom,
                query_text=prof["query"],
                old_y_mid=prof["y_mid"],
            )
            if score > best_score:
                best_score = score
                best_code = ob.block_code
        if best_code and best_score >= min_score:
            assignments[best_code].append(atom.atom_code)
        else:
            leftover_atom_codes.append(atom.atom_code)
    return assignments, leftover_atom_codes


def _finalize_assignments_with_image_fallback(
    assignments: dict[str, list[str]],
    leftover_atom_codes: list[str],
    *,
    new_atoms_by_code: dict[str, TextbookAtom],
    new_page: int,
) -> tuple[dict[str, list[str]], list[str], int]:
    atom_to_block_pre = _assignments_atom_to_block(assignments)
    text_atoms_assigned = [
        a
        for a in new_atoms_by_code.values()
        if int(a.page_index) == new_page
        and not _is_image_like_atom(a)
        and _atom_plain_text(a)
        and a.atom_code in atom_to_block_pre
    ]
    bands_pre = _section_bands_on_page(text_atoms_assigned)
    still_leftover: list[str] = []
    for code in leftover_atom_codes:
        atom = new_atoms_by_code.get(code)
        if not atom or not _is_image_like_atom(atom):
            still_leftover.append(code)
            continue
        target = _target_block_for_image(
            atom, text_atoms_assigned, atom_to_block_pre, bands_pre
        )
        if target:
            assignments.setdefault(target, []).append(code)
            atom_to_block_pre[code] = target
        else:
            still_leftover.append(code)
    image_reassigned = _reassign_image_atoms_by_text_proximity(
        assignments=assignments,
        atoms_by_code=new_atoms_by_code,
        page_index=new_page,
    )
    return assignments, still_leftover, image_reassigned


def seed_blocks_from_old_page(
    *,
    lesson_uid: str,
    new_page_index: int,
    old_page_index: int | None = None,
    replace_existing: bool = False,
    min_score: float = 0.10,
) -> dict:
    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)

    old_les, _match = _require_auto_pair_or_raise(new_les)
    old_page = int(old_page_index if old_page_index is not None else new_page_index)
    new_page = int(new_page_index)

    from ..block_pipeline_prepare import ensure_page_prepare

    ensure_page_prepare(
        lesson_uid=new_les.lesson_uid,
        lesson_id=new_les.id,
        page_index=new_page,
        lesson_name=new_les.lesson_name or "",
    )

    old_atoms = TextbookAtom.query.filter_by(lesson_id=old_les.id).all()
    new_atoms = TextbookAtom.query.filter_by(lesson_id=new_les.id).all()
    old_atoms_by_code = {a.atom_code: a for a in old_atoms}
    new_atoms_by_code = {a.atom_code: a for a in new_atoms}

    old_blocks = (
        Block.query.filter_by(lesson_id=old_les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    new_blocks = list(Block.query.filter_by(lesson_id=new_les.id).all())

    old_on_page = [
        b
        for b in old_blocks
        if _block_touches_page(b, old_page, old_atoms_by_code)
    ]
    if not old_on_page:
        raise ValueError(f"旧教材第 {old_page} 页无教学区块，请先在旧侧建块")

    atom_owners, _ = block_occupancy(new_les.id)
    bound_codes = set(atom_owners.keys())

    if replace_existing:
        to_remove = [
            b
            for b in new_blocks
            if _block_touches_page(b, new_page, new_atoms_by_code)
        ]
        remove_ids = {b.id for b in to_remove}
        for block in to_remove:
            db.session.delete(block)
        db.session.flush()
        new_blocks = [b for b in new_blocks if b.id not in remove_ids]
        bound_codes = {
            code
            for code, owner in atom_owners.items()
            if owner.id not in remove_ids
        }

    free_new_atoms = [
        a
        for a in new_atoms
        if int(a.page_index) == new_page and a.atom_code not in bound_codes
    ]

    if not free_new_atoms:
        raise ValueError(
            "新教材本页无可用原子，请先 OCR 本页，或勾选替换已有新块"
        )

    already_anchored = _old_blocks_already_anchored(new_blocks, old_les.id)
    old_profiles: list[dict] = []
    for ob in old_on_page:
        query, y_mid = _block_query_and_ymid(ob, old_atoms_by_code)
        skip = "already_anchored" if ob.block_code in already_anchored else ""
        old_profiles.append(
            {
                "block": ob,
                "query": query,
                "y_mid": y_mid,
                "skip_reason": skip,
            }
        )

    from .ai_seed_from_old_page import try_llm_mirror_assignments

    llm_warnings: list[str] = []
    assignment_source = "old_mirror"

    llm_assignments, llm_src, llm_warnings = try_llm_mirror_assignments(
        new_les=new_les,
        old_les=old_les,
        new_page_index=new_page,
        old_page_index=old_page,
        old_profiles=old_profiles,
        free_new_atoms=free_new_atoms,
        old_atoms_by_code=old_atoms_by_code,
    )
    if llm_assignments is not None:
        assignments = {
            prof["block"].block_code: list(
                llm_assignments.get(prof["block"].block_code) or []
            )
            for prof in old_profiles
        }
        leftover_atom_codes: list[str] = []
        assignment_source = llm_src
    else:
        assignments, leftover_atom_codes = _build_rule_based_assignments(
            free_new_atoms,
            old_profiles,
            min_score=min_score,
        )

    assignments, _dedupe_left = _dedupe_atom_assignments(
        assignments,
        old_profiles,
        new_atoms_by_code,
        min_score=0.0,
    )
    assigned_codes = {
        code
        for codes in assignments.values()
        for code in codes
    }
    leftover_atom_codes = [
        a.atom_code
        for a in free_new_atoms
        if a.atom_code not in assigned_codes
    ]
    leftover_atom_codes = _absorb_leftover_atoms(
        assignments,
        leftover_atom_codes,
        old_profiles,
        new_atoms_by_code,
        fallback_min_score=max(0.03, min_score * 0.5),
    )

    assignments, leftover_atom_codes, image_reassigned = (
        _finalize_assignments_with_image_fallback(
            assignments,
            leftover_atom_codes,
            new_atoms_by_code=new_atoms_by_code,
            new_page=new_page,
        )
    )

    old_vol = Volume.query.get(old_les.volume_id)
    vol_label = old_vol.display_title if old_vol else ""

    created: list[dict] = []
    skipped: list[dict] = []
    next_block_num = Block.query.filter_by(lesson_id=new_les.id).count() + 1
    sort_order = Block.query.filter_by(lesson_id=new_les.id).count() + 1

    for prof in old_profiles:
        ob = prof["block"]
        if prof["skip_reason"]:
            skipped.append(
                {
                    "old_block_code": ob.block_code,
                    "old_block_name": ob.block_name,
                    "reason": prof["skip_reason"],
                }
            )
            continue
        atom_codes = assignments.get(ob.block_code) or []
        if not atom_codes:
            skipped.append(
                {
                    "old_block_code": ob.block_code,
                    "old_block_name": ob.block_name,
                    "reason": "no_matching_atoms",
                }
            )
            continue

        block_code = f"{_block_code_prefix('new')}{next_block_num:02d}"
        next_block_num += 1
        new_name = _suggest_new_block_name_from_old(ob.block_name)
        anchor_ref = {
            "old_lesson_id": old_les.id,
            "old_lesson_uid": old_les.lesson_uid,
            "old_block_code": ob.block_code,
            "old_block_name": ob.block_name,
            "old_page_index": old_page,
            "volume_label": vol_label,
            "unit_title": old_les.unit_title or "",
            "lesson_name": old_les.lesson_name or "",
        }
        match_score = _profile_block_match_score(atom_codes, prof, new_atoms_by_code)
        match_score_top2 = 0.0
        for other in old_profiles:
            if other["block"].block_code == ob.block_code:
                continue
            alt = _profile_block_match_score(atom_codes, other, new_atoms_by_code)
            if alt > match_score_top2:
                match_score_top2 = alt
        page_indices = sorted(
            {
                new_atoms_by_code[c].page_index
                for c in atom_codes
                if c in new_atoms_by_code
            }
        )
        from ..block_pipeline import apply_block_pipeline_metadata

        meta = apply_block_pipeline_metadata(
            {"anchor_old_refs": [anchor_ref]},
            source_path=assignment_source,
            ai_step="anchor",
            match_score=match_score,
            match_score_top2=match_score_top2,
            stage_ref=new_name,
        )
        block = Block(
            lesson_id=new_les.id,
            block_code=block_code,
            block_name=new_name,
            atom_codes=atom_codes,
            course_slide_indices=None,
            textbook_page_start=min(page_indices) if page_indices else new_page,
            textbook_page_end=max(page_indices) if page_indices else new_page,
            sort_order=sort_order,
            metadata_json=meta,
        )
        db.session.add(block)
        sort_order += 1
        created.append(
            {
                "block_code": block_code,
                "block_name": new_name,
                "atom_codes": atom_codes,
                "old_block_code": ob.block_code,
                "old_block_name": ob.block_name,
                "match_score": round(match_score, 4),
                "match_score_top2": round(match_score_top2, 4),
            }
        )

    if not created:
        db.session.rollback()
        raise ValueError("未能为任何旧区块匹配到新原子，请检查 OCR 或调低 min_score")

    db.session.commit()

    return {
        "ok": True,
        "new_page_index": new_page,
        "old_page_index": old_page,
        "assignment_source": assignment_source,
        "llm_warnings": llm_warnings,
        "created": created,
        "skipped": skipped,
        "leftover_atom_codes": leftover_atom_codes,
        "image_atoms_reassigned": image_reassigned,
        "created_count": len(created),
    }


def _rule_anchor_pair_for_block(
    new_b: Block,
    old_on_page: list[Block],
    used_old: set[str],
    *,
    new_atoms_by_code: dict[str, TextbookAtom],
    old_atoms_by_code: dict[str, TextbookAtom],
) -> tuple[Block | None, float]:
    _, new_y = _block_query_and_ymid(new_b, new_atoms_by_code)
    best_ob: Block | None = None
    best_score = 0.0
    for ob in old_on_page:
        if ob.block_code in used_old:
            continue
        _, old_y = _block_query_and_ymid(ob, old_atoms_by_code)
        name_hint = _suggest_new_block_name_from_old(ob.block_name or "")
        name_s = _text_similarity(
            (new_b.block_name or "").strip(),
            name_hint or (ob.block_name or "").strip(),
        )
        if new_y is not None and old_y is not None:
            pos_s = max(0.0, 1.0 - abs(new_y - old_y) * 3.0)
        else:
            pos_s = 0.0
        score = name_s * 0.55 + pos_s * 0.45
        if score > best_score:
            best_score = score
            best_ob = ob
    return best_ob, best_score


def anchor_existing_blocks_on_page(
    *,
    lesson_uid: str,
    new_page_index: int,
    old_page_index: int | None = None,
    min_score: float = 0.12,
    replace_existing: bool = False,
) -> dict:
    """为本页已有新区块补写 anchor_old_refs（不新建块、不改原子）。"""
    from .ai_seed_from_old_page import try_llm_anchor_matches
    from .anchors import build_anchor_old_ref

    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)

    paired = _primary_old_lesson(new_les.id)
    if not paired:
        match = get_primary_lesson_match(new_les.id)
        if match and pair_review_status(match) == "no_old":
            raise ValueError("已标记无旧课对应，请用「跨课找旧块」标注来源")
        raise ValueError("本课尚无粗分旧课配对，请先运行粗分")
    old_les, _match = paired
    old_page = int(old_page_index if old_page_index is not None else new_page_index)
    new_page = int(new_page_index)

    from ..block_pipeline_prepare import ensure_page_prepare

    ensure_page_prepare(
        lesson_uid=new_les.lesson_uid,
        lesson_id=new_les.id,
        page_index=new_page,
        lesson_name=new_les.lesson_name or "",
    )

    old_atoms = TextbookAtom.query.filter_by(lesson_id=old_les.id).all()
    new_atoms = TextbookAtom.query.filter_by(lesson_id=new_les.id).all()
    old_atoms_by_code = {a.atom_code: a for a in old_atoms}
    new_atoms_by_code = {a.atom_code: a for a in new_atoms}

    old_blocks = Block.query.filter_by(lesson_id=old_les.id).all()
    new_blocks = Block.query.filter_by(lesson_id=new_les.id).all()

    old_on_page = [
        b
        for b in old_blocks
        if _block_touches_page(b, old_page, old_atoms_by_code)
    ]
    if not old_on_page:
        raise ValueError(f"旧教材第 {old_page} 页无教学区块")

    new_on_page = [
        b
        for b in new_blocks
        if _block_touches_page(b, new_page, new_atoms_by_code)
    ]
    if replace_existing:
        to_anchor = list(new_on_page)
    else:
        to_anchor = [
            b
            for b in new_on_page
            if not (b.metadata_json or {}).get("anchor_old_refs")
        ]
    if not to_anchor:
        raise ValueError("本页新区块均已锚定，无需补写")

    used_old: set[str] = set()
    for block in new_blocks:
        if replace_existing and block in to_anchor:
            continue
        for ref in (block.metadata_json or {}).get("anchor_old_refs") or []:
            code = str(ref.get("old_block_code") or "").strip()
            if code:
                used_old.add(code)

    llm_matches, anchor_source, llm_warnings = try_llm_anchor_matches(
        new_les=new_les,
        old_les=old_les,
        new_page_index=new_page,
        old_page_index=old_page,
        new_blocks=to_anchor,
        old_blocks=old_on_page,
        used_old_codes=used_old,
        new_atoms_by_code=new_atoms_by_code,
        old_atoms_by_code=old_atoms_by_code,
    )

    updated: list[dict] = []
    skipped: list[dict] = []
    pending = sorted(to_anchor, key=lambda b: (b.sort_order or 0, b.block_code))

    for new_b in pending:
        best_ob: Block | None = None
        best_score = 0.0
        match_source = "rules"
        if llm_matches is not None:
            old_code = llm_matches.get(new_b.block_code)
            if old_code:
                best_ob = next(
                    (ob for ob in old_on_page if ob.block_code == old_code),
                    None,
                )
                if best_ob and best_ob.block_code not in used_old:
                    best_score = 1.0
                    match_source = anchor_source
                else:
                    best_ob = None
        if not best_ob:
            best_ob, best_score = _rule_anchor_pair_for_block(
                new_b,
                old_on_page,
                used_old,
                new_atoms_by_code=new_atoms_by_code,
                old_atoms_by_code=old_atoms_by_code,
            )
        if not best_ob or best_score < min_score:
            skipped.append(
                {
                    "new_block_code": new_b.block_code,
                    "reason": "no_old_match",
                    "best_score": round(best_score, 3),
                }
            )
            continue
        ref = build_anchor_old_ref(
            new_lesson_id=new_les.id,
            old_block_code=best_ob.block_code,
            old_page_index=old_page,
        )
        if not ref:
            skipped.append(
                {
                    "new_block_code": new_b.block_code,
                    "reason": "anchor_build_failed",
                }
            )
            continue
        meta = dict(new_b.metadata_json or {})
        meta["anchor_old_refs"] = [ref]
        from ..block_pipeline import apply_block_pipeline_metadata

        meta = apply_block_pipeline_metadata(
            meta,
            source_path=match_source,
            ai_step="anchor",
            match_score=best_score,
        )
        new_b.metadata_json = meta
        used_old.add(best_ob.block_code)
        updated.append(
            {
                "new_block_code": new_b.block_code,
                "old_block_code": best_ob.block_code,
                "score": round(best_score, 3),
                "source": match_source,
            }
        )

    if not updated:
        db.session.rollback()
        raise ValueError("未能为任何新区块找到对照旧块，请手动点旧块或调低 min_score")

    db.session.commit()
    return {
        "ok": True,
        "new_page_index": new_page,
        "old_page_index": old_page,
        "anchor_source": anchor_source,
        "llm_warnings": llm_warnings,
        "updated": updated,
        "skipped": skipped,
        "updated_count": len(updated),
    }


def seed_blocks_from_old_lesson(
    *,
    lesson_uid: str,
    replace_existing: bool = False,
    min_score: float = 0.10,
    run_anchor: bool = True,
) -> dict:
    """对照粗分旧课，在本课全部教材页自动建块（可选整课补锚定）。"""
    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)
    page_indices = _lesson_page_indices(new_les.id)
    if not page_indices:
        raise ValueError("本课无教材页（请先在 intake 生成页图，或完成 OCR）")

    created_total = 0
    new_content_total = 0
    page_results: list[dict] = []
    errors: list[str] = []
    for pi in page_indices:
        try:
            result = seed_blocks_from_old_page(
                lesson_uid=lesson_uid,
                new_page_index=pi,
                old_page_index=pi,
                replace_existing=replace_existing,
                min_score=min_score,
            )
            n = int(result.get("created_count") or 0)
            created_total += n
            if n:
                page_results.append(
                    {"page_index": pi, "created_count": n, "source": "old_mirror"}
                )
        except ValueError as exc:
            db.session.rollback()
            errors.append(f"P{pi}: {exc}")
        except Exception as exc:
            db.session.rollback()
            errors.append(f"P{pi}: {exc}")

    # 新课独有页（如反思评价/拓展迁移）旧侧同页码常无块，需按栏目聚类补建。
    for pi in page_indices:
        try:
            orphan = seed_unassigned_atoms_as_new_blocks(
                lesson_uid=lesson_uid,
                page_index=pi,
            )
            n = int(orphan.get("created_count") or 0)
            new_content_total += n
            created_total += n
            if n:
                page_results.append(
                    {"page_index": pi, "created_count": n, "source": "new_content"}
                )
        except ValueError as exc:
            db.session.rollback()
            errors.append(f"P{pi} 新课补块: {exc}")
        except Exception as exc:
            db.session.rollback()
            errors.append(f"P{pi} 新课补块: {exc}")

    if new_content_total:
        apply_anchored_block_display_names(lesson_uid=lesson_uid)
        db.session.commit()

    if created_total == 0:
        msg = "未能建任何新区块"
        if errors:
            msg += "：" + "；".join(errors[:4])
            if len(errors) > 4:
                msg += f" 等 {len(errors)} 页"
        raise ValueError(msg)

    anchor_result: dict | None = None
    if run_anchor:
        try:
            anchor_result = anchor_all_unanchored_in_lesson(
                lesson_uid=lesson_uid,
                replace_existing=True,
                min_score=max(min_score, 0.12),
            )
        except ValueError as exc:
            anchor_result = {"ok": False, "error": str(exc)}
        except Exception as exc:
            db.session.rollback()
            anchor_result = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "created_count": created_total,
        "old_mirror_count": created_total - new_content_total,
        "new_content_count": new_content_total,
        "pages_seeded": len(page_results),
        "page_count": len(page_indices),
        "page_results": page_results,
        "errors": errors,
        "anchor": anchor_result,
    }


def _lesson_page_indices(lesson_id: str) -> list[int]:
    """课内页序号：优先 lesson_pages，否则从原子 page_index 推断。"""
    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    if pages:
        return [int(p.page_index) for p in pages]
    rows = (
        db.session.query(TextbookAtom.page_index)
        .filter_by(lesson_id=lesson_id)
        .distinct()
        .all()
    )
    indices = sorted({int(r[0]) for r in rows if r[0] is not None})
    return indices


def anchor_all_unanchored_in_lesson(
    *,
    lesson_uid: str,
    replace_existing: bool = True,
    min_score: float = 0.12,
) -> dict:
    """整课补锚定：逐页豆包锚定（含 1–3 步分析）。"""
    from ....models import LessonPage

    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    pages = (
        LessonPage.query.filter_by(lesson_id=new_les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    if not pages:
        raise ValueError("本课无教材页")

    all_updated: list[dict] = []
    all_skipped: list[dict] = []
    llm_warnings: list[str] = []
    pages_touched = 0

    for page in pages:
        try:
            result = anchor_existing_blocks_on_page(
                lesson_uid=lesson_uid,
                new_page_index=int(page.page_index),
                old_page_index=int(page.page_index),
                min_score=min_score,
                replace_existing=replace_existing,
            )
            pages_touched += 1
            all_updated.extend(result.get("updated") or [])
            all_skipped.extend(result.get("skipped") or [])
            llm_warnings.extend(result.get("llm_warnings") or [])
        except ValueError:
            db.session.rollback()
            continue

    if not all_updated:
        raise ValueError("未能锚定任何区块，请确认已有新区块且粗分旧课可用")

    return {
        "ok": True,
        "updated": all_updated,
        "skipped": all_skipped,
        "updated_count": len(all_updated),
        "pages_touched": pages_touched,
        "llm_warnings": llm_warnings,
        "anchor_source": "anchor_llm",
    }
