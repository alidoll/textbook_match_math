"""compare-v2 export.json（对齐 courseware-migration _build_export_payload）。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ....models import Volume
from ....repo_paths import repo_root
from ....services.dictionary import list_dictionary_entries
from .resolve import get_lesson_match_for_new_lesson
from .reuse_report import reuse_report_payload, upsert_reuse_report
from .workspace import build_compare_workspace


def _block_export_row(block: dict) -> dict:
    codes = list(block.get("atom_codes") or [])
    meta = block.get("metadata") or {}
    row = {
        "block_id": block.get("block_id") or block.get("block_code"),
        "block_name": block.get("block_name") or "",
        "atom_ids": codes,
        "atom_codes": codes,
        "cw_pgs": block.get("cw_pgs") or [],
        "old_tb_pgs": block.get("old_tb_pgs") or [],
        "new_tb_pgs": block.get("new_tb_pgs") or [],
        "sort_order": block.get("sort_order") or 0,
        "metadata": meta,
    }
    if meta.get("anchor_old_refs"):
        row["anchor_old_refs"] = meta["anchor_old_refs"]
    return row


def _match_export_row(match: dict, reuse_labels: dict[str, str], change_labels: dict[str, str]) -> dict:
    reuse_code = match.get("reuse_action") or ""
    change_code = match.get("change_type") or ""
    ai_reuse = match.get("ai_reuse_action") or ""
    ai_change = match.get("ai_change_type") or ""
    text_change = match.get("ai_text_change") or {}
    return {
        "match_id": match.get("match_id"),
        "old_block_id": match.get("old_block_id"),
        "new_block_id": match.get("new_block_id"),
        "match_type": match.get("match_type") or "1:1",
        "reuse_action": reuse_labels.get(reuse_code, reuse_code),
        "reuse_action_code": reuse_code,
        "change_type": change_labels.get(change_code, change_code),
        "change_type_code": change_code,
        "teacher_note": match.get("teacher_note") or "",
        "ai_reuse_action": reuse_labels.get(ai_reuse, ai_reuse),
        "ai_reuse_action_code": ai_reuse,
        "ai_change_type": change_labels.get(ai_change, ai_change),
        "ai_change_type_code": ai_change,
        "ai_teacher_note": match.get("ai_teacher_note") or "",
        "ai_rationale": match.get("ai_rationale") or [],
        "ai_change_points": match.get("ai_change_points") or [],
        "ai_confidence": match.get("ai_confidence") or "",
        "ai_optimize_subtype": match.get("ai_optimize_subtype"),
        "ai_match_score": match.get("ai_match_score"),
        "text_change_ratio": text_change.get("change_ratio"),
        "ai_source": match.get("ai_source") or "",
    }


def build_export_payload(
    *,
    new_lesson_uid: str,
    lesson_match_id: str | None = None,
) -> dict:
    workspace = build_compare_workspace(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match_id
    )
    match, old_les, new_les = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    old_vol = Volume.query.get(old_les.volume_id)
    new_vol = Volume.query.get(new_les.volume_id)

    reuse_labels = {
        r["code"]: r["label"] for r in list_dictionary_entries(category="reuse_action")
    }
    change_labels = {
        r["code"]: r["label"] for r in list_dictionary_entries(category="change_type")
    }

    atoms = []
    for a in workspace.get("float_atoms") or []:
        atoms.append(
            {
                **a,
                "atom_id": a.get("atom_code"),
                "text": (a.get("text") or a.get("content") or "").strip(),
            }
        )

    from ....models import ReuseReport

    report = ReuseReport.query.filter_by(match_id=match.id).first()
    report_data = reuse_report_payload(report)

    return {
        "ok": True,
        "lesson_id": new_les.lesson_uid,
        "lesson_uid": new_les.lesson_uid,
        "lesson_name": new_les.lesson_name,
        "old_lesson_uid": old_les.lesson_uid,
        "old_lesson_name": old_les.lesson_name,
        "lesson_match_id": match.id,
        "old_blocks": [_block_export_row(b) for b in workspace.get("old_blocks") or []],
        "new_blocks": [_block_export_row(b) for b in workspace.get("new_blocks") or []],
        "block_matches": [
            _match_export_row(m, reuse_labels, change_labels)
            for m in workspace.get("matches") or []
        ],
        "atoms": atoms,
        "float_atoms": atoms,
        "cw_pages": workspace.get("cw_pages") or [],
        "old_pages": workspace.get("old_pages") or [],
        "new_pages": workspace.get("new_pages") or [],
        "assets": {
            "courseware_pages": workspace.get("cw_pages") or [],
            "old_pages": workspace.get("old_pages") or [],
            "new_pages": workspace.get("new_pages") or [],
        },
        "compare_confirmed_at": workspace.get("compare_confirmed_at"),
        "compare_confirmed_by": workspace.get("compare_confirmed_by"),
        "edition": new_vol.edition if new_vol else "",
        "subject": new_vol.subject if new_vol else "",
        "version_name": new_vol.edition if new_vol else "",
        "volume_code": new_vol.volume_code if new_vol else "",
        "old_volume_code": old_vol.volume_code if old_vol else "",
        "grade_name": str(new_vol.grade) if new_vol else "",
        "term_name": new_vol.semester if new_vol else "",
        "reuse_report": report_data,
        "lesson_grade": workspace.get("lesson_grade"),
        "pre_stats": workspace.get("pre_stats"),
        "page_levels": workspace.get("page_levels") or [],
        "qa_flags": workspace.get("qa_flags") or [],
        "action_stats": workspace.get("action_stats"),
        "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


def export_file_path(new_lesson_uid: str) -> Path:
    safe = new_lesson_uid.replace("\\", "_").replace("/", "_")
    return repo_root() / "outputs" / f"export_{safe}.json"


def persist_compare_export(
    *,
    new_lesson_uid: str,
    lesson_match_id: str | None = None,
) -> str | None:
    try:
        out_dir = repo_root() / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = export_file_path(new_lesson_uid)
        payload = build_export_payload(
            new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match_id
        )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return str(path)
    except OSError:
        return None


def refresh_compare_artifacts(
    *,
    new_lesson_uid: str,
    lesson_match_id: str | None = None,
) -> dict:
    """更新 reuse_reports + export.json（配对变更或确认后调用）。"""
    report = upsert_reuse_report(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match_id
    )
    export_path = persist_compare_export(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match_id
    )
    return {
        "reuse_report": reuse_report_payload(report),
        "export_path": export_path,
    }
