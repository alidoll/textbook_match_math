"""教材区块文本 diff（M2）：支撑 5%/30%/70% 改动量边界。"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from .rules_config import (
    TEXT_CHANGE_DIRECT_REUSE_MAX,
    TEXT_CHANGE_NEW_BUILD_MIN,
    TEXT_CHANGE_OPTIMIZE_MAX,
)


def normalize_compare_text(text: str) -> str:
    """压缩空白与常见标点差异，便于可比。"""
    s = (text or "").strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[，。！？、；：""''（）【】《》…·\-—]", "", s)
    return s


def compute_text_change_ratio(old_text: str, new_text: str) -> dict:
    """
    返回改动量指标。主指标 change_ratio = 1 - char_similarity（0=完全相同，1=全换）。
    """
    old_n = normalize_compare_text(old_text)
    new_n = normalize_compare_text(new_text)

    if not old_n and not new_n:
        return {
            "char_similarity": 1.0,
            "token_similarity": 1.0,
            "change_ratio": 0.0,
            "bucket": "identical",
            "old_char_count": 0,
            "new_char_count": 0,
        }
    if not old_n or not new_n:
        return {
            "char_similarity": 0.0,
            "token_similarity": 0.0,
            "change_ratio": 1.0,
            "bucket": "new_build",
            "old_char_count": len(old_n),
            "new_char_count": len(new_n),
        }

    char_sim = fuzz.ratio(old_n, new_n) / 100.0
    token_sim = fuzz.token_set_ratio(old_text, new_text) / 100.0
    change_ratio = round(max(0.0, min(1.0, 1.0 - char_sim)), 4)

    if change_ratio <= TEXT_CHANGE_DIRECT_REUSE_MAX:
        bucket = "direct_reuse"
    elif change_ratio <= TEXT_CHANGE_OPTIMIZE_MAX:
        bucket = "optimize"
    elif change_ratio < TEXT_CHANGE_NEW_BUILD_MIN:
        bucket = "reference"
    else:
        bucket = "new_build"

    return {
        "char_similarity": round(char_sim, 4),
        "token_similarity": round(token_sim, 4),
        "change_ratio": change_ratio,
        "bucket": bucket,
        "old_char_count": len(old_n),
        "new_char_count": len(new_n),
    }


def diff_snippets(old_text: str, new_text: str, *, max_len: int = 40) -> dict:
    """提取便于写入修改要点的首尾片段。"""
    o = (old_text or "").replace("\n", " ").strip()
    n = (new_text or "").replace("\n", " ").strip()
    return {
        "old_excerpt": o[:max_len] + ("…" if len(o) > max_len else ""),
        "new_excerpt": n[:max_len] + ("…" if len(n) > max_len else ""),
    }
