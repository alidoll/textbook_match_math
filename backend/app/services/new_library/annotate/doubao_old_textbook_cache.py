"""双轨专用：旧教材豆包文字原子（仅缓存，不写旧库）。"""
from __future__ import annotations

import logging
import re
from typing import Any

from ...llm.page_text_extract import (
    dual_track_textbook_uses_doubao,
    extract_page_text_regions,
    load_page_text_cache,
    regions_to_raw_atoms,
)

logger = logging.getLogger(__name__)

DOUBAO_ATOM_PREFIX = "D"  # D004-001，与旧库 A004-001 区分


def _doubao_atom_code(page_index: int, seq: int) -> str:
    return f"{DOUBAO_ATOM_PREFIX}{int(page_index):03d}-{int(seq):03d}"


def _raw_to_doubao_atoms(raw_atoms: list[dict[str, Any]], *, page_index: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw_atoms, start=1):
        text = (item.get("content") or item.get("ocr_text") or "").strip()
        code = _doubao_atom_code(page_index, i)
        role = item.get("atom_type") or "text"
        out.append(
            {
                "atom_code": code,
                "page_index": int(page_index),
                "atom_type": role,
                "role": role,
                "content": text,
                "ocr_text": text,
                "bbox": {
                    "x_start": float(item.get("x_start", 0)),
                    "y_start": float(item.get("y_start", 0)),
                    "x_end": float(item.get("x_end", 1)),
                    "y_end": float(item.get("y_end", 1)),
                },
                "data_source": "doubao",
            }
        )
    return out


def _page_jobs_from_ctx(pages: list[Any]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for page in pages or []:
        if hasattr(page, "page_index"):
            jobs.append(
                {
                    "page_index": int(page.page_index),
                    "blob_id": getattr(page, "blob_id", None),
                }
            )
        elif isinstance(page, dict):
            jobs.append(
                {
                    "page_index": int(page.get("page_index") or 0),
                    "blob_id": page.get("blob_id"),
                }
            )
    return [j for j in jobs if j["page_index"] > 0]


def ocr_old_textbook_pages_to_cache(
    *,
    lesson_uid: str,
    pages: list[Any],
    force_refresh: bool = False,
) -> tuple[int, int, list[str]]:
    """旧教材逐页豆包 OCR → page_text 缓存（不写旧库）。返回 (done, total, warnings)。"""
    jobs = _page_jobs_from_ctx(pages)
    if not jobs:
        return 0, 0, ["旧教材无页图"]

    if not dual_track_textbook_uses_doubao():
        return 0, len(jobs), ["旧教材豆包 OCR 未启用"]

    warnings: list[str] = []
    done = 0
    for job in jobs:
        pi = int(job["page_index"])
        blob_id = job.get("blob_id")
        if not blob_id:
            warnings.append(f"旧教材 P{pi}：无 blob，跳过")
            continue
        raw_atoms, meta = extract_page_text_regions(
            lesson_uid=lesson_uid,
            page_index=pi,
            blob_id=blob_id,
            force_refresh=force_refresh,
        )
        if meta.get("warning"):
            warnings.append(f"旧教材 P{pi}：{meta['warning']}")
        if raw_atoms:
            done += 1
        else:
            warnings.append(f"旧教材 P{pi}：豆包未返回文字区域")
        logger.info(
            "old tb doubao cache %s P%s: %d regions (%s)",
            lesson_uid,
            pi,
            len(raw_atoms),
            meta.get("source"),
        )
    return done, len(jobs), warnings


def doubao_atoms_for_page(lesson_uid: str, page_index: int) -> list[dict[str, Any]]:
    """从缓存读取单页豆包原子（无缓存则 []）。"""
    cached = load_page_text_cache(lesson_uid, int(page_index))
    if not cached or not isinstance(cached.get("regions"), list):
        return []
    raw = regions_to_raw_atoms(
        cached["regions"],
        page_index=int(page_index),
    )
    return _raw_to_doubao_atoms(raw, page_index=int(page_index))


def build_old_textbook_doubao_payload(
    *,
    lesson_uid: str,
    pages: list[Any],
) -> dict[str, Any]:
    """组装 workspace / prep 用的旧教材豆包快照。"""
    jobs = _page_jobs_from_ctx(pages)
    page_rows: list[dict[str, Any]] = []
    flat_atoms: list[dict[str, Any]] = []
    for job in jobs:
        pi = int(job["page_index"])
        atoms = doubao_atoms_for_page(lesson_uid, pi)
        char_count = sum(len((a.get("content") or "")) for a in atoms)
        page_rows.append(
            {
                "source": "旧教材·豆包",
                "page_index": pi,
                "data_source": "doubao" if atoms else "—",
                "text_atom_count": len(atoms),
                "char_count": char_count,
                "has_text": bool(atoms),
                "low_coverage": not atoms,
                "atoms": atoms,
                "excerpt": " ".join(
                    (a.get("content") or "").replace("\n", " ")[:80] for a in atoms[:2]
                ),
            }
        )
        flat_atoms.extend(atoms)
    return {
        "lesson_uid": lesson_uid,
        "engine": "doubao_page_text_cache",
        "pages": page_rows,
        "atoms": flat_atoms,
        "pages_with_text": sum(1 for r in page_rows if r.get("has_text")),
        "pages_total": len(page_rows),
        "atom_count": len(flat_atoms),
    }


def doubao_text_for_pages(
    lesson_uid: str,
    page_indices: list[int],
) -> str:
    """合并若干页的豆包 OCR 正文（供教材轨 query）。"""
    parts: list[str] = []
    for pi in sorted({int(x) for x in page_indices if x}):
        for atom in doubao_atoms_for_page(lesson_uid, pi):
            text = (atom.get("content") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def page_indices_for_block(block: Any) -> list[int]:
    start = getattr(block, "textbook_page_start", None)
    end = getattr(block, "textbook_page_end", None)
    if start is not None:
        e = int(end if end is not None else start)
        return list(range(int(start), e + 1))
    indices: set[int] = set()
    for code in block.atom_codes or []:
        m = re.match(r"^[A-Z](\d{3})-", str(code))
        if m:
            indices.add(int(m.group(1)))
    return sorted(indices)
