"""教材原子 AI 整理：建议与应用（写库）。"""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any

from ....extensions import db
from ....models import FileBlob, LessonPage, TextbookAtom
from ....services.blobs import read_blob_bytes
from ....services.llm.atom_curate_metadata import (
    apply_metadata_to_page_atoms,
    enrich_curate_atom_roles,
    metadata_map_from_roles,
    record_merge_metadata,
    remap_metadata_map,
)
from ....services.llm.atom_curate_heuristics import (
    build_minimal_heuristic_supplement,
    collect_curate_split_specs,
    merge_curate_plans_llm_first,
    sanitize_atom_curate_plan,
    sanitize_llm_curate_plan,
)
from ....services.llm.atom_curate_suggest import (
    suggest_atom_curate_plan_with_llm,
    validate_atom_curate_plan,
)
from ...lesson_lookup import get_lesson_by_uid
from .atoms import (
    delete_atoms,
    extract_atoms_for_page,
    merge_atoms,
    save_page_atoms,
    split_mixed_dialogue_scene_spans,
)
from .workspace import _is_placeholder_atom

logger = logging.getLogger(__name__)

AI_CURATED_AT_KEY = "__ai_curated_at"


def mark_page_ai_curated(*, lesson_id: str, page_index: int) -> None:
    """成功执行 AI 整理后写入页级标记（供对照分析进度判定）。"""
    lp = LessonPage.query.filter_by(lesson_id=lesson_id, page_index=int(page_index)).first()
    if not lp:
        return
    lineage = dict(lp.atom_lineage_json or {})
    lineage[AI_CURATED_AT_KEY] = datetime.utcnow().isoformat(timespec="seconds")
    lp.atom_lineage_json = lineage
    db.session.flush()


def page_ai_curated(
    *,
    lesson_id: str,
    page_index: int,
    lp: LessonPage | None = None,
) -> bool:
    """本页是否已完成 AI 整理（页级标记或原子 curate 元数据）。"""
    row = lp or LessonPage.query.filter_by(
        lesson_id=lesson_id, page_index=int(page_index)
    ).first()
    if not row:
        return False
    lineage = row.atom_lineage_json or {}
    if lineage.get(AI_CURATED_AT_KEY):
        return True
    atoms = (
        TextbookAtom.query.filter_by(lesson_id=lesson_id, page_index=int(page_index))
        .order_by(TextbookAtom.atom_code)
        .all()
    )
    visible = [a for a in atoms if not _is_placeholder_atom(a)]
    if not visible:
        return False
    with_role = sum(
        1
        for a in visible
        if (a.metadata_json or {}).get("curate", {}).get("role")
    )
    return with_role >= max(1, int(len(visible) * 0.5))


def _lesson_page_image_data_url(lesson_id: str, page_index: int) -> str:
    lp = LessonPage.query.filter_by(lesson_id=lesson_id, page_index=page_index).first()
    if not lp or not lp.blob_id:
        raise ValueError(f"第 {page_index} 页教材图不存在")
    blob = FileBlob.query.get(lp.blob_id)
    if not blob:
        raise ValueError("教材页 blob 不存在")
    data = read_blob_bytes(blob)
    mime = (blob.mime_type or "image/png").lower()
    if "jpeg" in mime or "jpg" in mime:
        prefix = "data:image/jpeg;base64,"
    else:
        prefix = "data:image/png;base64,"
    b64 = base64.standard_b64encode(data).decode("ascii")
    return prefix + b64


def _atoms_for_curate(lesson_id: str, page_index: int) -> list[dict[str, Any]]:
    rows = (
        TextbookAtom.query.filter_by(lesson_id=lesson_id, page_index=page_index)
        .order_by(TextbookAtom.atom_code)
        .all()
    )
    out: list[dict[str, Any]] = []
    for a in rows:
        if _is_placeholder_atom(a):
            continue
        out.append(
            {
                "atom_code": a.atom_code,
                "atom_type": a.atom_type,
                "bbox": dict(a.bbox_json or {}),
                "content": a.content or "",
                "ocr_text": a.ocr_text or "",
            }
        )
    return out


def assess_page_curate_readiness(
    *,
    lesson_uid: str,
    page_index: int,
    book_type: str = "old",
) -> dict[str, Any]:
    """判断单页整理是否可跳过 OCR。"""
    from ....models import LessonPage, TextbookAtom
    from ...lesson_lookup import get_lesson_by_uid

    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    page_index = int(page_index)
    lp = LessonPage.query.filter_by(
        lesson_id=les.id, page_index=page_index
    ).first()
    atom_count = (
        TextbookAtom.query.filter_by(lesson_id=les.id, page_index=page_index).count()
    )
    visible = 0
    if atom_count:
        for a in TextbookAtom.query.filter_by(
            lesson_id=les.id, page_index=page_index
        ):
            t = (a.content or a.ocr_text or "").strip()
            if t.startswith("[未拆分") or t.startswith("[整页未识别") or t == "[整页图像]":
                continue
            code = a.atom_code or ""
            if "-GAP-" in code or code.endswith("-FULL-001"):
                continue
            visible += 1

    has_ocr = bool(lp and lp.ocr_atoms_json)
    if has_ocr and visible > 0:
        return {
            "page_index": page_index,
            "has_ocr_baseline": True,
            "atom_count": visible,
            "recommended_extract_first": False,
            "summary": "本页已完成 OCR，将仅执行 AI 整理",
        }
    return {
        "page_index": page_index,
        "has_ocr_baseline": has_ocr,
        "atom_count": visible,
        "recommended_extract_first": True,
        "summary": "将 OCR 本页并由 AI 整理原子",
    }


def suggest_ai_atom_curation(
    *,
    lesson_uid: str,
    page_index: int,
    book_type: str = "old",
) -> dict[str, Any]:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    page_index = int(page_index)
    atoms = _atoms_for_curate(les.id, page_index)
    if not atoms:
        raise ValueError(f"第 {page_index} 页没有可整理的原子，请先 OCR 本页")

    pdf_page = None
    if les.page_start:
        pdf_page = int(les.page_start) + page_index - 1

    image_url = _lesson_page_image_data_url(les.id, page_index)
    llm_out = suggest_atom_curate_plan_with_llm(
        lesson_name=les.lesson_name or "",
        unit_title=les.unit_title or "",
        page_index=page_index,
        pdf_page=pdf_page,
        atoms=atoms,
        image_url=image_url,
    )
    heuristic = build_minimal_heuristic_supplement(atoms)
    merged_raw = merge_curate_plans_llm_first(
        {
            "atom_roles": llm_out.get("atom_roles") or [],
            "merge_groups": llm_out.get("merge_groups") or [],
            "delete_codes": llm_out.get("delete_codes") or [],
            "warnings": llm_out.get("warnings") or [],
        },
        heuristic,
    )
    known = {a["atom_code"] for a in atoms}
    plan, val_warns = validate_atom_curate_plan(plan=merged_raw, known_codes=known)
    plan = sanitize_llm_curate_plan(
        plan, atoms, atom_roles=plan.get("atom_roles") or llm_out.get("atom_roles")
    )
    plan["atom_roles"] = enrich_curate_atom_roles(
        plan.get("atom_roles") or [], atoms
    )
    warnings = list(plan.get("warnings") or []) + val_warns
    plan["source"] = "llm_vision"
    return {
        "ok": True,
        "lesson_uid": lesson_uid,
        "page_index": page_index,
        "atom_count": len(atoms),
        "plan": plan,
        "warnings": warnings,
        "model": llm_out.get("model"),
        "source": llm_out.get("source"),
        "generated_at": llm_out.get("generated_at"),
    }


def apply_atom_curate_plan(
    *,
    lesson_uid: str,
    page_index: int,
    plan: dict[str, Any],
    renumber: bool = True,
    book_type: str = "old",
) -> dict[str, Any]:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    page_index = int(page_index)
    atoms = _atoms_for_curate(les.id, page_index)
    known = {a["atom_code"] for a in atoms}
    normalized, _ = validate_atom_curate_plan(plan=plan, known_codes=known)
    if plan.get("source") == "llm_vision" or normalized.get("source") == "llm_vision":
        normalized = sanitize_llm_curate_plan(
            normalized,
            atoms,
            atom_roles=normalized.get("atom_roles") or plan.get("atom_roles"),
        )
    elif normalized.get("atom_roles") or plan.get("atom_roles"):
        normalized = sanitize_llm_curate_plan(
            normalized, atoms, atom_roles=normalized.get("atom_roles") or plan.get("atom_roles")
        )
    else:
        normalized = sanitize_atom_curate_plan(normalized, atoms)

    warnings: list[str] = list(normalized.get("warnings") or [])
    deleted: list[str] = []
    merged: list[dict[str, Any]] = []
    split: list[dict[str, Any]] = []

    split_codes = list(normalized.get("split_codes") or [])
    if split_codes:
        split_set = set(split_codes)
        specs = [
            spec
            for spec in collect_curate_split_specs(atoms)
            if spec["source_code"] in split_set
        ]
        if specs:
            try:
                out = split_mixed_dialogue_scene_spans(
                    lesson_uid=lesson_uid,
                    split_specs=specs,
                    book_type=book_type,
                )
                split = list(out.get("split") or [])
                atoms = _atoms_for_curate(les.id, page_index)
                normalized = sanitize_llm_curate_plan(
                    {**normalized, "merge_groups": []},
                    atoms,
                    atom_roles=normalized.get("atom_roles") or plan.get("atom_roles"),
                )
                warnings = list(dict.fromkeys(warnings + list(normalized.get("warnings") or [])))
            except ValueError as exc:
                warnings.append(f"拆分粘连框失败：{exc}")

    delete_codes = list(normalized.get("delete_codes") or [])
    merge_groups = list(normalized.get("merge_groups") or [])
    atom_roles = list(normalized.get("atom_roles") or plan.get("atom_roles") or [])
    meta_by_code = metadata_map_from_roles(atom_roles)

    if delete_codes:
        try:
            out = delete_atoms(
                lesson_uid=lesson_uid,
                atom_codes=delete_codes,
                book_type=book_type,
            )
            deleted = list(out.get("deleted") or [])
            for code in deleted:
                meta_by_code.pop(code, None)
        except ValueError as exc:
            warnings.append(f"删除失败：{exc}")

    for grp in merge_groups:
        codes = list(grp.get("atom_codes") or [])
        if len(codes) < 2:
            continue
        try:
            out = merge_atoms(
                lesson_uid=lesson_uid,
                atom_codes=codes,
                book_type=book_type,
            )
            new_code = str(out.get("atom_code") or "")
            merged.append(
                {
                    "atom_code": new_code,
                    "merged_from": codes,
                    "reason": grp.get("reason") or "",
                }
            )
            if new_code:
                record_merge_metadata(meta_by_code, codes, new_code)
        except ValueError as exc:
            warnings.append(f"合并 {codes} 失败：{exc}")

    renumber_mapping: dict[str, str] = {}
    saved = 0
    if renumber and (_atoms_for_curate(les.id, page_index)):
        try:
            apply_metadata_to_page_atoms(
                lesson_id=les.id,
                page_index=page_index,
                meta_by_code=meta_by_code,
            )
            out = save_page_atoms(
                lesson_uid=lesson_uid,
                page_index=page_index,
                book_type=book_type,
            )
            saved = int(out.get("saved") or 0)
            renumber_mapping = dict(out.get("mapping") or {})
            meta_by_code = remap_metadata_map(meta_by_code, renumber_mapping)
            apply_metadata_to_page_atoms(
                lesson_id=les.id,
                page_index=page_index,
                meta_by_code=meta_by_code,
            )
        except ValueError as exc:
            warnings.append(f"保存本页编号失败：{exc}")
    elif meta_by_code:
        apply_metadata_to_page_atoms(
            lesson_id=les.id,
            page_index=page_index,
            meta_by_code=meta_by_code,
        )

    return {
        "ok": True,
        "page_index": page_index,
        "deleted": deleted,
        "split": split,
        "merged": merged,
        "saved": saved,
        "metadata_applied": len(meta_by_code),
        "warnings": warnings,
    }


def ai_curate_page(
    *,
    lesson_uid: str,
    page_index: int,
    extract_first: bool | None = None,
    replace_on_extract: bool = True,
    book_type: str = "old",
) -> dict[str, Any]:
    """单页：可选 OCR → AI 建议 → 应用 → 重编号。"""
    page_index = int(page_index)
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    readiness = assess_page_curate_readiness(
        lesson_uid=lesson_uid, page_index=page_index, book_type=book_type
    )
    do_extract = (
        readiness["recommended_extract_first"]
        if extract_first is None
        else extract_first
    )
    if do_extract:
        extract_atoms_for_page(
            lesson_uid=lesson_uid,
            page_index=page_index,
            replace_page=replace_on_extract,
            book_type=book_type,
        )

    suggest = suggest_ai_atom_curation(
        lesson_uid=lesson_uid,
        page_index=page_index,
        book_type=book_type,
    )
    apply_out = apply_atom_curate_plan(
        lesson_uid=lesson_uid,
        page_index=page_index,
        plan=suggest["plan"],
        renumber=True,
        book_type=book_type,
    )
    mark_page_ai_curated(lesson_id=les.id, page_index=page_index)
    return {
        "ok": True,
        "page_index": page_index,
        "extracted_first": do_extract,
        "readiness": readiness,
        "plan": suggest["plan"],
        "deleted": apply_out.get("deleted") or [],
        "merged": apply_out.get("merged") or [],
        "saved": apply_out.get("saved") or 0,
        "warnings": (suggest.get("warnings") or []) + (apply_out.get("warnings") or []),
        "model": suggest.get("model"),
    }


def ai_curate_lesson(
    *,
    lesson_uid: str,
    extract_first: bool | None = None,
    book_type: str = "old",
) -> dict[str, Any]:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    pages = (
        LessonPage.query.filter_by(lesson_id=les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    if not pages:
        raise ValueError("本课尚无教材页")

    page_results: list[dict[str, Any]] = []
    all_warnings: list[str] = []
    for lp in pages:
        pi = int(lp.page_index)
        try:
            out = ai_curate_page(
                lesson_uid=lesson_uid,
                page_index=pi,
                extract_first=extract_first,
                book_type=book_type,
            )
            page_results.append(out)
            all_warnings.extend(out.get("warnings") or [])
        except Exception as exc:
            logger.exception("AI curate page %s failed", pi)
            all_warnings.append(f"第 {pi} 页：{exc}")
            page_results.append({"ok": False, "page_index": pi, "error": str(exc)})

    ok_count = sum(1 for r in page_results if r.get("ok"))
    return {
        "ok": ok_count > 0,
        "lesson_uid": lesson_uid,
        "page_count": len(pages),
        "curated_count": ok_count,
        "pages": page_results,
        "warnings": all_warnings,
    }
