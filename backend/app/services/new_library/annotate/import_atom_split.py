"""导入情境双设问拆原子（方案 2：原子 1:1，区块仍合一）。"""
from __future__ import annotations

import re

from ....extensions import db
from ....models import Block, TextbookAtom
from ...lesson_lookup import get_lesson_by_uid
from ...old_library.annotate.atoms import _next_unmerge_atom_code
from ...old_library.annotate.workspace import _is_placeholder_atom

_SECTION_HEADERS = frozenset({"问题情境", "科学探究", "拓展迁移", "反思评价"})
_INTRO_SIGNALS = ("消融", "融化", "冰雪", "雪会", "状态发生", "春天", "物体", "变化")
_EXTEND_MARKERS = ("还有哪些", "还有什么物体", "什么物体会", "其他物体", "其他物质")


def _split_import_text(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    lines = [ln.strip() for ln in re.split(r"[\n\r]+", raw) if ln.strip()]
    parts: list[str] = []
    for ln in lines:
        if ln in _SECTION_HEADERS:
            continue
        marker_hit = next((m for m in _EXTEND_MARKERS if m in ln), None)
        if marker_hit:
            idx = ln.find(marker_hit)
            head = ln[:idx].rstrip("，,；; ")
            tail = ln[idx:].lstrip("，, ")
            if len(head) >= 4:
                parts.append(head)
            if len(tail) >= 4:
                parts.append(tail)
        elif len(ln) >= 4:
            parts.append(ln)
    return parts


def _should_split_dual_import(text: str, parts: list[str]) -> bool:
    """仅拆分「现象导入 + 还有哪些…」式双设问，避免误拆其它合并段。"""
    if len(parts) < 2:
        return False
    combined = re.sub(r"\s+", "", (text or ""))
    if len(combined) < 12:
        return False
    # 短栏目标题（工具材料 / 安全提示等）不拆
    if combined in _SECTION_HEADERS or len(combined) <= 8:
        return False
    has_extend = any(m in combined for m in _EXTEND_MARKERS)
    if not has_extend:
        return False
    head = parts[0]
    if "问题情境" in (text or "") or "问题情境" in head:
        return True
    has_intro = any(sig in combined for sig in _INTRO_SIGNALS)
    ends_question = combined.rstrip().endswith("？") or combined.rstrip().endswith("?")
    return has_intro and (ends_question or "？" in parts[1] or "?" in parts[1])


def _split_bbox(bbox: dict, ratio: float) -> tuple[dict, dict]:
    y0 = float(bbox.get("y_start", 0))
    y1 = float(bbox.get("y_end", 1))
    mid = y0 + (y1 - y0) * max(0.2, min(0.8, ratio))
    top = dict(bbox)
    bot = dict(bbox)
    top["y_end"] = mid
    bot["y_start"] = mid
    return top, bot


def _insert_atom_in_blocks(lesson_id: str, after_code: str, new_code: str) -> None:
    for block in Block.query.filter_by(lesson_id=lesson_id).all():
        codes = list(block.atom_codes or [])
        if after_code not in codes:
            continue
        idx = codes.index(after_code)
        codes.insert(idx + 1, new_code)
        block.atom_codes = codes


def split_dual_import_question_atoms(
    *,
    lesson_uid: str,
    book_type: str = "new",
) -> dict:
    """将合并的导入双设问拆成多个原子，供 1:1 匹配旧块。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    split_count = 0
    atoms = TextbookAtom.query.filter_by(lesson_id=les.id).order_by(
        TextbookAtom.page_index, TextbookAtom.atom_code
    ).all()

    for atom in atoms:
        if _is_placeholder_atom(atom):
            continue
        if (atom.atom_type or "").strip().lower() != "text":
            continue
        text = (atom.content or atom.ocr_text or "").strip()
        parts = _split_import_text(text)
        if not _should_split_dual_import(text, parts):
            continue
        if len(parts) > 2:
            parts = parts[:2]

        p1, p2 = parts
        bbox = dict(atom.bbox_json or {})
        ratio = len(p1) / max(1, len(p1) + len(p2))
        b1, b2 = _split_bbox(bbox, ratio)

        atom.content = p1
        atom.ocr_text = p1
        atom.bbox_json = b1

        new_code = _next_unmerge_atom_code(les.id, int(atom.page_index))
        db.session.add(
            TextbookAtom(
                lesson_id=les.id,
                atom_code=new_code,
                page_index=int(atom.page_index),
                atom_type="text",
                bbox_json=b2,
                content=p2,
                ocr_text=p2,
                metadata_json={"import_split_from": atom.atom_code},
            )
        )
        _insert_atom_in_blocks(les.id, atom.atom_code, new_code)
        split_count += 1

    if split_count:
        db.session.flush()
    return {"split_atoms": split_count}
