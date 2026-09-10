"""单页插图比对：A 版面 / C 图义·图注 / B 画面相似度。"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ...models import Volume
from ...parsers.pdf_spread import layout_for_volume, render_view_page_png
from .atom_compare import (
    PdfSource,
    _atom_plain,
    _fuzz_ratio,
    _pdf_path_for_volume,
    _serialize_atom,
    _strip_for_content,
    _ymid,
)

_log = logging.getLogger(__name__)

_IMAGE_TYPES = frozenset({"image"})
_TEXT_TYPES = frozenset({"text", "title", "caption"})


def _bbox(atom: dict) -> dict[str, float]:
    if atom.get("bbox"):
        bb = atom["bbox"]
        return {
            "x_start": float(bb.get("x_start", 0)),
            "y_start": float(bb.get("y_start", 0)),
            "x_end": float(bb.get("x_end", 1)),
            "y_end": float(bb.get("y_end", 1)),
        }
    return {
        "x_start": float(atom.get("x_start", 0)),
        "y_start": float(atom.get("y_start", 0)),
        "x_end": float(atom.get("x_end", 1)),
        "y_end": float(atom.get("y_end", 1)),
    }


def _area(bb: dict[str, float]) -> float:
    return max(0.0, bb["x_end"] - bb["x_start"]) * max(0.0, bb["y_end"] - bb["y_start"])


def _image_atoms(atoms: list[dict]) -> list[dict]:
    out = [a for a in atoms if (a.get("atom_type") or "") in _IMAGE_TYPES]
    out.sort(key=lambda a: (_ymid(a if "y_start" in a else {**a, **_bbox(a)}), _bbox(a)["x_start"]))
    return out


def _text_atoms(atoms: list[dict]) -> list[dict]:
    return [a for a in atoms if (a.get("atom_type") or "") in _TEXT_TYPES and _atom_plain(a)]


def _image_anchor_score(old_a: dict, new_a: dict) -> float:
    ob, nb = _bbox(old_a), _bbox(new_a)
    oy = (ob["y_start"] + ob["y_end"]) / 2.0
    ny = (nb["y_start"] + nb["y_end"]) / 2.0
    ox = (ob["x_start"] + ob["x_end"]) / 2.0
    nx = (nb["x_start"] + nb["x_end"]) / 2.0
    pos = max(0.0, 1.0 - (abs(oy - ny) / 0.22 + abs(ox - nx) / 0.35) / 2.0)
    oa, na = _area(ob), _area(nb)
    if oa <= 0 or na <= 0:
        area_sim = 0.0
    else:
        ratio = min(oa, na) / max(oa, na)
        area_sim = float(ratio)
    return 0.65 * pos + 0.35 * area_sim


def align_image_atoms(old_atoms: list[dict], new_atoms: list[dict]) -> dict:
    """A：插图数量 / 位置锚定 / 增删。"""
    old_imgs = _image_atoms(old_atoms)
    new_imgs = _image_atoms(new_atoms)
    used_new: set[int] = set()
    pairs: list[dict] = []

    for o in old_imgs:
        best_j, best = -1, 0.0
        for j, n in enumerate(new_imgs):
            if j in used_new:
                continue
            score = _image_anchor_score(o, n)
            if score > best:
                best, best_j = score, j
        if best_j < 0 or best < 0.38:
            continue
        used_new.add(best_j)
        n = new_imgs[best_j]
        ob, nb = _bbox(o), _bbox(n)
        dy = abs(((ob["y_start"] + ob["y_end"]) / 2) - ((nb["y_start"] + nb["y_end"]) / 2))
        change = "位置偏移" if dy >= 0.08 else "版面对齐"
        pairs.append(
            {
                "old_atom_id": o.get("atom_id"),
                "new_atom_id": n.get("atom_id"),
                "old_bbox": ob,
                "new_bbox": nb,
                "anchor_score": round(best, 3),
                "change": change,
            }
        )

    matched_old = {p["old_atom_id"] for p in pairs}
    matched_new = {p["new_atom_id"] for p in pairs}
    unmatched_old = [_serialize_atom(a, side="old") for a in old_imgs if a.get("atom_id") not in matched_old]
    unmatched_new = [_serialize_atom(a, side="new") for a in new_imgs if a.get("atom_id") not in matched_new]

    if not old_imgs and not new_imgs:
        verdict = "无插图"
    elif not unmatched_old and not unmatched_new and all(p["change"] == "版面对齐" for p in pairs):
        verdict = "版面一致"
    elif unmatched_old or unmatched_new:
        verdict = "插图增删"
    else:
        verdict = "版面有偏移"

    return {
        "pairs": pairs,
        "unmatched_old": unmatched_old,
        "unmatched_new": unmatched_new,
        "verdict": verdict,
        "summary": {
            "old_image_count": len(old_imgs),
            "new_image_count": len(new_imgs),
            "paired": len(pairs),
            "unmatched_old": len(unmatched_old),
            "unmatched_new": len(unmatched_new),
            "verdict": verdict,
        },
    }


_PINYIN_PAREN_RE = re.compile(
    r"\([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ\s]+\)"
)
_ILLUS_LABEL_RE = re.compile(r"^\[?\s*插图\s*\]?\s*[:：]?\s*", re.I)
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?])")


def _strip_pinyin(text: str) -> str:
    return _PINYIN_PAREN_RE.sub("", text or "")


def _theme_name_from_text(text: str, *, max_len: int = 18) -> str:
    """从邻近文字提短主题名（去拼音后取首分句）。"""
    t = _strip_pinyin(text)
    t = re.sub(r"\s+", "", t)
    if not t:
        return ""
    for sep in ("，", "。", "；", "！", "？", ",", ".", ";", "!", "?"):
        if sep in t:
            t = t.split(sep, 1)[0]
            break
    return t[:max_len]


def _image_layout_label(image: dict) -> str:
    """插图 OCR/版面 LLM 写入的主题：content 形如「[插图] 窗台下的豌豆花…」。"""
    for key in ("layout_label", "label", "theme"):
        raw = (image.get(key) or "").strip()
        if raw and raw not in ("[插图]", "插图"):
            return _ILLUS_LABEL_RE.sub("", raw).strip()
    for key in ("content", "ocr_text", "display_text"):
        raw = (image.get(key) or "").strip()
        if not raw:
            continue
        cleaned = _ILLUS_LABEL_RE.sub("", raw).strip()
        if cleaned and cleaned not in ("[插图]", "插图"):
            return cleaned[:80]
    return ""


def _char_set(text: str) -> set[str]:
    return {
        ch
        for ch in _strip_for_content(_strip_pinyin(text or ""))
        if "\u4e00" <= ch <= "\u9fff" or ch.isalnum()
    }


def _overlap_ratio(a: str, b: str) -> float:
    ha, hb = _char_set(a), _char_set(b)
    if not ha or not hb:
        return 0.0
    return len(ha & hb) / max(len(ha | hb), 1)


def _proximity_to_image(image: dict, text_atom: dict) -> float:
    """图上/下/旁邻近分；页底插图优先吃上方正文。"""
    bb, tb = _bbox(image), _bbox(text_atom)
    iy = (bb["y_start"] + bb["y_end"]) / 2.0
    ty = (tb["y_start"] + tb["y_end"]) / 2.0
    x_overlap = min(bb["x_end"], tb["x_end"]) - max(bb["x_start"], tb["x_start"])
    overlap_ratio = x_overlap / max(1e-6, bb["x_end"] - bb["x_start"])

    below = tb["y_start"] >= bb["y_end"] - 0.02 and tb["y_start"] <= bb["y_end"] + 0.20
    above = tb["y_end"] <= bb["y_start"] + 0.02 and tb["y_end"] >= bb["y_start"] - 0.35
    side = abs(ty - iy) < 0.10 and (
        tb["x_start"] >= bb["x_end"] - 0.02 or tb["x_end"] <= bb["x_start"] + 0.02
    )
    if not below and not above and not side:
        # 同页弱邻近：仍给一点分，便于「标签词 ↔ 正文词」全页匹配
        if overlap_ratio < 0.05 and abs(ty - iy) > 0.45:
            return 0.0
        dist = abs(ty - iy)
        return max(0.0, 0.28 * (1.0 - min(1.0, dist / 0.7))) * max(0.2, overlap_ratio + 0.3)

    if below or above:
        if overlap_ratio < 0.12:
            return 0.05
        edge = bb["y_end"] if below else bb["y_start"]
        ref = tb["y_start"] if below else tb["y_end"]
        dist = abs(ref - edge)
        base = 1.0 - min(1.0, dist / (0.20 if below else 0.35))
        # 图正上方正文略优先（页底插图常见）
        bonus = 0.08 if above else 0.0
        return max(0.0, base + bonus) + 0.25 * max(0.0, overlap_ratio)
    return 0.55


def _page_phrase_candidates(text_atoms: list[dict]) -> list[tuple[dict, str]]:
    """本页可匹配的句子/短段（去拼音）。"""
    out: list[tuple[dict, str]] = []
    seen: set[str] = set()
    for atom in text_atoms:
        raw = _strip_pinyin(_atom_plain(atom)).strip()
        if not raw:
            continue
        parts = [p.strip() for p in _SENT_SPLIT_RE.split(raw) if p and p.strip()]
        if not parts:
            parts = [raw]
        for part in parts:
            t = re.sub(r"\s+", "", part)
            if len(t) < 4 or len(t) > 120:
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append((atom, t))
        # 整段也留一条（较短时）
        whole = re.sub(r"\s+", "", raw)
        if 4 <= len(whole) <= 80 and whole not in seen:
            seen.add(whole)
            out.append((atom, whole))
    return out


def _score_phrase_to_label(phrase: str, label: str) -> float:
    if not phrase or not label:
        return 0.0
    fuzz = _fuzz_ratio(phrase, label)
    overlap = _overlap_ratio(phrase, label)
    # 标签词是否出现在句子中
    hits = 0
    label_chars = _char_set(label)
    phrase_chars = _char_set(phrase)
    if label_chars and phrase_chars:
        hits = len(label_chars & phrase_chars) / max(len(label_chars), 1)
    return 0.35 * fuzz + 0.35 * overlap + 0.30 * hits


def _match_page_text_for_image(
    image: dict,
    text_atoms: list[dict],
    *,
    hint_label: str = "",
) -> tuple[str, str, float]:
    """
    将插图对齐到本页某句/词组。
    返回 (matched_phrase, theme_short, score)。
    """
    candidates = _page_phrase_candidates(text_atoms)
    if not candidates:
        return "", "", 0.0
    best_phrase, best_theme, best = "", "", -1.0
    for atom, phrase in candidates:
        prox = _proximity_to_image(image, atom)
        if hint_label:
            content = _score_phrase_to_label(phrase, hint_label)
            score = 0.62 * content + 0.38 * prox
        else:
            # 无标签：邻近正文优先
            score = 0.85 * prox + 0.15 * min(1.0, len(_char_set(phrase)) / 12.0)
        if score > best:
            best = score
            best_phrase = phrase
            best_theme = _theme_name_from_text(hint_label or phrase, max_len=20)
            if hint_label and _overlap_ratio(phrase, hint_label) >= 0.35:
                # 有标签时主题尽量用标签短名，文句作「对应」
                best_theme = _theme_name_from_text(hint_label, max_len=20) or best_theme
    if best < (0.28 if hint_label else 0.35):
        return "", "", 0.0
    return best_phrase, best_theme, round(best, 3)


def _nearby_text_for_theme(image: dict, text_atoms: list[dict]) -> str:
    """图上/下/旁最近文字，可取正文（用于主题，不限短 caption）。"""
    best_t, best_score = "", -1.0
    for t in text_atoms:
        text = _atom_plain(t)
        if not text or len(text) > 200:
            continue
        score = _proximity_to_image(image, t)
        # 只要明显邻近
        if score < 0.35:
            continue
        if score > best_score:
            best_score, best_t = score, text
    return best_t


def _resolve_image_theme(image: dict, text_atoms: list[dict]) -> dict[str, Any]:
    """
    C1：读明白插图含义，并匹配本页对应文句。
    优先级：版面标签 → 标签对齐的正文 → 邻近正文。
    """
    label = _image_layout_label(image)
    matched, theme_from_page, match_score = _match_page_text_for_image(
        image, text_atoms, hint_label=label
    )
    if label:
        theme = _theme_name_from_text(label, max_len=20) or theme_from_page
        source = "layout_label"
        if matched:
            source = "layout_label+page_match"
        return {
            "theme": theme,
            "matched_text": matched[:100],
            "layout_label": label[:80],
            "source": source,
            "match_score": match_score,
        }
    if matched and theme_from_page:
        return {
            "theme": theme_from_page,
            "matched_text": matched[:100],
            "layout_label": "",
            "source": "page_match",
            "match_score": match_score,
        }
    near = _nearby_text_for_theme(image, text_atoms)
    theme = _theme_name_from_text(near, max_len=18)
    return {
        "theme": theme,
        "matched_text": (near or "")[:100] if theme else "",
        "layout_label": "",
        "source": "nearby" if theme else "",
        "match_score": 0.0,
    }


def _looks_like_body_sentence(text: str) -> bool:
    """完整叙述句（正文首句）不当真图注。"""
    t = (text or "").strip()
    if len(t) > 20:
        return True
    if "，" in t and ("。" in t or len(t) >= 14):
        return True
    if t.endswith("。") and len(t) >= 12:
        return True
    return False


def _true_caption_for_image(image: dict, text_atoms: list[dict]) -> str:
    """仅 caption 角色或图下极短说明；正文首句返回空。"""
    bb = _bbox(image)
    best_t, best_score = "", -1.0
    for t in text_atoms:
        tb = _bbox(t)
        text = _atom_plain(t)
        if not text:
            continue
        is_caption_type = (t.get("atom_type") or "") == "caption"
        if not is_caption_type:
            if len(text) > 20 or _looks_like_body_sentence(text):
                continue
        below = tb["y_start"] >= bb["y_end"] - 0.02 and tb["y_start"] <= bb["y_end"] + 0.10
        side = (
            abs(((tb["y_start"] + tb["y_end"]) / 2) - ((bb["y_start"] + bb["y_end"]) / 2)) < 0.08
            and (tb["x_start"] >= bb["x_end"] - 0.02 or tb["x_end"] <= bb["x_start"] + 0.02)
        )
        if not below and not side:
            continue
        x_overlap = min(bb["x_end"], tb["x_end"]) - max(bb["x_start"], tb["x_start"])
        overlap_ratio = x_overlap / max(1e-6, bb["x_end"] - bb["x_start"])
        if below and overlap_ratio < 0.2 and not is_caption_type:
            continue
        dist = abs(tb["y_start"] - bb["y_end"]) if below else 0.05
        score = (1.0 - min(1.0, dist / 0.12)) + 0.3 * max(0.0, overlap_ratio)
        if is_caption_type:
            score += 0.5
        if score > best_score:
            best_score, best_t = score, text
    return best_t


def _caption_for_image(image: dict, text_atoms: list[dict]) -> str:
    """兼容旧调用：等价于邻近主题原文（非真图注）。"""
    return _nearby_text_for_theme(image, text_atoms)


def _theme_change(old_theme: str, new_theme: str) -> tuple[str, float]:
    if not old_theme and not new_theme:
        return "无法命名", 0.0
    if not old_theme or not new_theme:
        return "无法命名", 0.0
    oc = _strip_for_content(old_theme)
    nc = _strip_for_content(new_theme)
    if not oc or not nc:
        return "无法命名", 0.0
    if oc == nc:
        return "主题一致", 1.0
    sim = _fuzz_ratio(oc, nc)
    overlap = _overlap_ratio(oc, nc)
    if sim >= 0.72 or oc in nc or nc in oc or overlap >= 0.55:
        return "主题一致", round(max(sim, overlap), 3)
    return "主题改写", round(sim, 3)


def _caption_change(old_cap: str, new_cap: str) -> tuple[str, float]:
    if not old_cap and not new_cap:
        return "无图注", 0.0
    if not old_cap:
        return "旧无图注", 0.0
    if not new_cap:
        return "新无图注", 0.0
    oc = _strip_for_content(_strip_pinyin(old_cap))
    nc = _strip_for_content(_strip_pinyin(new_cap))
    if oc == nc:
        return "图注一致", 1.0 if old_cap == new_cap else round(_fuzz_ratio(old_cap, new_cap), 3)
    sim = _fuzz_ratio(oc, nc)
    if sim >= 0.85:
        return "图注一致", round(sim, 3)
    return "图注改写", round(sim, 3)


def compare_image_captions(
    old_atoms: list[dict],
    new_atoms: list[dict],
    pairs: list[dict],
) -> dict:
    """C：C1 图义主题（读图+匹配本页文句）+ C2 真图注。"""
    old_text = _text_atoms(old_atoms)
    new_text = _text_atoms(new_atoms)
    old_by_id = {a.get("atom_id"): a for a in _image_atoms(old_atoms)}
    new_by_id = {a.get("atom_id"): a for a in _image_atoms(new_atoms)}
    rows: list[dict] = []
    for p in pairs:
        o = old_by_id.get(p.get("old_atom_id"))
        n = new_by_id.get(p.get("new_atom_id"))
        if not o or not n:
            continue
        old_res = _resolve_image_theme(o, old_text)
        new_res = _resolve_image_theme(n, new_text)
        old_theme = old_res.get("theme") or ""
        new_theme = new_res.get("theme") or ""
        theme_change, theme_sim = _theme_change(old_theme, new_theme)

        old_cap = _true_caption_for_image(o, old_text)
        new_cap = _true_caption_for_image(n, new_text)
        caption_change, cap_sim = _caption_change(old_cap, new_cap)

        # 兼容旧字段 change：仅主题改写 / 图注改写算「有差异」
        if theme_change == "主题改写":
            change = "主题改写"
        elif caption_change == "图注改写":
            change = "图注改写"
        elif theme_change == "主题一致":
            change = "主题一致"
        else:
            change = theme_change

        rows.append(
            {
                "old_atom_id": p.get("old_atom_id"),
                "new_atom_id": p.get("new_atom_id"),
                "old_theme": old_theme,
                "new_theme": new_theme,
                "old_matched_text": old_res.get("matched_text") or "",
                "new_matched_text": new_res.get("matched_text") or "",
                "old_theme_source": old_res.get("source") or "",
                "new_theme_source": new_res.get("source") or "",
                "old_layout_label": old_res.get("layout_label") or "",
                "new_layout_label": new_res.get("layout_label") or "",
                "theme_change": theme_change,
                "theme_similarity": theme_sim,
                "old_caption": old_cap[:80],
                "new_caption": new_cap[:80],
                "caption_change": caption_change,
                "caption_similarity": cap_sim,
                # 兼容：旧 UI 曾展示 old_caption 为邻近正文；现改为主题名优先
                "old_label": old_theme or old_cap or "[插图]",
                "new_label": new_theme or new_cap or "[插图]",
                "change": change,
                "similarity": theme_sim,
            }
        )

    theme_changed = [r for r in rows if r["theme_change"] == "主题改写"]
    cap_changed = [r for r in rows if r["caption_change"] == "图注改写"]
    changed = theme_changed + [r for r in cap_changed if r not in theme_changed]
    if not pairs:
        verdict = "无锚定插图"
    elif theme_changed or cap_changed:
        verdict = "图义有差异"
    else:
        verdict = "图义一致"
    return {
        "rows": rows,
        "changed": changed,
        "verdict": verdict,
        "summary": {
            "row_count": len(rows),
            "changed": len(changed),
            "theme_changed": len(theme_changed),
            "caption_changed": len(cap_changed),
            "verdict": verdict,
        },
    }


def _crop_norm(png_path: Path, bb: dict[str, float], *, size: int = 96):
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    img = cv2.imread(str(png_path), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    x0 = max(0, min(w - 1, int(bb["x_start"] * w)))
    x1 = max(x0 + 1, min(w, int(bb["x_end"] * w)))
    y0 = max(0, min(h - 1, int(bb["y_start"] * h)))
    y1 = max(y0 + 1, min(h, int(bb["y_end"] * h)))
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    # 去一点边，减轻框线噪声
    ch, cw = crop.shape[:2]
    if ch > 8 and cw > 8:
        m = max(1, min(ch, cw) // 20)
        crop = crop[m : ch - m, m : cw - m]
    return cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)


def _hist_similarity(a, b) -> float:
    import cv2  # type: ignore

    if a is None or b is None:
        return 0.0
    ha = cv2.calcHist([a], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
    hb = cv2.calcHist([b], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
    cv2.normalize(ha, ha)
    cv2.normalize(hb, hb)
    # correlation ∈ [-1,1] → [0,1]
    corr = float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL))
    return max(0.0, min(1.0, (corr + 1.0) / 2.0))


def _phash_similarity(a, b) -> float:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    if a is None or b is None:
        return 0.0

    def phash(img):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (32, 32), interpolation=cv2.INTER_AREA)
        # 简化 DCT 感知哈希：用块均值代替完整 DCT，足够做换图粗筛
        g = g.astype(np.float32)
        blocks = g.reshape(8, 4, 8, 4).mean(axis=(1, 3))
        med = float(np.median(blocks))
        return (blocks > med).flatten()

    ha, hb = phash(a), phash(b)
    same = int((ha == hb).sum())
    return same / float(len(ha))


def _visual_grade(sim: float) -> str:
    if sim >= 0.85:
        return "画面接近"
    if sim >= 0.55:
        return "可能有改动"
    return "疑似换图"


def compare_image_visuals(
    pairs: list[dict],
    *,
    old_png: Path | None,
    new_png: Path | None,
) -> dict:
    """B：本地直方图 + 感知哈希相似度。"""
    rows: list[dict] = []
    if not old_png or not new_png or not old_png.is_file() or not new_png.is_file():
        return {
            "rows": [],
            "verdict": "缺页图",
            "summary": {"row_count": 0, "low_similar": 0, "verdict": "缺页图"},
            "engine": "opencv_hist+phash",
        }
    for p in pairs:
        oc = _crop_norm(old_png, p["old_bbox"])
        nc = _crop_norm(new_png, p["new_bbox"])
        hist = _hist_similarity(oc, nc)
        ph = _phash_similarity(oc, nc)
        sim = round(0.55 * hist + 0.45 * ph, 3)
        grade = _visual_grade(sim)
        rows.append(
            {
                "old_atom_id": p.get("old_atom_id"),
                "new_atom_id": p.get("new_atom_id"),
                "similarity": sim,
                "hist_similarity": round(hist, 3),
                "phash_similarity": round(ph, 3),
                "grade": grade,
                "llm_summary": None,
            }
        )
    low = [r for r in rows if r["grade"] == "疑似换图"]
    if not pairs:
        verdict = "无锚定插图"
    elif not rows:
        verdict = "无法计算"
    elif low:
        verdict = "疑似换图"
    elif any(r["grade"] == "可能有改动" for r in rows):
        verdict = "画面可能有改动"
    else:
        verdict = "画面接近"
    # 可选 LLM：仅低相似且开关打开
    if low and os.getenv("DIFF_IMAGE_COMPARE_LLM", "").strip().lower() in ("1", "true", "yes", "on"):
        for r in low:
            r["llm_summary"] = "（LLM 加深未接入首期，保留开关占位）"
    return {
        "rows": rows,
        "verdict": verdict,
        "summary": {
            "row_count": len(rows),
            "low_similar": len(low),
            "verdict": verdict,
        },
        "engine": "opencv_hist+phash",
    }


def _render_page_png(volume: Volume, page_1: int, *, pdf_source: PdfSource = "full", preview_blob_id: str | None = None) -> Path:
    from ..volume_pdf import resolve_volume_pdf_path

    if pdf_source == "draft":
        pdf_path = resolve_volume_pdf_path(volume, source="draft", preview_blob_id=preview_blob_id)
    else:
        pdf_path = _pdf_path_for_volume(volume)
    layout = layout_for_volume(volume)
    td = Path(tempfile.mkdtemp(prefix="diff_imgcmp_"))
    out = td / f"{volume.volume_code}_p{page_1}.png"
    out.write_bytes(render_view_page_png(pdf_path, page_1, dpi=120, layout=layout))
    return out


def compare_page_images(
    old_atoms: list[dict],
    new_atoms: list[dict],
    *,
    old_png: Path | None = None,
    new_png: Path | None = None,
) -> dict:
    layout = align_image_atoms(old_atoms, new_atoms)
    captions = compare_image_captions(old_atoms, new_atoms, layout["pairs"])
    visuals = compare_image_visuals(layout["pairs"], old_png=old_png, new_png=new_png)

    parts = []
    if layout["verdict"] not in ("版面一致", "无插图"):
        parts.append(layout["verdict"])
    if captions["verdict"] == "图义有差异":
        parts.append("图义有差异")
    if visuals["verdict"] in ("疑似换图", "画面可能有改动"):
        parts.append(visuals["verdict"])
    if not parts:
        if layout["verdict"] == "无插图":
            verdict = "无插图"
        else:
            verdict = "插图一致"
    else:
        verdict = "；".join(parts)

    return {
        "verdict": verdict,
        "layout": layout,
        "captions": captions,
        "visuals": visuals,
        "summary": {
            "verdict": verdict,
            "layout": layout["summary"],
            "captions": captions["summary"],
            "visuals": visuals["summary"],
        },
    }


def compare_cached_page_images(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """读取两侧图片 OCR 缓存并做 A+B+C 比对；结果写入磁盘 + MySQL。"""
    from .atom_compare import (
        _build_atoms_payload,
        _load_pair_entry,
        _persist_image_compare_to_db,
        _save_image_compare,
        _valid_image_compare,
    )

    cache_file, entry = _load_pair_entry(
        old_vol,
        new_vol,
        old_page,
        new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    if not entry.get("old_image_ocr_done") or not entry.get("new_image_ocr_done"):
        raise ValueError("请先完成 ③ 图片 OCR（两侧）")

    if not force:
        cached_cmp = _valid_image_compare(entry)
        if cached_cmp is not None:
            return {
                "atoms": _build_atoms_payload(entry),
                "compare": cached_cmp,
                "from_cache": True,
            }

    old_raw = entry.get("old_raw") or []
    new_raw = entry.get("new_raw") or []

    old_png = new_png = None
    cleanup: list[Path] = []
    try:
        old_png = _render_page_png(old_vol, old_page)
        new_png = _render_page_png(
            new_vol,
            new_page,
            pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
        )
        cleanup.extend([old_png.parent, new_png.parent])
        compare = compare_page_images(old_raw, new_raw, old_png=old_png, new_png=new_png)
    finally:
        import shutil

        for d in cleanup:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    _save_image_compare(cache_file, entry, compare)
    _persist_image_compare_to_db(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        compare=compare,
    )
    return {
        "atoms": _build_atoms_payload(entry),
        "compare": compare,
        "from_cache": False,
    }
