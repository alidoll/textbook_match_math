"""Step2 目录复用匹配键与相似度（对齐 step2_reuse_directory_analysis.py）。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

try:
    from rapidfuzz import fuzz as _rf_fuzz
except ImportError:
    _rf_fuzz = None

# --- 常量（与 Step2 一致） ---
MAX_DYNAMIC_OLD_HITS_CAP = 25
MAX_REF_SIM_DYNAMIC_CAP = 1

REF_SIM_BLEND_CTX = 0.48
REF_SIM_THRESHOLD = 0.405
REF_CTX_FLOOR = 0.29
REF_SIM_LESSON_FLOOR = 0.37
REF_SIM_STRONG_LESSON = 0.44
REF_MATTER_TRILOGY_BONUS = 0.11

REF_PATH_LESSON_LES = 0.56
REF_PATH_LESSON_UNIT = 0.24
REF_PATH_LESSON_CTX = 0.20
REF_PATH_UNIT_UNIT = 0.40
REF_PATH_UNIT_CTX = 0.38
REF_PATH_UNIT_LES = 0.22
REF_PATH_IN_UNIT_UNIT = 0.28
REF_PATH_IN_UNIT_CTX = 0.44
REF_PATH_IN_UNIT_LES = 0.28
REF_PATH_IN_UNIT_EXTRA = 0.055
REF_PATH_IN_UNIT_TRIG = 0.50
REF_UNIT_RESCUE_FLOOR = 0.38
REF_SIM_HARD_JUNK_LES = 0.28
REF_SIM_HARD_JUNK_UNIT = 0.26
REF_SIM_HARD_JUNK_CTX = 0.28


def normalize_title(s: Any) -> str:
    if s is None or (isinstance(s, float) and str(s) == "nan"):
        return ""
    t = str(s).strip()
    t = re.sub(r"[\s\u00a0\u3000]+", " ", t)
    return t.strip()


def lesson_match_key(s: Any) -> str:
    t = normalize_title(s)
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"^0*(\d{1,2})[\s\u00a0\u3000\.．、]+", r"\1", t)
    for _ in range(8):
        nt = re.sub(r"(\d)\s+(?=[\u4e00-\u9fff])", r"\1", t)
        if nt == t:
            break
        t = nt
    t = re.sub(r"[\s\u00a0\u3000]+", "", t)
    return t


def lesson_reuse_body_key(s: Any) -> str:
    t = lesson_match_key(s)
    if not t:
        return ""
    return re.sub(r"^\d{1,3}(?=[\u4e00-\u9fff（])", "", t)


def lesson_reuse_match_key(s: Any) -> str:
    t = lesson_reuse_body_key(s)
    if not t:
        return ""
    t = re.sub(r"^第[一二三四五六七八九十百千零0-9]+课", "", t)
    return t.replace("的", "")


def unit_theme_match_key(u: Any) -> str:
    t = normalize_title(u)
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"^第[一二三四五六七八九十百千0-9]+单元\s*", "", t)
    for ch in (" ", "\u3000", "、", "，", ",", ";", "；", "·"):
        t = t.replace(ch, "")
    return t.replace("的", "")


def _longest_common_substring_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def raw_ref_similarity_score(ka: str, kb: str) -> float:
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    lcs = _longest_common_substring_len(ka, kb)
    lcs_bonus = 0.0
    if lcs >= 2:
        lcs_bonus = min(0.44, 0.09 + 0.11 * min(lcs, 6))
    sa, sb = set(ka), set(kb)
    jac = len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0
    jac_part = min(0.36, jac * 1.2)
    if _rf_fuzz is not None:
        r = _rf_fuzz.ratio(ka, kb) / 100.0
        pr = _rf_fuzz.partial_ratio(ka, kb) / 100.0
        tsr = _rf_fuzz.token_set_ratio(ka, kb) / 100.0
        base = max(r, pr * 0.96, tsr * 0.88)
    else:
        from difflib import SequenceMatcher

        base = SequenceMatcher(None, ka, kb).ratio()
    return float(min(0.999, max(base, lcs_bonus, jac_part)))


def lesson_ref_similarity_score(lesson_a: Any, lesson_b: Any) -> float:
    return raw_ref_similarity_score(
        lesson_reuse_match_key(lesson_a), lesson_reuse_match_key(lesson_b)
    )


def merge_unit_lesson_ref_score_line(unit: Any, lesson: Any) -> str:
    u = normalize_title(unit)
    lk = lesson_reuse_match_key(lesson)
    if not lk:
        return u
    if not u:
        return lk
    return f"{u} {lk}"


def unit_lesson_ref_context_score(
    unit_a: Any, lesson_a: Any, unit_b: Any, lesson_b: Any
) -> float:
    return raw_ref_similarity_score(
        merge_unit_lesson_ref_score_line(unit_a, lesson_a),
        merge_unit_lesson_ref_score_line(unit_b, lesson_b),
    )


def unit_ref_similarity_score(unit_a: Any, unit_b: Any) -> float:
    return raw_ref_similarity_score(
        unit_theme_match_key(unit_a), unit_theme_match_key(unit_b)
    )


def _unit_contains_solid_liquid_gas_trilogy(unit: Any) -> bool:
    t = normalize_title(unit)
    return bool(t) and ("固" in t and "液" in t and "气" in t)


def ref_similarity_matter_trilogy_bonus(u_new: Any, u_old: Any) -> float:
    if _unit_contains_solid_liquid_gas_trilogy(u_new) and _unit_contains_solid_liquid_gas_trilogy(
        u_old
    ):
        return float(REF_MATTER_TRILOGY_BONUS)
    return 0.0


def ref_similarity_multi_paths(
    unit_new: Any,
    lesson_new: str,
    unit_old: Any,
    lesson_old: Any,
) -> tuple[float, float, float, float]:
    sc_les = lesson_ref_similarity_score(lesson_new, lesson_old)
    sc_unit = unit_ref_similarity_score(unit_new, unit_old)
    sc_ctx = unit_lesson_ref_context_score(unit_new, lesson_new, unit_old, lesson_old)
    matter_b = ref_similarity_matter_trilogy_bonus(unit_new, unit_old)

    path_main = float(
        min(0.999, REF_SIM_BLEND_CTX * sc_ctx + (1.0 - REF_SIM_BLEND_CTX) * sc_les + matter_b)
    )
    path_lesson_led = float(
        REF_PATH_LESSON_LES * sc_les
        + REF_PATH_LESSON_UNIT * sc_unit
        + REF_PATH_LESSON_CTX * sc_ctx
    )
    path_unit_led = float(
        REF_PATH_UNIT_UNIT * sc_unit
        + REF_PATH_UNIT_CTX * sc_ctx
        + REF_PATH_UNIT_LES * sc_les
    )
    in_u_extra = REF_PATH_IN_UNIT_EXTRA if sc_unit >= REF_PATH_IN_UNIT_TRIG else 0.0
    path_in_similar_unit = float(
        REF_PATH_IN_UNIT_UNIT * sc_unit
        + REF_PATH_IN_UNIT_CTX * sc_ctx
        + REF_PATH_IN_UNIT_LES * sc_les
        + in_u_extra
    )
    sc_final = float(
        min(
            0.999,
            max(path_main, path_lesson_led, path_unit_led, path_in_similar_unit),
        )
    )
    return sc_final, sc_les, sc_unit, sc_ctx


_CHINESE_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
BODY_TEXT_MIN_CHINESE_CHARS = 40


def body_text_match_key(body: Any) -> str:
    """正文比对键：仅保留汉字，弱化 PDF 拼音噪声。"""
    if body is None:
        return ""
    return "".join(_CHINESE_CHAR_RE.findall(str(body)))


def body_text_similarity_score(body_a: Any, body_b: Any) -> float | None:
    """双方正文均足够长时返回相似度，否则 None（不参与正文加权）。"""
    ka = body_text_match_key(body_a)
    kb = body_text_match_key(body_b)
    if len(ka) < BODY_TEXT_MIN_CHINESE_CHARS or len(kb) < BODY_TEXT_MIN_CHINESE_CHARS:
        return None
    if _rf_fuzz is not None:
        return float(_rf_fuzz.token_set_ratio(ka, kb) / 100.0)
    return raw_ref_similarity_score(ka, kb)


def looks_like_unit_lesson_title_swap(
    unit_a: Any,
    lesson_a: Any,
    unit_b: Any,
    lesson_b: Any,
    *,
    cross_threshold: float = 0.48,
    margin_over_parallel: float = 0.12,
) -> bool:
    """新单元≈旧课名、新课名≈旧单元（两版目录对调）。"""
    u_a = unit_theme_match_key(unit_a)
    l_a = lesson_reuse_match_key(lesson_a)
    u_b = unit_theme_match_key(unit_b)
    l_b = lesson_reuse_match_key(lesson_b)
    if not u_a or not l_a or not u_b or not l_b:
        return False
    cross_a = raw_ref_similarity_score(u_a, l_b)
    cross_b = raw_ref_similarity_score(l_a, u_b)
    parallel_u = raw_ref_similarity_score(u_a, u_b)
    parallel_l = raw_ref_similarity_score(l_a, l_b)
    if cross_a < cross_threshold or cross_b < cross_threshold:
        return False
    return (cross_a + cross_b) >= (parallel_l + margin_over_parallel)


def format_old_lesson_hint(unit_title: str, lesson_text: str) -> str:
    u = normalize_title(unit_title)
    les = normalize_title(lesson_text)
    if u and les:
        return f"{u}｜{les}"
    return les or u
