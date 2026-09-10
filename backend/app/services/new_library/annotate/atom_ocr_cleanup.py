"""OCR 原子文本去噪（重复片段、括号堆叠等）。"""
from __future__ import annotations

import re


def normalize_ocr_atom_text(text: str) -> str:
    """压缩重复子串与常见 OCR 乱序噪声。"""
    s = (text or "").strip()
    if not s or s.startswith("["):
        return s

    changed = True
    while changed and len(s) > 8:
        changed = False
        max_chunk = min(48, len(s) // 2)
        for size in range(max_chunk, 3, -1):
            idx = 0
            while idx + 2 * size <= len(s):
                if s[idx : idx + size] == s[idx + size : idx + 2 * size]:
                    s = s[: idx + size] + s[idx + 2 * size :]
                    changed = True
                    break
                idx += 1
            if changed:
                break

    s = re.sub(r"([）)]\s*){2,}", "）", s)
    s = re.sub(r"([（(]\s*){2,}", "（", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


_BLANK_IMAGE_TEXTS = frozenset({"[插图]", "[整页图像]", "[图片]"})
_SENTENCE_END = frozenset("。！？；?!;")


def is_low_value_image_atom(atom) -> bool:
    """空白/占位插图：不应参与建块锚定。"""
    if (getattr(atom, "atom_type", "") or "").strip().lower() != "image":
        return False
    text = ((getattr(atom, "content", None) or getattr(atom, "ocr_text", None)) or "").strip()
    if text in _BLANK_IMAGE_TEXTS:
        return True
    bbox = getattr(atom, "bbox_json", None) or {}
    w = max(0.0, float(bbox.get("x_end", 1)) - float(bbox.get("x_start", 0)))
    h = max(0.0, float(bbox.get("y_end", 1)) - float(bbox.get("y_start", 0)))
    return w * h < 0.002 and (not text or text.startswith("["))


def merge_obvious_paragraph_fragments(
    *,
    lesson_uid: str,
    book_type: str = "new",
) -> dict:
    """同页相邻 OCR 句段合并（AI 整理失败时的兜底，避免一句话拆多个原子）。"""
    from ....models import TextbookAtom
    from ...lesson_lookup import get_lesson_by_uid
    from ...old_library.annotate.atoms import merge_atoms
    from ...old_library.annotate.workspace import _is_placeholder_atom

    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    total_merged = 0
    for _ in range(6):
        round_merged = _merge_paragraph_pass(les, lesson_uid=lesson_uid, book_type=book_type)
        total_merged += round_merged
        if round_merged == 0:
            break
    return {"merged_groups": total_merged}


def _merge_paragraph_pass(les, *, lesson_uid: str, book_type: str) -> int:
    from ....models import TextbookAtom
    from ...old_library.annotate.atoms import merge_atoms
    from ...old_library.annotate.workspace import _is_placeholder_atom

    merged_count = 0
    by_page: dict[int, list] = {}
    for atom in TextbookAtom.query.filter_by(lesson_id=les.id).order_by(
        TextbookAtom.page_index, TextbookAtom.atom_code
    ):
        if _is_placeholder_atom(atom):
            continue
        if (atom.atom_type or "").strip().lower() != "text":
            continue
        text = (atom.content or atom.ocr_text or "").strip()
        if not text or text.startswith("["):
            continue
        by_page.setdefault(int(atom.page_index), []).append(atom)

    for page_atoms in by_page.values():
        page_atoms.sort(
            key=lambda a: (
                float((a.bbox_json or {}).get("y_start", 0)),
                float((a.bbox_json or {}).get("x_start", 0)),
            )
        )
        i = 0
        while i < len(page_atoms) - 1:
            left = page_atoms[i]
            right = page_atoms[i + 1]
            lt = (left.content or left.ocr_text or "").strip()
            rt = (right.content or right.ocr_text or "").strip()
            lb = left.bbox_json or {}
            rb = right.bbox_json or {}
            y_gap = float(rb.get("y_start", 0)) - float(lb.get("y_end", 0))
            if y_gap > 0.06:
                i += 1
                continue
            if lt and lt[-1] in _SENTENCE_END:
                i += 1
                continue
            if rt.startswith(("问题情境", "科学探究", "拓展", "反思")) and not lt.endswith("情境"):
                i += 1
                continue
            if any(m in rt for m in ("还有哪些", "还有什么", "其他物体")):
                i += 1
                continue
            if any(m in lt for m in ("消融", "融化", "雪会", "冰雪")) and "还有" in rt:
                i += 1
                continue
            try:
                merge_atoms(
                    lesson_uid=lesson_uid,
                    atom_codes=[left.atom_code, right.atom_code],
                    book_type=book_type,
                )
                merged_count += 1
                page_atoms[i] = TextbookAtom.query.filter_by(
                    lesson_id=les.id, atom_code=left.atom_code
                ).first() or left
                page_atoms.pop(i + 1)
            except ValueError:
                i += 1
    return merged_count


def cleanup_lesson_atom_ocr_text(*, lesson_id: str) -> dict:
    """写回课内可见原子的 content/ocr_text 去噪结果。"""
    from ....models import TextbookAtom
    from ...old_library.annotate.workspace import _is_placeholder_atom

    updated = 0
    atoms = TextbookAtom.query.filter_by(lesson_id=lesson_id).all()
    for atom in atoms:
        if _is_placeholder_atom(atom):
            continue
        raw = (atom.content or atom.ocr_text or "").strip()
        if not raw or raw.startswith("["):
            continue
        cleaned = normalize_ocr_atom_text(raw)
        if cleaned and cleaned != raw:
            atom.content = cleaned
            if atom.ocr_text:
                atom.ocr_text = cleaned
            updated += 1
    return {"updated_atoms": updated}
