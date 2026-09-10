"""建块流水线 1–3 步：栏目识别 → 主题聚类 → 大小均衡。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ...models import Block, LessonPage, TextbookAtom
from ..llm.page_section_detect import detect_page_sections_with_llm
from .annotate.seed_from_old_page import (
    OLD_TO_NEW_BLOCK_NAME_HINTS,
    _atom_plain_text,
    _bbox_ymid,
    _is_image_like_atom,
    _is_section_header_atom,
    _section_bands_on_page,
)

logger = logging.getLogger(__name__)

_MIN_CLUSTER_ATOMS = 2
_PREPARE_BY_LESSON: dict[str, dict[int, dict[str, Any]]] = {}


@dataclass
class TopicCluster:
    cluster_id: str
    section_name: str
    atom_codes: list[str] = field(default_factory=list)
    y_mid: float = 0.5


@dataclass
class PagePrepareResult:
    page_index: int
    sections: list[dict[str, Any]] = field(default_factory=list)
    clusters: list[TopicCluster] = field(default_factory=list)
    column_source: str = "rules"
    balance_merged: int = 0


def get_page_prepare(lesson_uid: str, page_index: int) -> dict[str, Any] | None:
    return _PREPARE_BY_LESSON.get(lesson_uid, {}).get(int(page_index))


def set_lesson_prepare(lesson_uid: str, pages: dict[int, dict[str, Any]]) -> None:
    _PREPARE_BY_LESSON[lesson_uid] = pages


def clear_lesson_prepare(lesson_uid: str) -> None:
    _PREPARE_BY_LESSON.pop(lesson_uid, None)


def _section_name_from_header(atom: TextbookAtom) -> str:
    text = _atom_plain_text(atom)
    compact = text.replace(" ", "")
    for kw, label in OLD_TO_NEW_BLOCK_NAME_HINTS:
        if kw in compact or kw in text:
            return label
    return compact[:12] or "未命名栏目"


def _bands_from_rule_atoms(text_atoms: list[TextbookAtom]) -> list[dict[str, Any]]:
    bands = _section_bands_on_page(text_atoms)
    out: list[dict[str, Any]] = []
    for y0, y1, header in bands:
        name = _section_name_from_header(header) if header else "正文区"
        out.append(
            {
                "section_name": name,
                "y_start": y0,
                "y_end": y1,
                "source": "rules",
                "header_atom_code": header.atom_code if header else None,
            }
        )
    return out


def detect_page_columns(
    *,
    page_index: int,
    atoms: list[TextbookAtom],
    blob_id: str | None,
    lesson_name: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """① 栏目识别：规则栏目头 + 不足时豆包补充。"""
    text_atoms = [
        a
        for a in atoms
        if int(a.page_index) == int(page_index)
        and not _is_image_like_atom(a)
        and _atom_plain_text(a)
    ]
    headers = [a for a in text_atoms if _is_section_header_atom(a)]
    sections = _bands_from_rule_atoms(text_atoms)

    if len(headers) >= 2:
        return sections, "rules"

    llm_sections = detect_page_sections_with_llm(
        page_index=page_index,
        blob_id=blob_id,
        lesson_name=lesson_name,
    )
    if llm_sections:
        return llm_sections, "llm"

    if sections:
        return sections, "rules"
    return [
        {
            "section_name": "正文区",
            "y_start": 0.0,
            "y_end": 1.0,
            "source": "fallback",
            "header_atom_code": None,
        }
    ], "fallback"


def _section_for_atom(
    atom: TextbookAtom,
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    y = _bbox_ymid(atom.bbox_json)
    for sec in sections:
        if float(sec["y_start"]) - 0.01 <= y <= float(sec["y_end"]) + 0.01:
            return sec
    return sections[-1] if sections else {"section_name": "正文区"}


def cluster_page_topics(
    *,
    page_index: int,
    atoms: list[TextbookAtom],
    sections: list[dict[str, Any]],
) -> list[TopicCluster]:
    """② 主题聚类：按栏目 y 带把本页原子分组。"""
    page_atoms = [a for a in atoms if int(a.page_index) == int(page_index)]
    if not page_atoms:
        return []

    by_section: dict[str, list[TextbookAtom]] = {}
    for atom in page_atoms:
        sec = _section_for_atom(atom, sections)
        key = str(sec.get("section_name") or "正文区")
        by_section.setdefault(key, []).append(atom)

    clusters: list[TopicCluster] = []
    for idx, (section_name, group) in enumerate(
        sorted(by_section.items(), key=lambda kv: _bbox_ymid(kv[1][0].bbox_json))
    ):
        codes = [a.atom_code for a in sorted(group, key=lambda a: _bbox_ymid(a.bbox_json))]
        ys = [_bbox_ymid(a.bbox_json) for a in group]
        y_mid = sum(ys) / len(ys) if ys else 0.5
        clusters.append(
            TopicCluster(
                cluster_id=f"P{page_index}-C{idx + 1:02d}",
                section_name=section_name,
                atom_codes=codes,
                y_mid=y_mid,
            )
        )
    return clusters


def balance_page_clusters(clusters: list[TopicCluster]) -> tuple[list[TopicCluster], int]:
    """③ 大小均衡：合并同栏目下过小的相邻聚类。"""
    if len(clusters) < 2:
        return clusters, 0

    merged_count = 0
    out: list[TopicCluster] = list(clusters)
    changed = True
    while changed and len(out) >= 2:
        changed = False
        next_out: list[TopicCluster] = []
        i = 0
        while i < len(out):
            cur = out[i]
            if (
                len(cur.atom_codes) < _MIN_CLUSTER_ATOMS
                and i + 1 < len(out)
                and out[i + 1].section_name == cur.section_name
            ):
                nxt = out[i + 1]
                combined = TopicCluster(
                    cluster_id=cur.cluster_id,
                    section_name=cur.section_name,
                    atom_codes=cur.atom_codes + nxt.atom_codes,
                    y_mid=(cur.y_mid + nxt.y_mid) / 2.0,
                )
                next_out.append(combined)
                merged_count += 1
                changed = True
                i += 2
            else:
                next_out.append(cur)
                i += 1
        out = next_out
    return out, merged_count


def prepare_page(
    *,
    page_index: int,
    atoms: list[TextbookAtom],
    blob_id: str | None,
    lesson_name: str = "",
) -> PagePrepareResult:
    sections, column_source = detect_page_columns(
        page_index=page_index,
        atoms=atoms,
        blob_id=blob_id,
        lesson_name=lesson_name,
    )
    clusters = cluster_page_topics(
        page_index=page_index,
        atoms=atoms,
        sections=sections,
    )
    balanced, merged = balance_page_clusters(clusters)
    return PagePrepareResult(
        page_index=int(page_index),
        sections=sections,
        clusters=balanced,
        column_source=column_source,
        balance_merged=merged,
    )


def page_prepare_to_dict(result: PagePrepareResult) -> dict[str, Any]:
    return {
        "page_index": result.page_index,
        "sections": result.sections,
        "clusters": [
            {
                "cluster_id": c.cluster_id,
                "section_name": c.section_name,
                "atom_codes": c.atom_codes,
                "y_mid": c.y_mid,
            }
            for c in result.clusters
        ],
        "column_source": result.column_source,
        "balance_merged": result.balance_merged,
    }


def run_column_detect_step(
    *,
    lesson_id: str,
    lesson_uid: str,
    lesson_name: str = "",
) -> dict[int, dict[str, Any]]:
    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    atoms = TextbookAtom.query.filter_by(lesson_id=lesson_id).all()
    partial: dict[int, dict[str, Any]] = {}
    for page in pages:
        pi = int(page.page_index)
        sections, column_source = detect_page_columns(
            page_index=pi,
            atoms=atoms,
            blob_id=page.blob_id,
            lesson_name=lesson_name,
        )
        partial[pi] = {
            "page_index": pi,
            "sections": sections,
            "column_source": column_source,
            "clusters": [],
            "balance_merged": 0,
        }
    set_lesson_prepare(lesson_uid, partial)
    return partial


def run_topic_cluster_step(
    *,
    lesson_id: str,
    lesson_uid: str,
) -> dict[int, dict[str, Any]]:
    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    atoms = TextbookAtom.query.filter_by(lesson_id=lesson_id).all()
    cache = _PREPARE_BY_LESSON.get(lesson_uid, {})
    for page in pages:
        pi = int(page.page_index)
        entry = cache.get(pi) or {"page_index": pi, "sections": [], "column_source": "rules"}
        clusters = cluster_page_topics(
            page_index=pi,
            atoms=atoms,
            sections=entry.get("sections") or [],
        )
        entry["clusters"] = [
            {
                "cluster_id": c.cluster_id,
                "section_name": c.section_name,
                "atom_codes": c.atom_codes,
                "y_mid": c.y_mid,
            }
            for c in clusters
        ]
        cache[pi] = entry
    set_lesson_prepare(lesson_uid, cache)
    return cache


def run_balance_step(*, lesson_uid: str) -> dict[int, dict[str, Any]]:
    cache = _PREPARE_BY_LESSON.get(lesson_uid, {})
    for pi, entry in cache.items():
        raw_clusters = entry.get("clusters") or []
        clusters = [
            TopicCluster(
                cluster_id=str(c["cluster_id"]),
                section_name=str(c.get("section_name") or ""),
                atom_codes=list(c.get("atom_codes") or []),
                y_mid=float(c.get("y_mid") or 0.5),
            )
            for c in raw_clusters
        ]
        balanced, merged = balance_page_clusters(clusters)
        entry["clusters"] = [
            {
                "cluster_id": c.cluster_id,
                "section_name": c.section_name,
                "atom_codes": c.atom_codes,
                "y_mid": c.y_mid,
            }
            for c in balanced
        ]
        entry["balance_merged"] = merged
        cache[pi] = entry
    set_lesson_prepare(lesson_uid, cache)
    return cache


def ensure_page_prepare(
    *,
    lesson_uid: str,
    lesson_id: str,
    page_index: int,
    lesson_name: str = "",
) -> dict[str, Any]:
    """单页建块/锚定前确保 1–3 步分析结果存在。"""
    cached = get_page_prepare(lesson_uid, page_index)
    if cached:
        return cached
    atoms = TextbookAtom.query.filter_by(lesson_id=lesson_id).all()
    lp = LessonPage.query.filter_by(
        lesson_id=lesson_id,
        page_index=int(page_index),
    ).first()
    prep = prepare_page(
        page_index=int(page_index),
        atoms=atoms,
        blob_id=lp.blob_id if lp else None,
        lesson_name=lesson_name,
    )
    cache = dict(_PREPARE_BY_LESSON.get(lesson_uid) or {})
    cache[int(page_index)] = page_prepare_to_dict(prep)
    set_lesson_prepare(lesson_uid, cache)
    return cache[int(page_index)]


def format_prepare_hint(page_ctx: dict[str, Any] | None) -> str:
    if not page_ctx:
        return ""
    lines = ["═══ 本页建块前分析（栏目→聚类）═══"]
    for sec in page_ctx.get("sections") or []:
        lines.append(
            f"栏目「{sec.get('section_name')}」"
            f" y={float(sec.get('y_start', 0)):.2f}-{float(sec.get('y_end', 1)):.2f}"
            f" ({sec.get('source') or 'rules'})"
        )
    for cl in page_ctx.get("clusters") or []:
        codes = cl.get("atom_codes") or []
        preview = ", ".join(codes[:6])
        if len(codes) > 6:
            preview += f" …共{len(codes)}个"
        lines.append(
            f"聚类 {cl.get('cluster_id')}「{cl.get('section_name')}」: {preview or '（空）'}"
        )
    lines.append("同聚类原子应分配到同一旧块；勿跨栏目拆分。")
    return "\n".join(lines)


def seed_blocks_from_page_clusters(
    *,
    lesson_uid: str,
    page_index: int,
    page_ctx: dict[str, Any],
    replace_existing: bool = False,
) -> dict:
    """new_cluster 路径：按聚类创建新区块（无旧块锚定）。"""
    from ...extensions import db
    from ..lesson_lookup import get_lesson_by_uid
    from ..old_library.annotate.blocks import _block_code_prefix, _ensure_blocks_editable
    from .block_pipeline import apply_block_pipeline_metadata

    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(new_les)
    page = int(page_index)
    clusters = page_ctx.get("clusters") or []
    if not clusters:
        raise ValueError(f"第 {page} 页无可用聚类，请先 OCR")

    atoms = {
        a.atom_code: a
        for a in TextbookAtom.query.filter_by(lesson_id=new_les.id).all()
    }
    if replace_existing:
        to_remove = Block.query.filter_by(lesson_id=new_les.id).all()
        remove_ids = set()
        for block in to_remove:
            codes = block.atom_codes or []
            if any(
                atoms.get(str(c)) and int(atoms[str(c)].page_index) == page
                for c in codes
            ):
                db.session.delete(block)
                remove_ids.add(block.id)
        db.session.flush()

    created: list[dict] = []
    next_num = Block.query.filter_by(lesson_id=new_les.id).count() + 1
    sort_order = Block.query.filter_by(lesson_id=new_les.id).count() + 1
    for cl in clusters:
        codes = [c for c in (cl.get("atom_codes") or []) if c in atoms]
        if not codes:
            continue
        name = str(cl.get("section_name") or "未命名模块").strip()
        block_code = f"{_block_code_prefix('new')}{next_num:02d}"
        next_num += 1
        page_indices = sorted({int(atoms[c].page_index) for c in codes})
        meta = apply_block_pipeline_metadata(
            {},
            source_path="new_cluster",
            ai_step="attributes",
            stage_ref=name,
        )
        block = Block(
            lesson_id=new_les.id,
            block_code=block_code,
            block_name=name,
            atom_codes=codes,
            course_slide_indices=None,
            textbook_page_start=min(page_indices),
            textbook_page_end=max(page_indices),
            sort_order=sort_order,
            metadata_json=meta,
        )
        db.session.add(block)
        sort_order += 1
        created.append(
            {
                "block_code": block_code,
                "block_name": name,
                "atom_codes": codes,
                "cluster_id": cl.get("cluster_id"),
            }
        )

    if not created:
        db.session.rollback()
        raise ValueError(f"第 {page} 页未能从聚类创建区块")

    db.session.commit()
    return {
        "ok": True,
        "new_page_index": page,
        "created": created,
        "created_count": len(created),
        "source_path": "new_cluster",
    }
