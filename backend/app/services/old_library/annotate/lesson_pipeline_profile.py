"""旧库单课建块流水线配置（OCR 拆分 / 跳过整理 / 豆包建块）。"""
from __future__ import annotations

from typing import Any

# 旧库默认：豆包 OCR → 跳过版面整理 → 豆包建块（信任 LLM atom_codes，关闭 P0/P1/P2）
# image_refine_mode: off=跳过 P0/P1/P2 插图纠偏 | full=三步全开
# trust_llm_atoms: 建块后不再用规则覆盖豆包给出的 atom_codes，且跳过课件预匹配
_OLD_LIBRARY_DEFAULT: dict[str, Any] = {
    "split_ocr": True,
    "doubao_text_ocr": True,
    "skip_curate": True,
    "teaching_intent_on_seed": True,
    "image_refine_mode": "off",
    "atom_bind_mode": "legacy",
    "trust_llm_atoms": True,
    "label": "旧库 · 建块",
}

# 单课覆盖（可选 label 或局部开关）
LESSON_PIPELINE_PROFILES: dict[str, dict[str, Any]] = {}

IMAGE_REFINE_MODES: tuple[str, ...] = ("off", "p0", "full")


def _is_old_library_lesson_uid(lesson_uid: str) -> bool:
    uid = (lesson_uid or "").strip().lower()
    if not uid:
        return False
    return "-old-" in uid or uid.startswith("u-old-")


def get_lesson_pipeline_profile(lesson_uid: str) -> dict[str, Any]:
    uid = (lesson_uid or "").strip()
    override = dict(LESSON_PIPELINE_PROFILES.get(uid) or {})
    if override:
        base = {**_OLD_LIBRARY_DEFAULT, **override}
    elif _is_old_library_lesson_uid(uid):
        base = dict(_OLD_LIBRARY_DEFAULT)
    else:
        base = {}
    return {
        "enabled": bool(base),
        "split_ocr": bool(base.get("split_ocr")),
        "doubao_text_ocr": bool(base.get("doubao_text_ocr")),
        "skip_curate": bool(base.get("skip_curate")),
        "teaching_intent_on_seed": bool(base.get("teaching_intent_on_seed")),
        "image_refine_mode": _normalize_image_refine_mode(base.get("image_refine_mode")),
        "atom_bind_mode": _normalize_atom_bind_mode(base.get("atom_bind_mode")),
        "trust_llm_atoms": bool(base.get("trust_llm_atoms")),
        "label": base.get("label") or "",
    }


def _normalize_atom_bind_mode(raw: Any) -> str:
    mode = str(raw or "legacy").strip().lower()
    return mode if mode in ATOM_BIND_MODES else "legacy"


ATOM_BIND_MODES: tuple[str, ...] = ("legacy", "prematch")


def _normalize_image_refine_mode(raw: Any) -> str:
    mode = str(raw or "full").strip().lower()
    return mode if mode in IMAGE_REFINE_MODES else "full"


def profile_skip_curate(lesson_uid: str) -> bool:
    return get_lesson_pipeline_profile(lesson_uid).get("skip_curate", False)


def profile_image_refine_mode(lesson_uid: str) -> str:
    return get_lesson_pipeline_profile(lesson_uid).get("image_refine_mode", "full")


def profile_atom_bind_mode(lesson_uid: str) -> str:
    return get_lesson_pipeline_profile(lesson_uid).get("atom_bind_mode", "legacy")


def profile_trust_llm_atoms(lesson_uid: str) -> bool:
    return bool(get_lesson_pipeline_profile(lesson_uid).get("trust_llm_atoms"))
