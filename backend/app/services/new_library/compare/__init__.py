"""compare-v2 服务。"""
from .apply_anchors import apply_anchor_matches
from .auto_suggest import apply_ai_suggestion, run_auto_suggest
from .confirm import confirm_compare, unlock_compare
from .export import build_export_payload, persist_compare_export, refresh_compare_artifacts
from .export_pptx import generate_pptx, pptx_output_path
from .matches import create_match, delete_match, update_match
from .resolve import get_lesson_match_for_new_lesson
from .reuse_report import reuse_report_payload, upsert_reuse_report
from .workspace import build_compare_workspace

__all__ = [
    "apply_anchor_matches",
    "apply_ai_suggestion",
    "build_compare_workspace",
    "build_export_payload",
    "confirm_compare",
    "create_match",
    "delete_match",
    "generate_pptx",
    "get_lesson_match_for_new_lesson",
    "persist_compare_export",
    "pptx_output_path",
    "refresh_compare_artifacts",
    "reuse_report_payload",
    "run_auto_suggest",
    "unlock_compare",
    "update_match",
    "upsert_reuse_report",
]
