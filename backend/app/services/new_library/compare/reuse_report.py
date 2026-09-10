"""由 block_matches 汇总 reuse_reports。"""
from __future__ import annotations

from datetime import datetime

from ....extensions import db
from ....models import Block, BlockMatch, ReuseReport
from ....services.dictionary import list_dictionary_entries
from .resolve import get_lesson_match_for_new_lesson


_REUSABLE_CODES = frozenset({"reuse_as_is", "optimize"})
_REMOVE_CODES = frozenset({"remove"})


def _reuse_label_map() -> dict[str, str]:
    return {
        r["code"]: r["label"]
        for r in list_dictionary_entries(category="reuse_action")
    }


def _change_label_map() -> dict[str, str]:
    return {
        r["code"]: r["label"]
        for r in list_dictionary_entries(category="change_type")
    }


def build_reuse_report_data(
    *,
    lesson_match_id: str,
    old_les,
    new_les,
    matches: list[BlockMatch],
) -> dict:
    reuse_labels = _reuse_label_map()
    change_labels = _change_label_map()

    old_blocks = {b.id: b for b in Block.query.filter_by(lesson_id=old_les.id).all()}
    new_blocks = {b.id: b for b in Block.query.filter_by(lesson_id=new_les.id).all()}

    from ....models import TextbookAtom

    old_atoms = {a.atom_code: a for a in TextbookAtom.query.filter_by(lesson_id=old_les.id).all()}
    new_atoms = {a.atom_code: a for a in TextbookAtom.query.filter_by(lesson_id=new_les.id).all()}

    all_cw: set[int] = set()
    for block in old_blocks.values():
        for pg in block.course_slide_indices or []:
            all_cw.add(int(pg))

    reusable_slides: set[int] = set()
    slides_to_remove: list[dict] = []
    slides_to_add: list[dict] = []
    block_summaries: list[dict] = []

    matched_old: set[str] = set()
    matched_new: set[str] = set()

    for bm in matches:
        old_block = old_blocks.get(bm.old_block_id or "")
        new_block = new_blocks.get(bm.new_block_id or "")
        if old_block:
            matched_old.add(old_block.id)
        if new_block:
            matched_new.add(new_block.id)

        reuse_code = (bm.reuse_action or "").strip()
        change_code = (bm.change_type or "").strip()
        cw_pgs = sorted(int(x) for x in ((old_block.course_slide_indices or []) if old_block else []))

        block_summaries.append(
            {
                "old_block": (
                    f"{old_block.block_code} {old_block.block_name}"
                    if old_block
                    else None
                ),
                "new_block": (
                    f"{new_block.block_code} {new_block.block_name}"
                    if new_block
                    else None
                ),
                "reuse_action": reuse_labels.get(reuse_code, reuse_code),
                "reuse_action_code": reuse_code,
                "change_type": change_labels.get(change_code, change_code),
                "change_type_code": change_code,
                "teacher_note": bm.teacher_note or "",
                "course_slides": cw_pgs,
            }
        )

        if reuse_code in _REUSABLE_CODES:
            reusable_slides.update(cw_pgs)
        elif reuse_code in _REMOVE_CODES and cw_pgs:
            for pg in cw_pgs:
                slides_to_remove.append(
                    {
                        "slide_index": pg,
                        "reason": bm.teacher_note or change_labels.get(change_code, "内容删除"),
                    }
                )
        elif reuse_code in ("new_build", "reference") and new_block:
            slides_to_add.append(
                {
                    "topic": new_block.block_name,
                    "reason": bm.teacher_note or change_labels.get(change_code, "新增/重制"),
                }
            )

    for ob in old_blocks.values():
        if ob.id in matched_old:
            continue
        for pg in ob.course_slide_indices or []:
            slides_to_remove.append(
                {
                    "slide_index": int(pg),
                    "reason": f"旧块 {ob.block_code} 未配对",
                }
            )

    for nb in new_blocks.values():
        if nb.id in matched_new:
            continue
        slides_to_add.append(
            {
                "topic": nb.block_name,
                "reason": f"新区块 {nb.block_code} 未纳入对比",
            }
        )

    total_cw = len(all_cw) or 1
    reuse_ratio = round(len(reusable_slides) / total_cw, 3)

    reusable_sorted = sorted(reusable_slides)
    summary = (
        f"建议复用 {len(reusable_sorted)}/{len(all_cw)} 张课件页"
        f"（直接沿用+优化调整）；"
        f"需新增 {len(slides_to_add)} 个模块；"
        f"可跳过/删除 {len(slides_to_remove)} 张课件页。"
    )

    detail_json = {
        "block_summaries": block_summaries,
        "reusable_slides": reusable_sorted,
        "slides_to_add": slides_to_add,
        "slides_to_remove": slides_to_remove,
        "total_courseware_slides": sorted(all_cw),
        "matched_pair_count": len(matches),
    }
    return {
        "reuse_ratio": reuse_ratio,
        "summary": summary,
        "detail_json": detail_json,
    }


def upsert_reuse_report(
    *,
    new_lesson_uid: str,
    lesson_match_id: str | None = None,
) -> ReuseReport:
    lesson_match, old_les, new_les = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    matches = BlockMatch.query.filter_by(lesson_match_id=lesson_match.id).all()
    if not matches:
        raise ValueError("尚无区块配对，无法生成复用报告")

    payload = build_reuse_report_data(
        lesson_match_id=lesson_match.id,
        old_les=old_les,
        new_les=new_les,
        matches=matches,
    )

    report = ReuseReport.query.filter_by(match_id=lesson_match.id).first()
    if not report:
        report = ReuseReport(match_id=lesson_match.id)
        db.session.add(report)
    report.reuse_ratio = payload["reuse_ratio"]
    report.summary = payload["summary"]
    report.detail_json = payload["detail_json"]
    report.created_at = datetime.utcnow()
    db.session.commit()
    return report


def reuse_report_payload(report: ReuseReport | None) -> dict | None:
    if not report:
        return None
    return {
        "id": report.id,
        "match_id": report.match_id,
        "reuse_ratio": report.reuse_ratio,
        "summary": report.summary,
        "detail_json": report.detail_json or {},
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }
