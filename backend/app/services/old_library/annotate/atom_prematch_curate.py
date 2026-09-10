"""课件引导的原子预匹配整理：建块前按 slide OCR 拆/合/标引用（不写区块）。"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from ....extensions import db
from ....models import CoursewareSlide, LessonPage, TextbookAtom
from ...lesson_lookup import get_lesson_by_uid
from .ai_seed_blocks import _text_content_overlap_score
from .atoms import save_page_atoms, split_mixed_dialogue_scene_spans
from .workspace import _is_placeholder_atom

logger = logging.getLogger(__name__)

PREMATCH_AT_KEY = "__atom_prematch_at"
PREMATCH_REPORT_KEY = "__atom_prematch_report"

SLIDE_HIT_MIN = 0.18
LINE_HIT_MIN = 0.20
IMAGE_REF_MIN = 0.12
IMAGE_PRIMARY_MARGIN = 0.06
_QUIZ_SLIDE_KEYWORDS = ("选择题", "一起来做练习", "判断题", "多选题", "单选题", "练习吧")


def _is_quiz_slide_text(text: str) -> bool:
    t = text or ""
    if any(k in t for k in _QUIZ_SLIDE_KEYWORDS):
        return True
    opts = len(re.findall(r"[A-D][.．、]", t))
    if opts >= 3 and ("正确" in t or "变化" in t):
        return True
    return False


def _plain_hanzi(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _atom_dict(atom: TextbookAtom) -> dict[str, Any]:
    return {
        "atom_code": atom.atom_code,
        "page_index": atom.page_index,
        "atom_type": atom.atom_type,
        "content": atom.content or "",
        "ocr_text": atom.ocr_text or "",
        "bbox": dict(atom.bbox_json or {}),
    }


def _slides_by_index(lesson_id: str) -> dict[int, str]:
    rows = (
        CoursewareSlide.query.filter_by(lesson_id=lesson_id)
        .order_by(CoursewareSlide.slide_index)
        .all()
    )
    out: dict[int, str] = {}
    for row in rows:
        text = (row.ocr_text or "").strip()
        if text:
            out[int(row.slide_index)] = text
    return out


def _meaningful_lines(atom: dict[str, Any]) -> list[str]:
    text = str(atom.get("content") or atom.get("ocr_text") or "").strip()
    if not text or text.startswith("["):
        return []
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and re.search(r"[\u4e00-\u9fff]", ln) and not ln.startswith("[")
    ]
    if len(lines) >= 2:
        return lines
    plain = _plain_hanzi(text)
    if not plain:
        return []
    chunks = re.split(r"(?<=[？?。；;])", text)
    chunks = [
        c.strip()
        for c in chunks
        if c.strip() and len(re.findall(r"[\u4e00-\u9fff]", c)) >= 4
    ]
    return chunks if len(chunks) >= 2 else ([text] if text else [])


def _bbox_parts(atom: dict[str, Any], lines: list[str]) -> list[dict[str, Any]]:
    bbox = atom.get("bbox") or {}
    y0 = float(bbox.get("y_start", 0))
    y1 = float(bbox.get("y_end", 1))
    x0 = float(bbox.get("x_start", 0))
    x1 = float(bbox.get("x_end", 1))
    h = max(y1 - y0, 0.04)
    n = len(lines)
    parts: list[dict[str, Any]] = []
    cursor = y0
    weights = [max(len(_plain_hanzi(ln)), 4) for ln in lines]
    total_w = sum(weights) or n
    for i, ln in enumerate(lines):
        part_h = h * (weights[i] / total_w)
        y_end = y1 if i == n - 1 else round(cursor + part_h, 4)
        parts.append(
            {
                "atom_type": atom.get("atom_type") or "text",
                "content": ln[:500],
                "ocr_text": ln[:500],
                "bbox": {
                    "x_start": round(x0, 4),
                    "y_start": round(cursor, 4),
                    "x_end": round(x1, 4),
                    "y_end": round(y_end, 4),
                },
            }
        )
        cursor = y_end
    return parts


def _line_slide_affinity(
    line: str,
    slides_by_index: dict[int, str],
) -> tuple[int, float]:
    best_si = 0
    best_score = 0.0
    for si, slide_text in slides_by_index.items():
        score = _text_content_overlap_score(slide_text, line)
        if score > best_score:
            best_score = score
            best_si = si
    return best_si, best_score


def suggest_courseware_atom_splits(
    atoms: list[dict[str, Any]],
    slides_by_index: dict[int, str],
) -> list[dict[str, Any]]:
    """多行/多句 text 原子：若各行最佳 slide 不同且均有命中 → 建议拆分。"""
    if not slides_by_index:
        return []
    specs: list[dict[str, Any]] = []
    for atom in atoms:
        if (atom.get("atom_type") or "text").lower() != "text":
            continue
        lines = _meaningful_lines(atom)
        if len(lines) < 2:
            continue
        hits: list[tuple[str, int, float]] = []
        for ln in lines:
            si, score = _line_slide_affinity(ln, slides_by_index)
            if score >= LINE_HIT_MIN:
                hits.append((ln, si, score))
        if len(hits) < 2:
            continue
        slide_set = {si for _, si, _ in hits}
        if len(slide_set) < 2:
            continue
        parts = _bbox_parts(atom, lines)
        if len(parts) < 2:
            continue
        specs.append(
            {
                "source_code": atom["atom_code"],
                "parts": parts,
                "reason": "课件预匹配：各行对应不同 slide",
                "line_hits": [
                    {"line": ln, "slide_index": si, "score": round(sc, 3)}
                    for ln, si, sc in hits
                ],
            }
        )
    return specs


def _slide_image_match_score(slide_text: str, label: str) -> float:
    score = _text_content_overlap_score(slide_text, label)
    if score >= IMAGE_REF_MIN:
        return score
    plain_label = _plain_hanzi(label)
    plain_slide = _plain_hanzi(slide_text)
    if not plain_label or not plain_slide:
        return score
    for n in range(min(8, len(plain_label)), 1, -1):
        for i in range(len(plain_label) - n + 1):
            sub = plain_label[i : i + n]
            if sub in plain_slide:
                return max(IMAGE_REF_MIN, n / max(len(plain_label), 1))
    return score


def _image_label_for_match(atom: dict[str, Any]) -> str:
    label = str(atom.get("content") or atom.get("ocr_text") or "").strip()
    if label.startswith("[插图]"):
        label = label[4:].strip()
    if label.startswith("[") and "]" in label:
        label = label.split("]", 1)[-1].strip()
    return label


def suggest_image_slide_refs(
    atoms: list[dict[str, Any]],
    slides_by_index: dict[int, str],
) -> list[dict[str, Any]]:
    """插图原子：标注 primary_slide + referenced_slides（一图多课件引用）。"""
    if not slides_by_index:
        return []
    refs: list[dict[str, Any]] = []
    for atom in atoms:
        if (atom.get("atom_type") or "text").lower() != "image":
            continue
        label = _image_label_for_match(atom)
        if not label:
            continue
        scores: list[tuple[int, float]] = []
        for si, slide_text in slides_by_index.items():
            score = _slide_image_match_score(slide_text, label)
            if score >= IMAGE_REF_MIN:
                scores.append((si, score))
        if not scores:
            continue
        scores.sort(key=lambda x: x[1], reverse=True)
        non_quiz = [
            (si, sc)
            for si, sc in scores
            if not _is_quiz_slide_text(slides_by_index.get(si) or "")
        ]
        pick_from = non_quiz if non_quiz else scores
        primary_si, primary_score = pick_from[0]
        ref_slides = [primary_si]
        for si, sc in pick_from[1:]:
            if sc >= max(0.28, primary_score - 0.06):
                ref_slides.append(si)
        ref_slides = sorted(set(ref_slides))[:4]
        secondary = [si for si in ref_slides if si != primary_si]
        if primary_score < IMAGE_REF_MIN and len(ref_slides) < 2:
            continue
        refs.append(
            {
                "atom_code": atom["atom_code"],
                "primary_slide": primary_si,
                "referenced_slides": sorted(set(ref_slides)),
                "secondary_slides": sorted(secondary),
                "slide_scores": {str(si): round(sc, 3) for si, sc in scores[:8]},
                "reason": "课件预匹配：一图多 slide 引用"
                if len(ref_slides) >= 2
                else "课件预匹配：强命中 slide",
            }
        )
    return refs


def suggest_image_row_groups(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同页同行并列插图：建议同一 image_group（建块时整组绑定）。"""
    images = [
        a
        for a in atoms
        if (a.get("atom_type") or "text").lower() == "image"
        and (a.get("content") or a.get("ocr_text") or "").strip()
    ]
    if len(images) < 2:
        return []
    by_page: dict[int, list[dict[str, Any]]] = {}
    for atom in images:
        by_page.setdefault(int(atom.get("page_index") or 0), []).append(atom)

    groups: list[dict[str, Any]] = []
    for page_index, page_atoms in by_page.items():
        page_atoms.sort(key=lambda a: float((a.get("bbox") or {}).get("x_start", 0)))
        row: list[dict[str, Any]] = []
        for atom in page_atoms:
            b = atom.get("bbox") or {}
            y_mid = (float(b.get("y_start", 0)) + float(b.get("y_end", 0))) / 2
            if not row:
                row = [atom]
                continue
            prev = row[-1]
            pb = prev.get("bbox") or {}
            py_mid = (float(pb.get("y_start", 0)) + float(pb.get("y_end", 0))) / 2
            if abs(y_mid - py_mid) <= 0.06:
                row.append(atom)
            else:
                if len(row) >= 2:
                    groups.append(
                        {
                            "page_index": page_index,
                            "atom_codes": [a["atom_code"] for a in row],
                            "reason": "同页并列插图组",
                        }
                    )
                row = [atom]
        if len(row) >= 2:
            groups.append(
                {
                    "page_index": page_index,
                    "atom_codes": [a["atom_code"] for a in row],
                    "reason": "同页并列插图组",
                }
            )
    return groups


def suggest_atom_prematch_plan(
    *,
    lesson_uid: str,
    book_type: str = "old",
) -> dict[str, Any]:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    slides_by_index = _slides_by_index(les.id)
    if not slides_by_index:
        raise ValueError("本课尚无课件 OCR，请先完成课件文字 OCR")

    atoms = [
        _atom_dict(a)
        for a in TextbookAtom.query.filter_by(lesson_id=les.id).order_by(
            TextbookAtom.page_index, TextbookAtom.atom_code
        )
        if not _is_placeholder_atom(a)
    ]
    splits = suggest_courseware_atom_splits(atoms, slides_by_index)
    image_refs = suggest_image_slide_refs(atoms, slides_by_index)
    image_groups = suggest_image_row_groups(atoms)
    needs_review = [
        s["source_code"]
        for s in splits
        if any(h.get("score", 0) < 0.28 for h in s.get("line_hits") or [])
    ]
    return {
        "ok": True,
        "lesson_uid": lesson_uid,
        "slide_count": len(slides_by_index),
        "atom_count": len(atoms),
        "splits": splits,
        "image_refs": image_refs,
        "image_groups": image_groups,
        "needs_review": needs_review,
        "warnings": [],
    }


def _merge_prematch_metadata(
    existing: dict[str, Any] | None,
    patch: dict[str, Any],
) -> dict[str, Any]:
    meta = dict(existing or {})
    prematch = dict(meta.get("prematch") or {})
    prematch.update(patch)
    meta["prematch"] = prematch
    return meta


def apply_atom_prematch_plan(
    *,
    lesson_uid: str,
    plan: dict[str, Any] | None = None,
    renumber: bool = True,
    book_type: str = "old",
) -> dict[str, Any]:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    plan = plan or suggest_atom_prematch_plan(lesson_uid=lesson_uid, book_type=book_type)
    warnings: list[str] = list(plan.get("warnings") or [])
    split_out: list[dict[str, Any]] = []
    metadata_updates = 0
    pages_to_renumber: set[int] = set()

    split_specs = [
        {"source_code": s["source_code"], "parts": s["parts"]}
        for s in plan.get("splits") or []
    ]
    source_pages: dict[str, int] = {
        str(a.atom_code): int(a.page_index)
        for a in TextbookAtom.query.filter_by(lesson_id=les.id).all()
    }
    if split_specs:
        try:
            out = split_mixed_dialogue_scene_spans(
                lesson_uid=lesson_uid,
                split_specs=split_specs,
                book_type=book_type,
            )
            split_out = list(out.get("split") or [])
            for spec in plan.get("splits") or []:
                src_code = str(spec.get("source_code") or "")
                pages_to_renumber.add(source_pages.get(src_code, 0))
                created = next(
                    (x.get("created") for x in split_out if x.get("source") == src_code),
                    [],
                )
                hits = spec.get("line_hits") or []
                for code, hit in zip(created, hits):
                    row = TextbookAtom.query.filter_by(
                        lesson_id=les.id, atom_code=code
                    ).first()
                    if not row:
                        continue
                    row.metadata_json = _merge_prematch_metadata(
                        row.metadata_json,
                        {
                            "primary_slide": hit.get("slide_index"),
                            "referenced_slides": [hit.get("slide_index")],
                            "source": "courseware_split",
                            "line_hit_score": hit.get("score"),
                        },
                    )
                    metadata_updates += 1
            warnings.append(f"课件预匹配：已拆分 {len(split_out)} 个粘连 text 原子")
        except ValueError as exc:
            warnings.append(f"拆分失败：{exc}")

    group_id = 0
    for grp in plan.get("image_groups") or []:
        group_id += 1
        gid = f"row-{grp.get('page_index')}-{group_id}"
        for code in grp.get("atom_codes") or []:
            row = TextbookAtom.query.filter_by(lesson_id=les.id, atom_code=code).first()
            if not row:
                continue
            row.metadata_json = _merge_prematch_metadata(
                row.metadata_json,
                {"image_group_id": gid, "source": "courseware_row_group"},
            )
            metadata_updates += 1

    for ref in plan.get("image_refs") or []:
        code = ref.get("atom_code")
        row = TextbookAtom.query.filter_by(lesson_id=les.id, atom_code=code).first()
        if not row:
            continue
        row.metadata_json = _merge_prematch_metadata(
            row.metadata_json,
            {
                "primary_slide": ref.get("primary_slide"),
                "referenced_slides": ref.get("referenced_slides") or [],
                "secondary_slides": ref.get("secondary_slides") or [],
                "slide_scores": ref.get("slide_scores") or {},
                "source": "courseware_image_ref",
            },
        )
        metadata_updates += 1

    renumber_maps: dict[int, dict[str, str]] = {}
    if renumber:
        for page_index in sorted(p for p in pages_to_renumber if p > 0):
            try:
                out = save_page_atoms(
                    lesson_uid=lesson_uid,
                    page_index=page_index,
                    book_type=book_type,
                )
                renumber_maps[page_index] = dict(out.get("mapping") or {})
            except ValueError as exc:
                warnings.append(f"第 {page_index} 页重编号失败：{exc}")

    lp = (
        LessonPage.query.filter_by(lesson_id=les.id)
        .order_by(LessonPage.page_index)
        .first()
    )
    if lp:
        lineage = dict(lp.atom_lineage_json or {})
        lineage[PREMATCH_AT_KEY] = datetime.utcnow().isoformat(timespec="seconds")
        lineage[PREMATCH_REPORT_KEY] = {
            "split_count": len(split_out),
            "image_ref_count": len(plan.get("image_refs") or []),
            "image_group_count": len(plan.get("image_groups") or []),
            "needs_review": plan.get("needs_review") or [],
        }
        lp.atom_lineage_json = lineage

    db.session.commit()
    return {
        "ok": True,
        "lesson_uid": lesson_uid,
        "split": split_out,
        "image_refs": plan.get("image_refs") or [],
        "image_groups": plan.get("image_groups") or [],
        "metadata_updates": metadata_updates,
        "renumber_maps": renumber_maps,
        "needs_review": plan.get("needs_review") or [],
        "warnings": warnings,
    }


def lesson_prematch_done(*, lesson_id: str) -> bool:
    lp = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .first()
    )
    if not lp:
        return False
    lineage = lp.atom_lineage_json or {}
    return bool(lineage.get(PREMATCH_AT_KEY))


def build_prematch_review(
    *,
    lesson_uid: str,
    book_type: str = "old",
) -> dict[str, Any]:
    """供 annotate 检视：已执行的拆分/引用/组图 + 当前建议。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    done = lesson_prematch_done(lesson_id=les.id)
    lp = (
        LessonPage.query.filter_by(lesson_id=les.id)
        .order_by(LessonPage.page_index)
        .first()
    )
    report = dict((lp.atom_lineage_json or {}).get(PREMATCH_REPORT_KEY) or {}) if lp else {}

    splits: list[dict[str, Any]] = []
    image_refs: list[dict[str, Any]] = []
    image_groups: dict[str, list[str]] = {}
    for atom in TextbookAtom.query.filter_by(lesson_id=les.id).order_by(
        TextbookAtom.page_index, TextbookAtom.atom_code
    ):
        if _is_placeholder_atom(atom):
            continue
        pm = (atom.metadata_json or {}).get("prematch") or {}
        if pm.get("source") == "courseware_split":
            splits.append(
                {
                    "atom_code": atom.atom_code,
                    "page_index": atom.page_index,
                    "content": (atom.content or "")[:120],
                    "primary_slide": pm.get("primary_slide"),
                    "score": pm.get("line_hit_score"),
                }
            )
        elif pm.get("source") == "courseware_image_ref":
            image_refs.append(
                {
                    "atom_code": atom.atom_code,
                    "content": (atom.content or "")[:80],
                    "primary_slide": pm.get("primary_slide"),
                    "referenced_slides": pm.get("referenced_slides") or [],
                }
            )
        gid = pm.get("image_group_id")
        if gid:
            image_groups.setdefault(str(gid), []).append(atom.atom_code)

    pending: dict[str, Any] = {}
    if not done:
        try:
            pending = suggest_atom_prematch_plan(lesson_uid=lesson_uid, book_type=book_type)
        except ValueError as exc:
            pending = {"error": str(exc)}

    return {
        "done": done,
        "report": report,
        "splits": splits,
        "image_refs": image_refs[:24],
        "image_groups": [
            {"group_id": gid, "atom_codes": codes}
            for gid, codes in sorted(image_groups.items())
        ],
        "needs_review": report.get("needs_review") or [],
        "pending_plan": pending if not done else None,
    }


def atom_prematch_lesson(
    *,
    lesson_uid: str,
    book_type: str = "old",
    renumber: bool = True,
) -> dict[str, Any]:
    plan = suggest_atom_prematch_plan(lesson_uid=lesson_uid, book_type=book_type)
    apply_out = apply_atom_prematch_plan(
        lesson_uid=lesson_uid,
        plan=plan,
        renumber=renumber,
        book_type=book_type,
    )
    return {**plan, **apply_out}
