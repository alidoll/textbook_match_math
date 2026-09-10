"""旧教材页 → 浮动原子（整页覆盖、坐标 0–1 归一化）。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_PINYIN_CHAR_RE = re.compile(
    r"[a-zA-Z\u0100-\u024f\u0300-\u036füÜǖǘǚǜ·'’\s]"
)
_ILLUSTRATION_MARKER = "[插图]"


def _atom_ocr_remove_stamp() -> bool:
    return os.getenv("ATOM_OCR_REMOVE_STAMP", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _atom_ocr_two_phase() -> bool:
    return os.getenv("ATOM_OCR_TWO_PHASE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _atom_center_in_mask(
    atom: dict[str, Any], mask: Any, img_w: int, img_h: int
) -> bool:
    cx = int((float(atom["x_start"]) + float(atom["x_end"])) / 2 * img_w)
    cy = int((float(atom["y_start"]) + float(atom["y_end"])) / 2 * img_h)
    cx = max(0, min(img_w - 1, cx))
    cy = max(0, min(img_h - 1, cy))
    return bool(mask[cy, cx] > 0)


def _atom_overlap_ratio_with_mask(
    atom: dict[str, Any], mask: Any, img_w: int, img_h: int
) -> float:
    import numpy as np

    x0 = int(float(atom["x_start"]) * img_w)
    y0 = int(float(atom["y_start"]) * img_h)
    x1 = int(float(atom["x_end"]) * img_w)
    y1 = int(float(atom["y_end"]) * img_h)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img_w, x1), min(img_h, y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    sub = mask[y0:y1, x0:x1]
    if sub.size == 0:
        return 0.0
    return float(np.count_nonzero(sub)) / float(sub.size)


def drop_atoms_in_stamp_mask(
    atoms: list[dict[str, Any]],
    stamp_mask: Any,
    img_w: int,
    img_h: int,
    *,
    min_overlap: float = 0.28,
) -> list[dict[str, Any]]:
    """去掉中心或大部分落在红章 mask 内的 OCR 误识别框。"""
    import numpy as np

    if stamp_mask is None or not np.any(stamp_mask):
        return atoms
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") not in ("text", "title"):
            out.append(atom)
            continue
        if _atom_center_in_mask(atom, stamp_mask, img_w, img_h):
            continue
        if _atom_overlap_ratio_with_mask(atom, stamp_mask, img_w, img_h) >= min_overlap:
            continue
        out.append(atom)
    return out


def drop_atoms_overlapping_exclude_regions(
    atoms: list[dict[str, Any]],
    exclude_regions: list[dict[str, Any]],
    *,
    min_overlap: float = 0.35,
) -> list[dict[str, Any]]:
    """去掉与 stamp_noise / page_number 等区域高度重叠的文本框。"""
    if not exclude_regions:
        return atoms
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") not in ("text", "title"):
            out.append(atom)
            continue
        drop = False
        for region in exclude_regions:
            if _containment_ratio(atom, region) >= min_overlap:
                drop = True
                break
            if _iou(atom, region) >= min_overlap * 0.85:
                drop = True
                break
        if not drop:
            out.append(atom)
    return out


def _image_label_from_content(content: str) -> str:
    text = (content or "").strip()
    for prefix in ("[插图]", "[image]", "[图片"):
        if text.startswith(prefix):
            rest = text[len(prefix) :].strip()
            if rest.startswith("]"):
                rest = rest[1:].strip()
            return rest[:500]
    return ""


def _propagate_layout_labels_to_cv(
    cv_images: list[dict[str, Any]], llm_images: list[dict[str, Any]]
) -> None:
    """OpenCV 框若与 LLM 框部分重合，把 label 贴到 CV 原子 content 上。"""
    for cv in cv_images:
        if _image_label_from_content(_atom_text(cv)):
            continue
        best_label = ""
        best_iou = 0.0
        for llm in llm_images:
            label = _image_label_from_content(_atom_text(llm))
            if not label:
                continue
            overlap = _iou(cv, llm)
            if overlap > best_iou:
                best_iou = overlap
                best_label = label
        if best_label and best_iou >= 0.12:
            cv["content"] = f"[插图] {best_label}"[:500]


def merge_layout_image_atoms(
    existing: list[dict[str, Any]], llm_images: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """LLM 插图框优先：与 OpenCV 框重合时保留 LLM、去掉 CV；label 可贴到未去重的 CV 框。"""
    if not llm_images:
        return existing
    kept = [a for a in existing if a.get("atom_type") != "image"]
    cv_images = [a for a in existing if a.get("atom_type") == "image"]
    for llm_atom in llm_images:
        cv_images = [
            cv
            for cv in cv_images
            if _iou(cv, llm_atom) < 0.35
            and _containment_ratio(cv, llm_atom) < 0.55
        ]
    _propagate_layout_labels_to_cv(cv_images, llm_images)
    return kept + cv_images + llm_images


def _atom_text(atom: dict[str, Any]) -> str:
    return (atom.get("content") or atom.get("ocr_text") or "").strip()


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _looks_like_pinyin(text: str) -> bool:
    """判断是否为拼音行（拉丁字母+声调，无汉字）。"""
    t = (text or "").strip()
    if not t or _has_cjk(t):
        return False
    if not re.search(r"[a-zA-Z]", t):
        return False
    pinyinish = sum(1 for ch in t if _PINYIN_CHAR_RE.fullmatch(ch))
    return pinyinish / len(t) >= 0.75


def _pinyin_syllables(text: str) -> list[str]:
    return [
        s
        for s in re.split(r"\s+", (text or "").strip())
        if s and re.search(r"[a-zA-Z]", s)
    ]


def _assign_pinyin_groups_to_columns(
    syllables: list[str], hanzi_columns: list[dict[str, Any]]
) -> list[str]:
    """按下方汉字字数把一条长拼音切成多段（活动条：推抽屉 / 铅球 / 弹簧）。"""
    if not syllables or len(hanzi_columns) < 2:
        return [" ".join(syllables)] if syllables else []
    char_counts = [
        max(1, len(re.sub(r"\s+", "", _atom_text(h)))) for h in hanzi_columns
    ]
    total_chars = sum(char_counts)
    n_syl = len(syllables)
    targets: list[int] = []
    assigned = 0
    for i, cc in enumerate(char_counts):
        if i == len(char_counts) - 1:
            targets.append(max(1, n_syl - assigned))
        else:
            t = max(1, round(n_syl * cc / total_chars))
            targets.append(t)
            assigned += t
    while sum(targets) > n_syl:
        j = targets.index(max(targets))
        targets[j] -= 1
    while sum(targets) < n_syl:
        targets[-1] += 1
    groups: list[str] = []
    idx = 0
    for t in targets:
        chunk = syllables[idx : idx + t]
        idx += t
        groups.append(" ".join(chunk))
    return groups


def _hanzi_columns_below_pinyin(
    pinyin_atom: dict[str, Any], texts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cols: list[dict[str, Any]] = []
    for other in texts:
        if other is pinyin_atom:
            continue
        ot = _atom_text(other)
        if not _has_cjk(ot) or _looks_like_pinyin(ot):
            continue
        if not _is_directly_below(pinyin_atom, other):
            continue
        hc = (float(other["x_start"]) + float(other["x_end"])) / 2.0
        px0, px1 = float(pinyin_atom["x_start"]), float(pinyin_atom["x_end"])
        if hc < px0 - 0.02 or hc > px1 + 0.02:
            if _x_overlap_ratio(pinyin_atom, other) < 0.08:
                continue
        cols.append(other)
    cols.sort(key=lambda a: float(a["x_start"]))
    return cols


def split_wide_pinyin_by_hanzi_columns(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """宽拼音行下方有多列汉字时，按列拆成多条拼音再参与配对。"""
    texts = [a for a in atoms if a.get("atom_type") in ("text", "title")]
    others = [a for a in atoms if a.get("atom_type") not in ("text", "title")]
    replacements: dict[int, list[dict[str, Any]]] = {}

    for atom in texts:
        at = _atom_text(atom)
        if not _looks_like_pinyin(at):
            continue
        cols = _hanzi_columns_below_pinyin(atom, texts)
        if len(cols) < 2:
            continue
        if not all(_looks_like_vocab_label(_atom_text(c)) for c in cols):
            continue
        if float(cols[-1]["x_start"]) - float(cols[0]["x_end"]) < 0.04:
            continue
        syllables = _pinyin_syllables(at)
        if len(syllables) < len(cols) + 1:
            continue
        groups = _assign_pinyin_groups_to_columns(syllables, cols)
        if len(groups) != len(cols) or not all(groups):
            continue
        parts: list[dict[str, Any]] = []
        for col, group in zip(cols, groups):
            sub = dict(atom)
            sub["content"] = group[:500]
            sub["ocr_text"] = group[:500]
            pad = 0.012
            sub["x_start"] = round(
                max(float(atom["x_start"]), float(col["x_start"]) - pad), 4
            )
            sub["x_end"] = round(
                min(float(atom["x_end"]), float(col["x_end"]) + pad), 4
            )
            parts.append(sub)
        replacements[id(atom)] = parts

    if not replacements:
        return atoms

    out_texts: list[dict[str, Any]] = []
    for atom in texts:
        repl = replacements.get(id(atom))
        out_texts.extend(repl if repl else [atom])
    out = others + out_texts
    out.sort(key=lambda a: (a.get("page", 0), a["y_start"], a["x_start"]))
    return out


def _find_pinyin_cjk_split_in_text(text: str) -> tuple[str, str] | None:
    """单框内「拼音…汉字…」按首个汉字处切开（OCR/PDF 常合成一行）。"""
    t = (text or "").strip()
    if not t or not _has_cjk(t) or not re.search(r"[a-zA-Z]", t):
        return None
    if "\n" in t:
        head, tail = t.split("\n", 1)
        head, tail = head.strip(), tail.strip()
        # 已是「拼音行 + 汉字行」竖拼，保留为一个原子
        if head and tail and _looks_like_pinyin(head) and _has_cjk(tail):
            if not re.search(r"[a-zA-Z]", tail):
                return None
    for i, ch in enumerate(t):
        if not ("\u4e00" <= ch <= "\u9fff"):
            continue
        left = t[:i].strip()
        right = t[i:].strip()
        if len(left) < 3 or len(right) < 2:
            continue
        if _looks_like_pinyin(left):
            return left, right
    return None


def _estimate_pinyin_cjk_bbox_split(
    left: str, right: str, x0: float, x1: float
) -> tuple[float, float]:
    """估算左右两框边界（页边拼音 + 右侧提示语，中间留空）。"""

    def vis(s: str) -> float:
        w = 0.0
        for ch in s:
            if "\u4e00" <= ch <= "\u9fff":
                w += 1.0
            elif ch.isalpha():
                w += 0.42
            elif ch.isspace():
                w += 0.18
            else:
                w += 0.5
        return w

    lw, rw = vis(left), vis(right)
    width = max(1e-6, x1 - x0)
    if lw + rw <= 0:
        return x0 + width * 0.32, x0 + width * 0.40
    ratio = min(lw / (lw + rw), 0.36)
    left_end = x0 + width * ratio
    right_start = x0 + width * max(ratio + 0.06, 0.38)
    if right_start >= x1 - 0.05:
        right_start = left_end + width * 0.04
    return left_end, right_start


def _split_atom_by_pinyin_cjk_content(atom: dict[str, Any]) -> list[dict[str, Any]]:
    text = _atom_text(atom)
    pair = _find_pinyin_cjk_split_in_text(text)
    if not pair:
        return [atom]
    left_text, right_text = pair
    x0, y0 = float(atom["x_start"]), float(atom["y_start"])
    x1, y1 = float(atom["x_end"]), float(atom["y_end"])
    left_end, right_start = _estimate_pinyin_cjk_bbox_split(left_text, right_text, x0, x1)
    left_atom = dict(atom)
    left_atom["x_end"] = round(left_end, 4)
    left_atom["content"] = left_text[:500]
    left_atom["ocr_text"] = left_text[:500]
    right_atom = dict(atom)
    right_atom["x_start"] = round(right_start, 4)
    right_atom["content"] = right_text[:500]
    right_atom["ocr_text"] = right_text[:500]
    return [left_atom, right_atom]


def split_mixed_pinyin_cjk_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把单框内的「拼音+汉字」拆成两个原子。"""
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") not in ("text", "title"):
            out.append(atom)
            continue
        lines = [ln.strip() for ln in _atom_text(atom).splitlines() if ln.strip()]
        if len(lines) >= 3 and _lines_alternate_pinyin_hanzi(lines):
            out.append(atom)
        else:
            out.extend(_split_atom_by_pinyin_cjk_content(atom))
    return out


def bbox_to_unit(
    x0: float, y0: float, x1: float, y1: float, pw: float, ph: float
) -> tuple[float, float, float, float]:
    if pw <= 0 or ph <= 0:
        return 0.0, 0.0, 1.0, 1.0
    xs = max(0.0, min(1.0, x0 / pw))
    xe = max(0.0, min(1.0, x1 / pw))
    ys = max(0.0, min(1.0, y0 / ph))
    ye = max(0.0, min(1.0, y1 / ph))
    if xe <= xs:
        xe = min(1.0, xs + 0.002)
    if ye <= ys:
        ye = min(1.0, ys + 0.002)
    return xs, ys, xe, ye


def _span_items_from_line(line: dict[str, Any]) -> list[tuple[float, float, float, float, str]]:
    items: list[tuple[float, float, float, float, str]] = []
    for sp in line.get("spans") or []:
        t = (sp.get("text") or "").strip()
        if not t:
            continue
        bb = sp.get("bbox")
        if not bb or len(bb) < 4:
            continue
        x0, y0, x1, y1 = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
        items.append((x0, y0, x1, y1, t))
    return items


def _merge_span_cluster(
    cluster: list[tuple[float, float, float, float, str]],
) -> tuple[float, float, float, float, str]:
    xs = [c[0] for c in cluster]
    ys = [c[1] for c in cluster]
    xe = [c[2] for c in cluster]
    ye = [c[3] for c in cluster]
    text = "".join(c[4] for c in cluster).strip()
    return min(xs), min(ys), max(xe), max(ye), text


def _should_force_split_text_pair(
    left_text: str, right_text: str, gap_norm: float
) -> bool:
    """横向上相邻两段文本是否应拆成两个原子。"""
    lt = (left_text or "").strip()
    rt = (right_text or "").strip()
    if not lt or not rt:
        return False
    # 页边拼音 + 右侧汉字：即使 PDF/OCR 框重叠（gap≤0）也必须拆开
    if _looks_like_pinyin(lt) and _has_cjk(rt):
        return True
    if _has_cjk(lt) and _looks_like_pinyin(rt):
        return True
    # 活动条上多组拼音标签（推抽屉 / 铅球 / 弹簧）不应并成一行
    if _looks_like_pinyin(lt) and _looks_like_pinyin(rt):
        if gap_norm >= 0.025:
            return True
    if (
        len(_pinyin_syllables(lt)) >= 2
        and len(_pinyin_syllables(rt)) >= 2
        and gap_norm >= 0.012
    ):
        return True
    # 活动条上多个短词标签
    if _looks_like_vocab_label(lt) and _looks_like_vocab_label(rt) and gap_norm >= 0.035:
        return True
    # 对话框被插图隔开：「需要每天」…「的铜钱草的资料」
    lp, rp = re.sub(r"\s+", "", lt), re.sub(r"\s+", "", rt)
    if rp.startswith("的") and _has_cjk(lp) and len(lp) <= 6:
        if not lp.endswith(("？", "。", "！", "；", "」")):
            if len(rp) > 1 and rp[1] not in "有是这在那我他她它":
                return True
    if lp.endswith("每天") and rp.startswith("的"):
        return True
    if gap_norm < 0.08:
        return False
    if _looks_like_short_label(lt) and len(rp) > len(lp) + 2:
        return True
    return False


_WRAP_DE_DIALOGUE_RE = re.compile(r"^(.{2,6})(的[\u4e00-\u9fff]{3,}.+)$")


def _try_de_dialogue_wrap_parts(plain: str) -> tuple[str, str] | None:
    m = _WRAP_DE_DIALOGUE_RE.match(plain)
    if not m:
        return None
    left, right = m.group(1), m.group(2)
    if "的" in left or left.endswith(("？", "。", "！", "；")):
        return None
    if not re.fullmatch(r"[\u4e00-\u9fff]+", left):
        return None
    if len(right) > 1 and right[1] in "有是这在那我他她它":
        return None
    if right.startswith(("的形状", "的形")) and len(left) >= 4:
        return None
    return left, right


def _split_atom_bbox_for_wrap(
    atom: dict[str, Any], left: str, right: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    x0, y0, x1, y1 = (
        float(atom["x_start"]),
        float(atom["y_start"]),
        float(atom["x_end"]),
        float(atom["y_end"]),
    )
    width = x1 - x0
    gap_frac = min(0.38, max(0.10, width * 0.28))
    content_w = max(width - gap_frac, width * 0.5)
    lw, rw = max(1, len(left)), max(1, len(right))
    left_end = x0 + content_w * lw / (lw + rw)
    right_start = left_end + gap_frac
    if right_start >= x1 - 0.04:
        return None
    left_atom = dict(atom)
    left_atom["x_start"] = round(x0, 4)
    left_atom["x_end"] = round(left_end, 4)
    right_atom = dict(atom)
    right_atom["x_start"] = round(right_start, 4)
    right_atom["x_end"] = round(x1, 4)
    return left_atom, right_atom


def split_wrapped_dialogue_text_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """宽框内「短左段 + 的…」被插图隔开的对话断句，拆成两个原子。"""
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") not in ("text", "title"):
            out.append(atom)
            continue
        raw = _atom_text(atom)
        w = _atom_width(atom)
        if w < 0.26:
            out.append(atom)
            continue

        pinyin_line: str | None = None
        hanzi_plain: str | None = None
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if "\n" in raw:
            if len(lines) >= 2 and _looks_like_pinyin(lines[0]) and _has_cjk(lines[-1]):
                pinyin_line = lines[0]
                hanzi_plain = re.sub(r"\s+", "", lines[-1])
            elif len(lines) >= 2 and _lines_alternate_pinyin_hanzi(lines):
                out.append(atom)
                continue
            elif len(lines) >= 2 and all(
                _has_cjk(ln) and not _looks_like_pinyin(ln) for ln in lines
            ):
                out.append(atom)
                continue
            else:
                out.append(atom)
                continue
        else:
            hanzi_plain = re.sub(r"\s+", "", raw)

        parts = _try_de_dialogue_wrap_parts(hanzi_plain or "")
        if not parts:
            out.append(atom)
            continue
        left, right = parts
        pair = _split_atom_bbox_for_wrap(atom, left, right)
        if not pair:
            out.append(atom)
            continue
        left_atom, right_atom = pair
        if pinyin_line:
            left_atom["content"] = f"{pinyin_line}\n{left}"[:500]
            left_atom["ocr_text"] = left_atom["content"]
        else:
            left_atom["content"] = left[:500]
            left_atom["ocr_text"] = left[:500]
        right_atom["content"] = right[:500]
        right_atom["ocr_text"] = right[:500]
        out.extend([left_atom, right_atom])
    return out


def _must_not_merge_atoms(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """同一行上两类版式块永不合并（含 bbox 重叠的情况）。"""
    lt = _atom_text(left)
    rt = _atom_text(right)
    if _should_force_split_text_pair(lt, rt, gap_norm=0.0):
        return True
    # 右侧批注绝不并入左侧长正文
    if _looks_like_side_note_text(rt) and float(right.get("x_start", 0)) >= 0.45:
        lp = re.sub(r"\s+", "", lt)
        if len(lp) >= 16 and not _looks_like_side_note_text(lt):
            return True
    if _looks_like_side_note_text(lt) and float(left.get("x_start", 0)) >= 0.45:
        rp = re.sub(r"\s+", "", rt)
        if len(rp) >= 16 and not _looks_like_side_note_text(rt):
            return True
    if not _has_cjk(rt) or _looks_like_pinyin(rt):
        return False
    lx0 = float(left["x_start"])
    rx0 = float(right["x_start"])
    rt_plain = re.sub(r"\s+", "", rt)
    lp_plain = re.sub(r"\s+", "", lt)
    # 右侧短提示语（对话框）不与左侧 caption / 拼音块并格
    if len(rt_plain) <= 14 and rx0 >= 0.40 and rx0 >= lx0 + 0.10:
        if rt_plain.startswith("的") and len(lp_plain) <= 6:
            return False
        if len(lp_plain) <= 8 or (
            lp_plain and lp_plain[-1] in "？。！；，、」"
        ):
            return True
    first_line = lt.split("\n", 1)[0].strip()
    if _looks_like_pinyin(first_line) and "\n" in lt and rx0 > lx0 + 0.12:
        return True
    return False


_SIDE_NOTE_HINTS = (
    "读这段话",
    "我仿佛",
    "我觉得",
    "我听到",
    "我看到",
    "边观察",
    "记录吧",
)


def _looks_like_side_note_text(text: str) -> bool:
    """语文页右侧外挂批注/对话框短句。"""
    plain = re.sub(r"\s+", "", text or "")
    if not plain or len(plain) < 6 or len(plain) > 80:
        return False
    if any(h in plain for h in _SIDE_NOTE_HINTS):
        return True
    if plain.endswith("。") and 10 <= len(plain) <= 60 and plain.startswith(("读", "我", "这")):
        return True
    return False


def _is_side_note_atom(atom: dict[str, Any]) -> bool:
    if atom.get("atom_type") not in ("text", "title"):
        return False
    if float(atom.get("x_start") or 0) < 0.48:
        return False
    return _looks_like_side_note_text(_atom_text(atom))


def split_embedded_side_note_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """正文与右侧批注文案被 OCR 并成一框时拆开。"""
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") not in ("text", "title"):
            out.append(atom)
            continue
        raw = _atom_text(atom)
        if _atom_width(atom) < 0.32 and "\n" not in raw:
            out.append(atom)
            continue
        raw_idx = None
        for h in _SIDE_NOTE_HINTS:
            i = raw.find(h)
            if i >= 10:
                raw_idx = i
                break
        if raw_idx is None:
            out.append(atom)
            continue
        left, right = raw[:raw_idx].strip(), raw[raw_idx:].strip()
        if len(re.sub(r"\s+", "", left)) < 12 or not _looks_like_side_note_text(right):
            out.append(atom)
            continue
        pair = _split_atom_bbox_for_wrap(atom, left, right)
        if not pair:
            x0 = float(atom["x_start"])
            x1 = float(atom["x_end"])
            mid = x0 + (x1 - x0) * 0.62
            left_atom = dict(atom)
            right_atom = dict(atom)
            left_atom["x_end"] = round(mid - 0.01, 4)
            right_atom["x_start"] = round(mid + 0.01, 4)
            pair = (left_atom, right_atom)
        left_atom, right_atom = pair
        left_atom["content"] = left[:500]
        left_atom["ocr_text"] = left[:500]
        right_atom["content"] = right[:500]
        right_atom["ocr_text"] = right[:500]
        right_atom["block_role"] = "side_note"
        if float(right_atom["x_start"]) < 0.50:
            span = max(0.22, float(atom["x_end"]) - 0.55)
            right_atom["x_start"] = round(max(0.55, float(atom["x_end"]) - span), 4)
            right_atom["x_end"] = round(float(atom["x_end"]), 4)
            left_atom["x_end"] = round(float(right_atom["x_start"]) - 0.015, 4)
        out.extend([left_atom, right_atom])
    return out


def clamp_body_away_from_side_notes(
    atoms: list[dict[str, Any]], *, gutter: float = 0.015
) -> list[dict[str, Any]]:
    """收紧侧批注框，并禁止左侧正文 x_end 越过批注左缘。"""
    notes = [a for a in atoms if _is_side_note_atom(a) or a.get("block_role") == "side_note"]
    out: list[dict[str, Any]] = []
    for atom in atoms:
        a = dict(atom)
        if a.get("atom_type") not in ("text", "title"):
            out.append(a)
            continue
        text = _atom_text(a)
        if _is_side_note_atom(a) or a.get("block_role") == "side_note":
            plain_len = len(re.sub(r"\s+", "", text))
            max_w = min(0.42, max(0.20, plain_len * 0.022))
            if _atom_width(a) > max_w * 1.12:
                a["x_end"] = round(float(a["x_start"]) + max_w, 4)
            a["block_role"] = "side_note"
            out.append(a)
            continue
        ay0, ay1 = float(a["y_start"]), float(a["y_end"])
        for n in notes:
            ny0, ny1 = float(n["y_start"]), float(n["y_end"])
            if min(ay1, ny1) - max(ay0, ny0) <= 0:
                continue
            nx0 = float(n["x_start"])
            if float(a["x_start"]) >= nx0 - 0.02:
                continue
            cap = nx0 - gutter
            if float(a["x_end"]) > cap:
                a["x_end"] = round(max(float(a["x_start"]) + 0.12, cap), 4)
        out.append(a)
    return out


def tag_side_note_roles(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for atom in atoms:
        a = dict(atom)
        if _is_side_note_atom(a):
            a["block_role"] = "side_note"
        out.append(a)
    return out


def _split_interview_question_and_home_title(text: str) -> tuple[str, str] | None:
    """采访录问句 + 右栏气泡短标题（如「蚕宝宝的家」）并在同一字符串时拆开。"""
    raw = (text or "").strip()
    if "采访" not in raw and "采访记录" not in raw:
        return None
    # 多行：末行短标题
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) >= 2:
        last = lines[-1]
        last_plain = re.sub(r"\s+", "", last)
        if (
            _looks_like_bubble_opener_line(last)
            or (3 <= len(last_plain) <= 8 and "？" not in last_plain and "问" not in last_plain)
        ):
            left = "\n".join(lines[:-1]).strip()
            if len(re.sub(r"\s+", "", left)) >= 4:
                return left, last
    # 单行：问句？后接短标题
    m = re.search(r"(.+[？?])([\u4e00-\u9fff]{2,8})$", re.sub(r"\s+", "", raw))
    if m:
        # 映射回原始（无空白版已匹配）；用无空白切分再尽量还原
        plain = re.sub(r"\s+", "", raw)
        left_p, right_p = m.group(1), m.group(2)
        if "问" in left_p or "采访" in left_p:
            return left_p, right_p
    # 回退：最后一个「？」后短尾
    plain = re.sub(r"\s+", "", raw)
    qi = max(plain.rfind("？"), plain.rfind("?"))
    if qi >= 4:
        right = plain[qi + 1 :]
        left = plain[: qi + 1]
        if 2 <= len(right) <= 8 and "问" not in right:
            return left, right
    return None


def split_interview_side_bubble_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """采访录宽框内嵌右栏气泡标题时拆成左问句 + 右栏短标题。"""
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") not in ("text", "title"):
            out.append(atom)
            continue
        parts = _split_interview_question_and_home_title(_atom_text(atom))
        if not parts:
            out.append(atom)
            continue
        left, right = parts
        x0, x1 = float(atom["x_start"]), float(atom["x_end"])
        # 右栏落在页右半
        right_start = max(0.45, x0 + (x1 - x0) * 0.55)
        if right_start >= x1 - 0.08:
            right_start = max(0.45, x1 - 0.22)
        left_atom = dict(atom)
        right_atom = dict(atom)
        left_atom["content"] = left[:500]
        left_atom["ocr_text"] = left[:500]
        left_atom["x_start"] = round(x0, 4)
        left_atom["x_end"] = round(max(x0 + 0.12, right_start - 0.02), 4)
        right_atom["content"] = right[:500]
        right_atom["ocr_text"] = right[:500]
        right_atom["x_start"] = round(right_start, 4)
        right_atom["x_end"] = round(x1, 4)
        right_atom["block_role"] = "side_note"
        out.extend([left_atom, right_atom])
    return out


def _lines_alternate_pinyin_hanzi(lines: list[str]) -> bool:
    """多行是否为「拼音、汉字、拼音…」交替（允许末尾单独拼音行）。"""
    if len(lines) < 2:
        return False
    i = 0
    pairs = 0
    while i < len(lines):
        if not _looks_like_pinyin(lines[i]):
            return False
        if i + 1 < len(lines) and _has_cjk(lines[i + 1]):
            pairs += 1
            i += 2
        else:
            i += 1
    return pairs >= 1


def _looks_like_activity_instruction_line(line: str) -> bool:
    t = (line or "").strip()
    plain = re.sub(r"\s+", "", t)
    if not t:
        return False
    if any(k in t for k in ("查阅资料", "向他人请教", "与同学", "交流", "观点", "试一试", "填写")):
        return True
    return False


def _looks_like_bubble_opener_line(line: str) -> bool:
    plain = re.sub(r"\s+", "", line or "")
    if not plain or len(plain) > 14:
        return False
    if _looks_like_activity_instruction_line(line):
        return False
    if plain.endswith(("？", "?", "。", "！", "；")):
        return False
    if plain.endswith(("喜欢", "需要", "可以", "觉得")):
        return True
    return len(plain) <= 10 and _has_cjk(line)


def _looks_like_dialogue_line(line: str) -> bool:
    plain = re.sub(r"\s+", "", line)
    if not plain:
        return False
    if plain.endswith("……") or plain.endswith("…"):
        return True
    if plain.startswith(("向我", "我用力", "我把", "我来", "这是")):
        return True
    if plain.startswith("这") and len(plain) <= 16:
        return True
    return False


def _looks_like_summary_banner_line(line: str) -> bool:
    """绿色活动条/总结句（如「我会描述不同的感受」）。"""
    plain = re.sub(r"\s+", "", line)
    if not plain or len(plain) > 24:
        return False
    if plain.startswith(("我会", "我能", "我可以")):
        return True
    if "不同的感受" in plain or "自己的感受" in plain:
        return True
    return False


def _looks_like_numbered_instruction(line: str) -> bool:
    plain = re.sub(r"\s+", "", line)
    return bool(re.match(r"^\d+[\.．、]", plain))


def _group_multiline_segments(lines: list[str]) -> list[list[str]]:
    """把多行文本收成语义段（拼音+汉字成对，其余单行一段）。"""
    segments: list[list[str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            _looks_like_pinyin(line)
            and i + 1 < len(lines)
            and _has_cjk(lines[i + 1])
            and not _looks_like_pinyin(lines[i + 1])
        ):
            segments.append([line, lines[i + 1]])
            i += 2
            continue
        segments.append([line])
        i += 1
    return segments


def _segment_semantic_kind(segment: list[str]) -> str:
    text = "\n".join(segment)
    hanzi = _last_hanzi_line(text) or segment[-1]
    if _looks_like_summary_banner_line(hanzi) or _looks_like_activity_bar_line(hanzi):
        return "summary"
    if _looks_like_activity_instruction_line(hanzi):
        return "instruction"
    if _looks_like_bubble_opener_line(hanzi):
        return "bubble"
    if _looks_like_dialogue_line(hanzi):
        return "dialogue"
    if _looks_like_numbered_instruction(hanzi):
        return "instruction"
    plain = re.sub(r"\s+", "", hanzi)
    if _has_cjk(hanzi) and plain.endswith("。") and len(plain) <= 14:
        return "instruction_tail"
    return "body"


def _segments_should_split(kind_a: str, kind_b: str) -> bool:
    if kind_a == kind_b:
        return False
    if {kind_a, kind_b} <= {"instruction", "instruction_tail"}:
        return False
    if "instruction" in (kind_a, kind_b) and kind_a != kind_b:
        return True
    if {kind_a, kind_b} <= {"bubble", "dialogue"}:
        return False
    return True


def _looks_like_activity_bar_line(line: str) -> bool:
    plain = re.sub(r"\s+", "", line)
    if len(plain) < 5 or len(plain) > 16:
        return False
    if any(ch in plain for ch in "？。！，、…"):
        return False
    if plain.startswith("说一说") or plain.startswith("想一想"):
        return False
    return True


def _looks_like_vocab_label(text: str) -> bool:
    """活动条上的短词标签（推抽屉 / 铅球 / 弹簧），不应横向并成一格。"""
    plain = re.sub(r"\s+", "", text or "")
    if not plain or not _has_cjk(plain) or _looks_like_pinyin(plain):
        return False
    if len(plain) > 6:
        return False
    if any(ch in plain for ch in "？。！，；、…"):
        return False
    return True


def _should_split_multiline_atom(atom: dict[str, Any], lines: list[str]) -> bool:
    h = _atom_height(atom)
    if len(lines) < 2 or h <= 0.04:
        return False
    segments = _group_multiline_segments(lines)
    if len(segments) >= 2:
        kinds = [_segment_semantic_kind(seg) for seg in segments]
        if any(
            _segments_should_split(kinds[i], kinds[i + 1])
            for i in range(len(kinds) - 1)
        ):
            return True
    if _lines_alternate_pinyin_hanzi(lines):
        return h > 0.042
    cjk_rows = [ln for ln in lines if _has_cjk(ln) and not _looks_like_pinyin(ln)]
    if len(cjk_rows) >= 2:
        return h > 0.042
    return len(lines) >= 3 and h > 0.05


def split_multiline_pinyin_hanzi_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OCR/PDF 把多行打进一个框时，按行拆成子原子再参与配对。"""
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") not in ("text", "title"):
            out.append(atom)
            continue
        lines = [ln.strip() for ln in _atom_text(atom).splitlines() if ln.strip()]
        if not _should_split_multiline_atom(atom, lines):
            out.append(atom)
            continue
        segments = _group_multiline_segments(lines)
        if len(segments) <= 1:
            out.append(atom)
            continue
        y0, y1 = float(atom["y_start"]), float(atom["y_end"])
        total_lines = sum(len(seg) for seg in segments)
        line_h = max((y1 - y0) / max(total_lines, 1), 0.008)
        cursor = y0
        for seg in segments:
            sub = dict(atom)
            sub["content"] = "\n".join(seg)[:500]
            sub["ocr_text"] = sub["content"]
            seg_h = line_h * len(seg)
            sub["y_start"] = round(cursor, 4)
            sub["y_end"] = round(min(y1, cursor + seg_h), 4)
            cursor = sub["y_end"]
            out.append(sub)
    out.sort(key=lambda a: (a.get("page", 0), a["y_start"], a["x_start"]))
    return out


def clamp_tall_single_line_text_bboxes(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """单行文字却竖向过高（蔓延到上下邻行）时收紧框高。"""
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") not in ("text", "title"):
            out.append(atom)
            continue
        lines = [ln.strip() for ln in _atom_text(atom).splitlines() if ln.strip()]
        if len(lines) != 1:
            out.append(atom)
            continue
        h = _atom_height(atom)
        if h <= 0.048:
            out.append(atom)
            continue
        plain = re.sub(r"\s+", "", lines[0])
        if _looks_like_pinyin(lines[0]):
            est_h = min(0.028, max(0.018, len(plain) * 0.0028 + 0.014))
        else:
            est_h = min(0.038, max(0.022, len(plain) * 0.0035 + 0.018))
        if h <= est_h * 1.35:
            out.append(atom)
            continue
        yc = _y_center(atom)
        a = dict(atom)
        a["y_start"] = round(yc - est_h / 2, 4)
        a["y_end"] = round(yc + est_h / 2, 4)
        out.append(a)
    return out


def _is_pinyin_hanzi_block(atom: dict[str, Any]) -> bool:
    text = _atom_text(atom)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return _looks_like_pinyin(lines[0]) and _has_cjk(lines[-1])
    return _looks_like_pinyin(text) and _has_cjk(text)


def _last_hanzi_line(text: str) -> str:
    for ln in reversed([x.strip() for x in text.splitlines() if x.strip()]):
        if _has_cjk(ln):
            return ln
    return ""


def _hanzi_line_looks_complete_for_stack(line: str) -> bool:
    """折行段落：上一行汉字未写完才允许与下一行拼音块纵向合并。"""
    plain = re.sub(r"\s+", "", line)
    if not plain:
        return False
    if plain[-1] in "？。！；」』\"":
        return True
    # 课节标题、活动条等短句单独成块
    if len(plain) <= 14 and "，" not in plain and "、" not in plain:
        return True
    return False


def _should_stack_paragraph_blocks(top: dict[str, Any], bottom: dict[str, Any]) -> bool:
    """上下相邻的两组拼音+汉字是否属于同一段落（仅折行续写，不跨标题/问句/活动条）。"""
    if not _is_directly_below(top, bottom, max_gap=0.055):
        return False
    if _x_overlap_ratio(top, bottom) < 0.45:
        return False
    if _must_not_merge_atoms(top, bottom):
        return False

    top_hanzi = _last_hanzi_line(_atom_text(top))
    if not top_hanzi or _hanzi_line_looks_complete_for_stack(top_hanzi):
        return False
    if _looks_like_activity_bar_line(top_hanzi):
        return False
    if _looks_like_summary_banner_line(top_hanzi):
        return False
    if _looks_like_dialogue_line(top_hanzi):
        return False
    if _looks_like_numbered_instruction(top_hanzi):
        return False

    bt = _atom_text(bottom)
    bot_lines = [ln.strip() for ln in bt.splitlines() if ln.strip()]
    bot_hanzi = _last_hanzi_line(bt)
    if bot_hanzi and _looks_like_dialogue_line(bot_hanzi):
        return False
    if bot_hanzi and _looks_like_summary_banner_line(bot_hanzi):
        return False
    if bot_hanzi and _looks_like_numbered_instruction(bot_hanzi):
        return False
    if bot_lines and _looks_like_pinyin(bot_lines[0]):
        return True
    if _looks_like_pinyin(bt) and top_hanzi:
        return True
    return False


def merge_pinyin_hanzi_paragraph_blocks(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """多组「拼音+汉字」纵向往收为一段落原子。"""
    texts = [a for a in atoms if a.get("atom_type") in ("text", "title")]
    others = [a for a in atoms if a.get("atom_type") not in ("text", "title")]
    if len(texts) <= 1:
        return atoms

    texts.sort(key=lambda a: (float(a["y_start"]), float(a["x_start"])))
    stacked: list[dict[str, Any]] = []
    i = 0
    while i < len(texts):
        cur = dict(texts[i])
        j = i + 1
        while j < len(texts) and _should_stack_paragraph_blocks(cur, texts[j]):
            cur = _merge_vertical_pair(cur, texts[j])
            j += 1
        stacked.append(cur)
        i = j

    out = others + stacked
    out.sort(key=lambda a: (a.get("page", 0), a["y_start"], a["x_start"]))
    return out


def _estimate_single_line_width_norm(text: str) -> float:
    """按字数粗算单行文本合理宽度（归一化）。"""
    t = (text or "").strip()
    if not t:
        return 0.12
    if _looks_like_pinyin(t):
        w = sum(0.016 if ch.isspace() else 0.013 for ch in t)
        return max(0.12, min(0.34, w))
    plain = re.sub(r"\s+", "", t)
    return max(0.08, min(0.55, len(plain) * 0.028))


def refine_atom_layout_bboxes(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """修正 PDF 字层过宽框；同行拼音与汉字框互不侵占。"""
    texts = [a for a in atoms if a.get("atom_type") in ("text", "title")]
    others = [a for a in atoms if a.get("atom_type") not in ("text", "title")]
    if not texts:
        return atoms

    refined: list[dict[str, Any]] = []
    for atom in texts:
        a = dict(atom)
        text = _atom_text(a)
        est_w = _estimate_single_line_width_norm(text)
        cur_w = _atom_width(a)
        if _looks_like_pinyin(text) and cur_w > est_w * 1.25:
            cols_below = _hanzi_columns_below_pinyin(a, texts)
            if len(cols_below) < 2:
                a["x_end"] = round(float(a["x_start"]) + est_w, 4)
        refined.append(a)

    for i, left in enumerate(refined):
        if not _looks_like_pinyin(_atom_text(left)):
            continue
        for right in refined[i + 1 :]:
            if not _has_cjk(_atom_text(right)):
                continue
            if not _same_text_row(left, right):
                continue
            if float(right["x_start"]) <= float(left["x_start"]) + 0.04:
                continue
            cap = float(right["x_start"]) - 0.012
            if float(left["x_end"]) > cap:
                left["x_end"] = round(max(float(left["x_start"]) + 0.08, cap), 4)

    out = others + refined
    out.sort(key=lambda a: (a.get("page", 0), a["y_start"], a["x_start"]))
    return out


def _split_span_items_into_clusters(
    items: list[tuple[float, float, float, float, str]], pw: float
) -> list[list[tuple[float, float, float, float, str]]]:
    if not items:
        return []
    ordered = sorted(items, key=lambda x: x[0])
    gap_limit = max(24.0, pw * 0.06) if pw > 0 else 24.0
    clusters: list[list[tuple[float, float, float, float, str]]] = [[ordered[0]]]
    for item in ordered[1:]:
        prev = clusters[-1][-1]
        gap_px = item[0] - prev[2]
        gap_norm = gap_px / pw if pw > 0 else gap_px
        left_text = "".join(c[4] for c in clusters[-1])
        if gap_px > gap_limit or _should_force_split_text_pair(
            left_text, item[4], gap_norm
        ):
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _text_boxes_from_line(
    line: dict[str, Any], pw: float
) -> list[tuple[float, float, float, float, str]]:
    """同一 PDF line 内按 span 间距拆簇，避免拼音标注与右侧正文并成一格。"""
    items = _span_items_from_line(line)
    if not items:
        return []
    return [_merge_span_cluster(c) for c in _split_span_items_into_clusters(items, pw)]


def extract_atoms_from_fitz_page(page: Any, page_num: int) -> list[dict[str, Any]]:
    """从 PyMuPDF Page 提取：text dict（行级）+ 图块 + 页内图片 xref 矩形。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []

    pw = float(page.rect.width)
    ph = float(page.rect.height)
    atoms: list[dict[str, Any]] = []
    seen_rects: set[tuple[int, int, int, int]] = set()

    def add_atom(
        *,
        atom_type: str,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        content: str,
        suffix: str,
    ) -> None:
        xs, ys, xe, ye = bbox_to_unit(x0, y0, x1, y1, pw, ph)
        key = (int(xs * 10000), int(ys * 10000), int(xe * 10000), int(ye * 10000))
        if key in seen_rects:
            if atom_type in ("text", "image"):
                return
        seen_rects.add(key)
        idx = len(atoms) + 1
        atoms.append(
            {
                "atom_id": f"A{page_num:03d}-{idx:03d}{suffix}",
                "atom_type": atom_type,
                "page": page_num,
                "y_start": round(ys, 4),
                "y_end": round(ye, 4),
                "x_start": round(xs, 4),
                "x_end": round(xe, 4),
                "content": content[:500],
                "ocr_text": content[:500],
                "parent_block_id": None,
                "bound_cw_pgs": [],
                "is_locked": False,
            }
        )

    td = page.get_text("dict") or {}
    for block in td.get("blocks") or []:
        btype = block.get("type", 0)
        if btype == 0:
            for line in block.get("lines") or []:
                for x0, y0, x1, y1, text in _text_boxes_from_line(line, pw):
                    if not text:
                        continue
                    if len(text) == 1 and text.isspace():
                        continue
                    is_title = y1 < ph * 0.12 and len(text) < 40
                    add_atom(
                        atom_type="title" if is_title else "text",
                        x0=x0,
                        y0=y0,
                        x1=x1,
                        y1=y1,
                        content=text,
                        suffix="",
                    )
        elif btype == 1:
            bb = block.get("bbox")
            if bb and len(bb) >= 4:
                x0, y0, x1, y1 = (
                    float(bb[0]),
                    float(bb[1]),
                    float(bb[2]),
                    float(bb[3]),
                )
                if (x1 - x0) < 12 or (y1 - y0) < 12:
                    continue
                add_atom(
                    atom_type="image",
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    content="[插图]",
                    suffix="-img",
                )

    # 页内绘制的图片实例（补充 dict 未列出的 xref）
    try:
        for img in page.get_images(full=True) or []:
            xref = int(img[0])
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            for r in rects or []:
                if hasattr(r, "x0"):
                    x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
                else:
                    bb = getattr(r, "bbox", None) or (0, 0, 0, 0)
                    x0, y0, x1, y1 = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
                if (x1 - x0) < 8 or (y1 - y0) < 8:
                    continue
                add_atom(
                    atom_type="image",
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    content=f"[图片 xref:{xref}]",
                    suffix="-xref",
                )
    except Exception:
        pass

    atoms.sort(key=lambda a: (a["page"], a["y_start"], a["x_start"]))
    return dedup_overlapping_image_atoms(atoms)


def _y_center(atom: dict[str, Any]) -> float:
    return (float(atom["y_start"]) + float(atom["y_end"])) / 2.0


def _atom_height(atom: dict[str, Any]) -> float:
    return max(1e-6, float(atom["y_end"]) - float(atom["y_start"]))


def _same_text_row(a: dict[str, Any], b: dict[str, Any], *, y_slack: float = 0.018) -> bool:
    """两文本框是否在同一视觉行（PDF 常把每字拆成独立 line）。"""
    tol = max(y_slack, 0.55 * min(_atom_height(a), _atom_height(b)))
    return abs(_y_center(a) - _y_center(b)) <= tol


def _row_x_gap_limit(row_atoms: list[dict[str, Any]]) -> float:
    """估算同行碎片可合并的最大横向间距（对话框内偏小，双栏/标签+正文偏大则拆开）。"""
    if len(row_atoms) < 2:
        return 0.14
    ordered = sorted(row_atoms, key=lambda a: float(a["x_start"]))
    gaps: list[float] = []
    for i in range(1, len(ordered)):
        gap = float(ordered[i]["x_start"]) - float(ordered[i - 1]["x_end"])
        if gap > 0:
            gaps.append(gap)
    if not gaps:
        return 0.14
    gaps.sort()
    if len(gaps) >= 2 and gaps[-1] > gaps[-2] * 2.2:
        return max(0.10, min(0.14, gaps[-2] * 1.15))
    return max(0.12, min(0.14, gaps[-1] * 1.15))


def _looks_like_short_label(text: str) -> bool:
    t = re.sub(r"\s+", "", (text or ""))
    return 1 <= len(t) <= 8


def _should_force_split_horizontal(left: dict[str, Any], right: dict[str, Any], gap: float) -> bool:
    """左侧拼音/短标签 + 右侧正文不应并成一格。"""
    lt = (left.get("content") or left.get("ocr_text") or "").strip()
    rt = (right.get("content") or right.get("ocr_text") or "").strip()
    return _should_force_split_text_pair(lt, rt, gap)


def _ends_sentence_fragment(text: str) -> bool:
    plain = re.sub(r"\s+", "", text or "")
    return bool(plain) and plain[-1] in "？。！；，、」"


def _starts_new_phrase_after_gap(text: str) -> bool:
    plain = re.sub(r"\s+", "", text or "")
    if not plain:
        return False
    if plain[0] in "这那我你他是":
        return True
    if plain.startswith("的") and len(plain) > 1:
        if plain[1] in "有是这在那我他她它":
            return False
        if len(plain) >= 4:
            return True
    return False


def _is_single_cjk_orphan(atom: dict[str, Any]) -> bool:
    plain = re.sub(r"\s+", "", _atom_text(atom))
    return len(plain) == 1 and "\u4e00" <= plain <= "\u9fff"


def images_on_same_row_between(
    left: dict[str, Any],
    right: dict[str, Any],
    images: list[dict[str, Any]],
    *,
    y_slack: float = 0.038,
) -> bool:
    """左右文本之间（横向空隙内）是否有插图块。"""
    ly = _y_center(left)
    gap_x0 = float(left["x_end"])
    gap_x1 = float(right["x_start"])
    if gap_x1 <= gap_x0 + 0.015:
        return False
    for img in images:
        if img.get("atom_type") != "image":
            continue
        if abs(_y_center(img) - ly) > y_slack:
            continue
        ix0, ix1 = float(img["x_start"]), float(img["x_end"])
        if ix0 < gap_x1 and ix1 > gap_x0:
            return True
    return False


def looks_like_sentence_wrap_continuation(
    left: dict[str, Any], right: dict[str, Any]
) -> bool:
    """正文句被 inline 插图隔开时，左右 OCR 碎段是否仍属同一句（非对话气泡）。"""
    lt = re.sub(r"\s+", "", _atom_text(left))
    rt = re.sub(r"\s+", "", _atom_text(right))
    if not lt or not rt or not _has_cjk(lt) or not _has_cjk(rt):
        return False
    if _looks_like_pinyin(lt) or _looks_like_pinyin(rt):
        return False
    if lt.endswith(("。", "！", "？", "；")):
        return False
    # 对话绕排：仅极短左段 + 明显对话语气时才拆开（勿误伤正文「我们周围」+「的事物…」）
    if rt.startswith("的") and len(lt) <= 6 and not lt.endswith(("？", "。", "！", "；", "，")):
        if lt.endswith("每天") and rt.startswith("的"):
            return False
        if _looks_like_dialogue_line(lt) or _looks_like_dialogue_line(rt):
            return False
        if len(lt) <= 4 and lt[-1] in "我你他她它":
            return False
    if lt.endswith("每天") and rt.startswith("的"):
        return False
    if _looks_like_dialogue_line(lt) or _looks_like_dialogue_line(rt):
        return False
    continuation_starters = "的了中国这那与和及以而将把被让给从向在是有也都要会能可"
    if rt[0] in continuation_starters and len(lt) <= 36:
        return True
    if lt.endswith(("，", "、", "：", "；", "…", "……")):
        return True
    if len(lt) <= 10:
        return True
    if len(lt) <= 18 and not lt.endswith(("。", "！", "？")):
        return True
    return False


def _rapidocr_plain_text_on_bgr(bgr: Any) -> str:
    """对裁剪条带重新 OCR，按 x 序拼接为整行文本。"""
    import tempfile

    try:
        import cv2  # type: ignore
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return ""

    td = Path(tempfile.mkdtemp(prefix="tbocr_strip_"))
    tp = td / "strip.png"
    try:
        cv2.imwrite(str(tp), bgr)
        ocr = RapidOCR()
        result, _ = ocr(str(tp))
        if not result:
            return ""
        parts: list[tuple[float, str]] = []
        for it in result:
            if not it or len(it) < 2:
                continue
            box, text = it[0], (it[1] or "").strip()
            if not text:
                continue
            try:
                xs = [float(p[0]) for p in box]
                xc = sum(xs) / len(xs)
            except (TypeError, ValueError, IndexError):
                xc = 0.0
            parts.append((xc, text))
        parts.sort(key=lambda x: x[0])
        return "".join(t for _, t in parts)
    except Exception:
        return ""
    finally:
        tp.unlink(missing_ok=True)
        try:
            td.rmdir()
        except OSError:
            pass


def _refill_merged_atom_text_from_strip(
    merged: dict[str, Any], page_bgr: Any
) -> dict[str, Any]:
    """绕排合并后对整行条带重 OCR，补全插图上方/中间漏字。"""
    if page_bgr is None:
        return merged
    h, w = page_bgr.shape[:2]
    pad_y = max(4, int(h * 0.006))
    x0 = max(0, int(float(merged["x_start"]) * w))
    y0 = max(0, int(float(merged["y_start"]) * h) - pad_y)
    x1 = min(w, int(float(merged["x_end"]) * w))
    y1 = min(h, int(float(merged["y_end"]) * h) + pad_y)
    if x1 - x0 < 12 or y1 - y0 < 8:
        return merged
    crop = page_bgr[y0:y1, x0:x1]
    if crop is None or crop.size == 0:
        return merged
    refill = _rapidocr_plain_text_on_bgr(crop)
    old = re.sub(r"\s+", "", _atom_text(merged))
    new = re.sub(r"\s+", "", refill)
    if new and len(new) >= max(len(old), 4) * 0.75:
        out = dict(merged)
        out["content"] = refill[:500]
        out["ocr_text"] = refill[:500]
        return out
    return merged


def _should_split_row_cluster_at_gap(
    left: dict[str, Any],
    right: dict[str, Any],
    gap: float,
    gap_limit: float,
    *,
    images: list[dict[str, Any]] | None = None,
) -> bool:
    """是否在同一行上将左右两段拆成两个簇（双栏/对话断句）。"""
    lt, rt = _atom_text(left), _atom_text(right)
    if _should_force_split_text_pair(lt, rt, gap):
        return True
    if _should_force_split_horizontal(left, right, gap) or _must_not_merge_atoms(left, right):
        return True
    if gap <= gap_limit:
        return False
    if _ends_sentence_fragment(_atom_text(left)):
        return True
    if _starts_new_phrase_after_gap(_atom_text(right)) and gap >= 0.06:
        return True
    if gap > 0.22:
        imgs = images or []
        if imgs and images_on_same_row_between(left, right, imgs):
            if looks_like_sentence_wrap_continuation(left, right):
                return False
        return True
    return False


def _split_row_clusters_by_x_gap(
    row_atoms: list[dict[str, Any]],
    *,
    max_gap: float | None = None,
    images: list[dict[str, Any]] | None = None,
) -> list[list[dict[str, Any]]]:
    """同一行内横向上相距过远的片段拆开（双栏等）。"""
    if not row_atoms:
        return []
    gap_limit = max_gap if max_gap is not None else _row_x_gap_limit(row_atoms)
    ordered = sorted(row_atoms, key=lambda a: float(a["x_start"]))
    clusters: list[list[dict[str, Any]]] = [[ordered[0]]]
    for atom in ordered[1:]:
        if _is_single_cjk_orphan(atom):
            clusters[-1].append(atom)
            continue
        prev = clusters[-1][-1]
        gap = float(atom["x_start"]) - float(prev["x_end"])
        if _should_split_row_cluster_at_gap(
            prev, atom, gap, gap_limit, images=images
        ):
            clusters.append([atom])
        else:
            clusters[-1].append(atom)
    return clusters


def merge_text_fragments_across_inline_images(
    atoms: list[dict[str, Any]],
    *,
    page_bgr: Any | None = None,
) -> list[dict[str, Any]]:
    """正文句绕排/同行大间距时合并 OCR 碎段；有插图时也可绕插图合并。"""
    images = [a for a in atoms if a.get("atom_type") == "image"]

    changed = True
    while changed:
        changed = False
        texts = sorted(
            [a for a in atoms if a.get("atom_type") in ("text", "title")],
            key=lambda a: (_y_center(a), float(a["x_start"])),
        )
        if len(texts) < 2:
            break
        others = [a for a in atoms if a.get("atom_type") not in ("text", "title")]
        for i in range(len(texts) - 1):
            left, right = texts[i], texts[i + 1]
            if not _same_text_row(left, right, y_slack=0.038):
                continue
            gap = float(right["x_start"]) - float(left["x_end"])
            if gap < 0.04:
                continue
            should_merge = False
            if gap <= 0.22 and not _should_split_row_cluster_at_gap(
                left, right, gap, gap_limit=0.14, images=images
            ):
                should_merge = True
            elif images_on_same_row_between(left, right, images) and (
                looks_like_sentence_wrap_continuation(left, right)
            ):
                should_merge = True
            elif gap >= 0.10 and looks_like_sentence_wrap_continuation(left, right):
                should_merge = True
            if not should_merge:
                continue
            if _must_not_merge_atoms(left, right):
                rt = re.sub(r"\s+", "", _atom_text(right))
                # 仅放行「我们周围」+「的事物…」类 de-续句，不放行短标签+右栏
                if not (
                    rt.startswith("的")
                    and looks_like_sentence_wrap_continuation(left, right)
                ):
                    continue
            merged = _merge_text_row_group([left, right])
            merged = _refill_merged_atom_text_from_strip(merged, page_bgr)
            texts = texts[:i] + [merged] + texts[i + 2 :]
            atoms = others + texts
            changed = True
            break

    out = list(atoms)
    out.sort(key=lambda a: (a.get("page", 0), a["y_start"], a["x_start"]))
    return out


def _merge_text_row_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(group, key=lambda a: float(a["x_start"]))
    base = dict(ordered[0])
    xs = [float(a["x_start"]) for a in ordered]
    ys = [float(a["y_start"]) for a in ordered]
    xe = [float(a["x_end"]) for a in ordered]
    ye = [float(a["y_end"]) for a in ordered]
    parts = [(a.get("content") or a.get("ocr_text") or "").strip() for a in ordered]
    text = "".join(p for p in parts if p)
    types = {a.get("atom_type") for a in ordered}
    base["x_start"] = round(min(xs), 4)
    base["y_start"] = round(min(ys), 4)
    base["x_end"] = round(max(xe), 4)
    base["y_end"] = round(max(ye), 4)
    base["content"] = text[:500]
    base["ocr_text"] = text[:500]
    base["atom_type"] = "title" if "title" in types else "text"
    return base


def merge_text_atoms_by_row(
    atoms: list[dict[str, Any]], *, y_slack: float = 0.018, max_x_gap: float | None = None
) -> list[dict[str, Any]]:
    """把同一视觉行上的 text/title 原子合并为一框（修复一字一框）。"""
    texts = [a for a in atoms if a.get("atom_type") in ("text", "title")]
    others = [a for a in atoms if a.get("atom_type") not in ("text", "title")]
    if len(texts) <= 1:
        return atoms

    texts.sort(key=lambda a: (_y_center(a), float(a["x_start"])))
    row_groups: list[list[dict[str, Any]]] = []
    for atom in texts:
        placed = False
        for group in row_groups:
            if _same_text_row(group[0], atom, y_slack=y_slack):
                group.append(atom)
                placed = True
                break
        if not placed:
            row_groups.append([atom])

    images = [a for a in atoms if a.get("atom_type") == "image"]
    merged_texts: list[dict[str, Any]] = []
    for group in row_groups:
        for cluster in _split_row_clusters_by_x_gap(
            group, max_gap=max_x_gap, images=images
        ):
            if len(cluster) == 1:
                merged_texts.append(cluster[0])
            elif any(
                _must_not_merge_atoms(cluster[i], cluster[i + 1])
                or _should_force_split_text_pair(
                    _atom_text(cluster[i]),
                    _atom_text(cluster[i + 1]),
                    float(cluster[i + 1]["x_start"]) - float(cluster[i]["x_end"]),
                )
                for i in range(len(cluster) - 1)
            ):
                merged_texts.extend(cluster)
            else:
                merged_texts.append(_merge_text_row_group(cluster))

    out = others + merged_texts
    out.sort(key=lambda a: (a.get("page", 0), a["y_start"], a["x_start"]))
    return out


def _is_pinyin_only_atom(atom: dict[str, Any]) -> bool:
    lines = [ln.strip() for ln in _atom_text(atom).splitlines() if ln.strip()]
    return bool(lines) and all(_looks_like_pinyin(ln) for ln in lines)


def _can_pair_pinyin_with_hanzi(
    upper: dict[str, Any],
    lower: dict[str, Any],
    *,
    min_x_overlap: float = 0.35,
    max_vertical_gap: float = 0.09,
) -> bool:
    """拼音行与下方汉字配对（含 OCR 框过高盖住汉字行的情况）。"""
    upper_text = _atom_text(upper)
    lower_text = _atom_text(lower)
    if not _looks_like_pinyin(upper_text):
        return False
    if not _has_cjk(lower_text) or _looks_like_pinyin(lower_text):
        return False
    if float(lower["x_start"]) > float(upper["x_start"]) + 0.20:
        return False
    xo = _x_overlap_ratio(upper, lower)
    if xo < min_x_overlap:
        return False
    if (
        _same_text_row(upper, lower)
        and not (
            _looks_like_pinyin(upper_text)
            and float(_y_center(lower)) > float(_y_center(upper)) + 0.002
        )
    ):
        return False
    if _is_directly_below(upper, lower, max_gap=max_vertical_gap):
        return True
    if _is_pinyin_only_atom(upper):
        uy0, uy1 = float(upper["y_start"]), float(upper["y_end"])
        ly0, ly1 = float(lower["y_start"]), float(lower["y_end"])
        # OCR 拼音框偏高，汉字行紧贴其下缘或略被框住
        if ly0 >= uy0 + (uy1 - uy0) * 0.28 and ly0 <= uy1 + max_vertical_gap + 0.012:
            return True
        if ly0 >= uy0 and ly1 <= uy1 + max_vertical_gap + 0.012:
            return True
    return False


def _x_overlap_ratio(a: dict[str, Any], b: dict[str, Any]) -> float:
    ax0, ax1 = float(a["x_start"]), float(a["x_end"])
    bx0, bx1 = float(b["x_start"]), float(b["x_end"])
    inter = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    narrower = min(ax1 - ax0, bx1 - bx0)
    return inter / max(narrower, 1e-9)


def _is_directly_below(
    upper: dict[str, Any], lower: dict[str, Any], *, max_gap: float = 0.09
) -> bool:
    if _y_center(lower) <= _y_center(upper):
        return False
    gap = float(lower["y_start"]) - float(upper["y_end"])
    return -0.008 <= gap <= max_gap


def _merge_vertical_pair(top: dict[str, Any], bottom: dict[str, Any]) -> dict[str, Any]:
    merged = dict(bottom)
    py = (top.get("content") or top.get("ocr_text") or "").strip()
    hz = (bottom.get("content") or bottom.get("ocr_text") or "").strip()
    if _looks_like_pinyin(py) and _has_cjk(hz):
        hz_plain = re.sub(r"\s+", "", hz)
        syllables = _pinyin_syllables(py)
        top_w = float(top["x_end"]) - float(top["x_start"])
        bot_w = float(bottom["x_end"]) - float(bottom["x_start"])
        if (
            len(syllables) > len(hz_plain) > 0
            and len(hz_plain) <= 6
            and _looks_like_vocab_label(hz)
        ):
            py = " ".join(syllables[: len(hz_plain)])
        if top_w > bot_w + 0.05 and _looks_like_vocab_label(hz):
            pad = 0.012
            merged["x_start"] = round(float(bottom["x_start"]) - pad, 4)
            merged["x_end"] = round(float(bottom["x_end"]) + pad, 4)
            merged["y_start"] = round(min(float(top["y_start"]), float(bottom["y_start"])), 4)
            merged["y_end"] = round(max(float(top["y_end"]), float(bottom["y_end"])), 4)
            merged["content"] = f"{py}\n{hz}"[:500]
            merged["ocr_text"] = merged["content"]
            if bottom.get("atom_type") == "title" or top.get("atom_type") == "title":
                merged["atom_type"] = "title"
            return merged
    merged["x_start"] = round(min(float(top["x_start"]), float(bottom["x_start"])), 4)
    merged["y_start"] = round(min(float(top["y_start"]), float(bottom["y_start"])), 4)
    merged["x_end"] = round(max(float(top["x_end"]), float(bottom["x_end"])), 4)
    merged["y_end"] = round(max(float(top["y_end"]), float(bottom["y_end"])), 4)
    merged["content"] = f"{py}\n{hz}"[:500]
    merged["ocr_text"] = merged["content"]
    if bottom.get("atom_type") == "title" or top.get("atom_type") == "title":
        merged["atom_type"] = "title"
    return merged


def merge_pinyin_with_hanzi_below(
    atoms: list[dict[str, Any]],
    *,
    min_x_overlap: float = 0.35,
    max_vertical_gap: float = 0.09,
) -> list[dict[str, Any]]:
    """拼音行 + 正下方汉字行合并为一个原子（全局按竖向间距最近配对，避免抢字）。"""
    texts = [a for a in atoms if a.get("atom_type") in ("text", "title")]
    others = [a for a in atoms if a.get("atom_type") not in ("text", "title")]
    if len(texts) <= 1:
        return atoms

    candidates: list[tuple[float, float, int, int]] = []
    for i, upper in enumerate(texts):
        upper_text = upper.get("content") or upper.get("ocr_text") or ""
        if not _looks_like_pinyin(upper_text):
            continue
        for j, lower in enumerate(texts):
            if i == j:
                continue
            lower_text = lower.get("content") or lower.get("ocr_text") or ""
            if not _has_cjk(lower_text):
                continue
            if _is_pinyin_hanzi_block(lower):
                continue
            if not _can_pair_pinyin_with_hanzi(
                upper,
                lower,
                min_x_overlap=min_x_overlap,
                max_vertical_gap=max_vertical_gap,
            ):
                continue
            gap = float(lower["y_start"]) - float(upper["y_end"])
            xo = _x_overlap_ratio(upper, lower)
            candidates.append((gap, -xo, i, j))

    candidates.sort(key=lambda t: (t[0], t[1]))
    used = [False] * len(texts)
    merged_texts: list[dict[str, Any]] = []
    for _gap, _nxo, i, j in candidates:
        if used[i] or used[j]:
            continue
        merged_texts.append(_merge_vertical_pair(texts[i], texts[j]))
        used[i] = used[j] = True

    for i, atom in enumerate(texts):
        if not used[i]:
            merged_texts.append(atom)

    out = others + merged_texts
    out.sort(key=lambda a: (a.get("page", 0), a["y_start"], a["x_start"]))
    return out


def _atom_area(atom: dict[str, Any]) -> float:
    return max(
        1e-9,
        (float(atom["x_end"]) - float(atom["x_start"]))
        * (float(atom["y_end"]) - float(atom["y_start"])),
    )


def _atom_width(atom: dict[str, Any]) -> float:
    return max(1e-9, float(atom["x_end"]) - float(atom["x_start"]))


def _is_nested_image_wrapper_pair(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    """插图里外双层框：删外包、留内层紧框（非主图+角标 inset）。"""
    if outer.get("atom_type") != "image" or inner.get("atom_type") != "image":
        return False
    ao, ai = _atom_area(outer), _atom_area(inner)
    if ao <= ai * 1.03:
        return False
    if _containment_ratio(inner, outer) < 0.68:
        return False
    if ao / max(ai, 1e-9) > 3.0:
        return False
    if _iou(outer, inner) < 0.10:
        return False
    return True


def _is_overlapping_image_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """两插图框高度重合（PDF dict 图块 + xref 双份，或里外双层框）。"""
    if a.get("atom_type") != "image" or b.get("atom_type") != "image":
        return False
    if _iou(a, b) >= 0.52:
        return True
    cr_ab = _containment_ratio(a, b)
    cr_ba = _containment_ratio(b, a)
    if cr_ab >= 0.82 and cr_ba >= 0.82:
        return True
    aa, ab = _atom_area(a), _atom_area(b)
    outer, inner = (a, b) if aa >= ab else (b, a)
    if _containment_ratio(inner, outer) >= 0.65 and _iou(a, b) >= 0.35:
        return True
    return _is_nested_image_wrapper_pair(outer, inner)


def _duplicate_image_drop_index(a: dict[str, Any], b: dict[str, Any]) -> int:
    """重叠插图对中应丢弃的下标（0=a, 1=b），保留更紧的内层框。"""
    aa, ab = _atom_area(a), _atom_area(b)
    if aa > ab * 1.03 and _containment_ratio(b, a) >= 0.65:
        return 0
    if ab > aa * 1.03 and _containment_ratio(a, b) >= 0.65:
        return 1
    aid = str(a.get("atom_id") or a.get("atom_code") or "")
    bid = str(b.get("atom_id") or b.get("atom_code") or "")
    if "-xref" in aid and "-xref" not in bid:
        return 0
    if "-xref" in bid and "-xref" not in aid:
        return 1
    if aa > ab * 1.02 and _containment_ratio(b, a) >= 0.75:
        return 0
    if ab > aa * 1.02 and _containment_ratio(a, b) >= 0.75:
        return 1
    sa = re.search(r"-(\d+)", aid)
    sb = re.search(r"-(\d+)", bid)
    na = int(sa.group(1)) if sa else 0
    nb = int(sb.group(1)) if sb else 0
    return 0 if na > nb else 1


def dedup_overlapping_image_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉高度重合的重复插图框（dict 图块 + xref 双份、里外双层框）。"""
    images = [i for i, a in enumerate(atoms) if a.get("atom_type") == "image"]
    remove: set[int] = set()
    for i in images:
        if i in remove:
            continue
        for j in images:
            if i >= j or j in remove:
                continue
            a, b = atoms[i], atoms[j]
            if not _is_overlapping_image_duplicate(a, b):
                continue
            drop = _duplicate_image_drop_index(a, b)
            remove.add(i if drop == 0 else j)
    return [a for k, a in enumerate(atoms) if k not in remove]


def drop_mixed_content_wrapper_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """删掉同时包住多个 image 子原子的印刷边框外包框。

    只删满足以下全部条件的 image 原子：
    1. 内部有 ≥2 个 image 子原子（纯文字子原子不触发）
    2. 面积较小（< 0.25），避免误删正常大插图
    这样可以去掉教材印刷边框，同时保护风筝图等正常插图。
    """
    images = [i for i, a in enumerate(atoms) if a.get("atom_type") == "image"]
    remove: set[int] = set()
    for i in images:
        if i in remove:
            continue
        outer = atoms[i]
        o_area = _atom_area(outer)
        # 正常插图面积通常 > 0.05，印刷边框面积通常也不会超过 0.25
        # 但为安全起见，不限制面积，改为只看子原子类型
        image_children = [
            j for j, a in enumerate(atoms)
            if j != i
            and j not in remove
            and a.get("atom_type") == "image"
            and _containment_ratio(a, outer) >= 0.60
            and _atom_area(a) <= o_area * 0.80
        ]
        if len(image_children) >= 2:
            remove.add(i)
    return [a for k, a in enumerate(atoms) if k not in remove]


def _containment_ratio(inner: dict[str, Any], outer: dict[str, Any]) -> float:
    """inner 面积中有多少比例落在 outer 内。"""
    try:
        ix0 = max(float(inner["x_start"]), float(outer["x_start"]))
        iy0 = max(float(inner["y_start"]), float(outer["y_start"]))
        ix1 = min(float(inner["x_end"]), float(outer["x_end"]))
        iy1 = min(float(inner["y_end"]), float(outer["y_end"]))
    except (KeyError, TypeError, ValueError):
        return 0.0
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    area = _atom_area(inner)
    if area <= 1e-12:
        return 0.0
    return (iw * ih) / area


def _is_gap_placeholder(atom: dict[str, Any]) -> bool:
    t = (atom.get("content") or "").strip()
    return t.startswith("[未拆分") or t.startswith("[整页未识别")


def _is_real_text_atom(atom: dict[str, Any]) -> bool:
    if atom.get("atom_type") not in ("text", "title"):
        return False
    return not _is_gap_placeholder(atom)


def dedup_overlapping_text_atoms(
    atoms: list[dict[str, Any]], *, iou_threshold: float = 0.38
) -> list[dict[str, Any]]:
    """重叠文本框只保留更大、内容更完整的一个（去掉字层+OCR 双份）。"""
    others = [a for a in atoms if not _is_real_text_atom(a)]
    texts = [a for a in atoms if _is_real_text_atom(a)]
    if len(texts) <= 1:
        return atoms

    texts.sort(key=lambda a: (-_atom_area(a), -len(a.get("content") or "")))
    kept: list[dict[str, Any]] = []
    for atom in texts:
        if any(
            (
                _iou(atom, k) >= iou_threshold
                or (
                    _atom_area(atom) < _atom_area(k)
                    and _containment_ratio(atom, k) > 0.7
                )
            )
            and not _must_not_merge_atoms(k, atom)
            and not (_is_pinyin_only_atom(k) and _has_cjk(_atom_text(atom)))
            for k in kept
        ):
            continue
        kept.append(atom)
    out = others + kept
    out.sort(key=lambda a: (a.get("page", 0), a["y_start"], a["x_start"]))
    return out


def drop_text_spanning_wrapper_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """删竖向蔓延大框：内侧已有独立行级原子时去掉外包。"""
    idx_texts = [i for i, a in enumerate(atoms) if _is_real_text_atom(a)]
    remove: set[int] = set()
    for i in idx_texts:
        if i in remove:
            continue
        outer = atoms[i]
        oh = _atom_height(outer)
        if oh < 0.09:
            continue
        outer_plain = re.sub(r"\s+", "", _atom_text(outer))
        children: list[int] = []
        for j in idx_texts:
            if i == j or j in remove:
                continue
            inner = atoms[j]
            if _containment_ratio(inner, outer) < 0.55:
                continue
            if _atom_height(inner) >= oh * 0.68:
                continue
            children.append(j)
        if len(children) >= 2:
            remove.add(i)
            continue
        if len(children) == 1:
            inner_plain = re.sub(r"\s+", "", _atom_text(atoms[children[0]]))
            if (
                len(inner_plain) >= 3
                and inner_plain in outer_plain
                and oh > _atom_height(atoms[children[0]]) * 1.35
            ):
                remove.add(i)
    return [a for k, a in enumerate(atoms) if k not in remove]


def drop_atoms_contained_in_larger(
    atoms: list[dict[str, Any]], *, min_ratio: float = 0.82
) -> list[dict[str, Any]]:
    """去掉被更大文本框几乎完全包住的小框（合并后不留子原子）。"""
    idx_texts = [i for i, a in enumerate(atoms) if _is_real_text_atom(a)]
    remove: set[int] = set()
    for i in idx_texts:
        if i in remove:
            continue
        for j in idx_texts:
            if i == j or j in remove:
                continue
            ai, aj = atoms[i], atoms[j]
            if _atom_area(ai) >= _atom_area(aj):
                continue
            if _containment_ratio(ai, aj) >= min_ratio:
                if _must_not_merge_atoms(aj, ai):
                    continue
                remove.add(i)
    return [a for k, a in enumerate(atoms) if k not in remove]


def drop_tiny_noise_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉页边装饰图块碎片、OCR 误识别的极小噪点框。"""
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if _is_gap_placeholder(atom):
            out.append(atom)
            continue
        area = _atom_area(atom)
        w, h = _atom_width(atom), _atom_height(atom)
        if atom.get("atom_type") == "image":
            # PDF 页边重复装饰点/线常被拆成大量极小 image 块
            if area < 0.0002 or w < 0.012 or h < 0.012:
                continue
        elif atom.get("atom_type") in ("text", "title"):
            text = (atom.get("content") or atom.get("ocr_text") or "").strip()
            plain = re.sub(r"\s+", "", text)
            if area < 0.00008 and len(plain) <= 2:
                continue
            if w < 0.01 and h < 0.01 and len(plain) <= 1:
                continue
        out.append(atom)
    return out


def drop_empty_image_overlay_noise(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """删 PDF 字层产出的空图块：两类 — ①被大框覆盖的空图；②孤立空图。"""
    texts = [a for a in atoms if a.get("atom_type") in ("text", "title")]
    images = [a for a in atoms if a.get("atom_type") == "image"]

    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") != "image":
            out.append(atom)
            continue
        text = (atom.get("content") or atom.get("ocr_text") or "").strip()
        if text and not text.startswith("[插图]"):
            out.append(atom)  # 有实际文字内容，不是空图块
            continue
        area = float(atom["x_end"] - atom["x_start"]) * float(atom["y_end"] - atom["y_start"])
        drop = False

        # 检查一：被更大的图/文本框大部分包住
        for other in atoms:
            if other is atom:
                continue
            other_area = float(other["x_end"] - other["x_start"]) * float(other["y_end"] - other["y_start"])
            if other_area <= area:
                continue
            if _containment_ratio(atom, other) >= 0.55:
                drop = True
                break
            if _overlap_area_ratio(atom, other) >= 0.60:
                drop = True
                break

        # 检查二：孤立空图块 — 只有 [插图]、面积小，且不包含任何文字原子
        if not drop:
            area = float(atom["x_end"] - atom["x_start"]) * float(atom["y_end"] - atom["y_start"])
            if area < 0.12:
                contains_text = False
                for t in texts:
                    if _containment_ratio(t, atom) >= 0.55:
                        contains_text = True
                        break
                if not contains_text:
                    drop = True

        if not drop:
            out.append(atom)
    return out


def _overlap_area_ratio(small: dict[str, Any], big: dict[str, Any]) -> float:
    """small 被 big 覆盖的面积占 small 总面积的比例。"""
    sx0 = float(small["x_start"]); sy0 = float(small["y_start"])
    sx1 = float(small["x_end"]);   sy1 = float(small["y_end"])
    bx0 = float(big["x_start"]);   by0 = float(big["y_start"])
    bx1 = float(big["x_end"]);     by1 = float(big["y_end"])
    ix0 = max(sx0, bx0); iy0 = max(sy0, by0)
    ix1 = min(sx1, bx1); iy1 = min(sy1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    small_area = max(1e-9, (sx1 - sx0) * (sy1 - sy0))
    return inter / small_area


def strip_leading_illustration_marker_text_atoms(
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """PDF/OCR 常把 [插图] 占位符与气泡汉字混进同一 text 框，剥掉 leading 行。"""
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if atom.get("atom_type") not in ("text", "title"):
            out.append(atom)
            continue
        raw = _atom_text(atom)
        if not raw.startswith(_ILLUSTRATION_MARKER):
            out.append(atom)
            continue
        lines = raw.splitlines()
        if not lines or lines[0].strip() != _ILLUSTRATION_MARKER:
            out.append(atom)
            continue
        rest = "\n".join(lines[1:]).strip()
        if not rest:
            out.append(atom)
            continue
        cleaned = dict(atom)
        cleaned["content"] = rest[:500]
        if atom.get("ocr_text") is not None:
            cleaned["ocr_text"] = rest[:500]
        out.append(cleaned)
    return out


def drop_gap_overlapping_real(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """占位条带若与真实原子重叠则丢弃，避免大框套小框。"""
    reals = [
        a
        for a in atoms
        if a.get("atom_type") == "image" or _is_real_text_atom(a)
    ]
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if not _is_gap_placeholder(atom):
            out.append(atom)
            continue
        if any(_iou(atom, real) > 0.12 for real in reals):
            continue
        out.append(atom)
    return out


def normalize_extracted_atoms(
    atoms: list[dict[str, Any]],
    *,
    page_num: int,
    fill_gaps: bool = False,
) -> list[dict[str, Any]]:
    """提取后统一规整：拆混排框 → 行合并 → 拼音+汉字 → 去重 → 再合并同行碎片。"""
    if not atoms:
        return atoms

    atoms = split_mixed_pinyin_cjk_atoms(atoms)
    atoms = split_wrapped_dialogue_text_atoms(atoms)
    atoms = split_embedded_side_note_atoms(atoms)
    atoms = split_interview_side_bubble_atoms(atoms)
    atoms = strip_leading_illustration_marker_text_atoms(atoms)
    atoms = split_multiline_pinyin_hanzi_atoms(atoms)
    atoms = clamp_tall_single_line_text_bboxes(atoms)

    prev_len = -1
    while len(atoms) != prev_len:
        prev_len = len(atoms)
        atoms = merge_text_atoms_by_row(atoms)

    atoms = split_wide_pinyin_by_hanzi_columns(atoms)
    atoms = refine_atom_layout_bboxes(atoms)

    prev_len = -1
    while len(atoms) != prev_len:
        prev_len = len(atoms)
        atoms = merge_pinyin_with_hanzi_below(atoms)

    atoms = merge_pinyin_hanzi_paragraph_blocks(atoms)

    atoms = dedup_overlapping_text_atoms(atoms)

    prev_len = -1
    while len(atoms) != prev_len:
        prev_len = len(atoms)
        atoms = merge_text_atoms_by_row(atoms)

    atoms = merge_text_fragments_across_inline_images(atoms)

    atoms = merge_pinyin_hanzi_paragraph_blocks(atoms)
    atoms = dedup_overlapping_image_atoms(atoms)
    atoms = drop_mixed_content_wrapper_atoms(atoms)
    atoms = drop_text_spanning_wrapper_atoms(atoms)
    atoms = drop_atoms_contained_in_larger(atoms)
    atoms = drop_tiny_noise_atoms(atoms)
    if fill_gaps:
        atoms = fill_vertical_gaps(atoms, page_num)
        atoms = drop_gap_overlapping_real(atoms)
    atoms = split_mixed_pinyin_cjk_atoms(atoms)
    atoms = split_wrapped_dialogue_text_atoms(atoms)
    atoms = split_embedded_side_note_atoms(atoms)
    atoms = split_interview_side_bubble_atoms(atoms)
    atoms = strip_leading_illustration_marker_text_atoms(atoms)
    atoms = clamp_tall_single_line_text_bboxes(atoms)
    atoms = refine_atom_layout_bboxes(atoms)
    atoms = tag_side_note_roles(atoms)
    atoms = clamp_body_away_from_side_notes(atoms)
    return atoms


def merge_text_atoms_for_display(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """行级 + 拼音合并 + 去重（不插入缝隙占位）。"""
    page = int(atoms[0].get("page", 1)) if atoms else 1
    return normalize_extracted_atoms(atoms, page_num=page, fill_gaps=False)


def _iou(a: dict[str, Any], b: dict[str, Any]) -> float:
    ax0, ay0, ax1, ay1 = a["x_start"], a["y_start"], a["x_end"], a["y_end"]
    bx0, by0, bx1, by1 = b["x_start"], b["y_start"], b["x_end"], b["y_end"]
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(1e-9, (ax1 - ax0) * (ay1 - ay0))
    area_b = max(1e-9, (bx1 - bx0) * (by1 - by0))
    return inter / (area_a + area_b - inter)


def _rapidocr_atoms_on_image(
    image_path: Path, page_num: int, img_w: int, img_h: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return out
    ocr = RapidOCR()
    result, _ = ocr(str(image_path))
    if not result:
        return out
    for it in result:
        if not it or len(it) < 2:
            continue
        box, text = it[0], (it[1] or "").strip()
        if not text:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except (TypeError, ValueError, IndexError):
            continue
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        if (x1 - x0) < 8 or (y1 - y0) < 8:
            continue
        xs_u, ys_u, xe_u, ye_u = bbox_to_unit(x0, y0, x1, y1, float(img_w), float(img_h))
        if (xe_u - xs_u) < 0.01 and (ye_u - ys_u) < 0.01:
            continue
        out.append(
            {
                "atom_id": f"A{page_num:03d}-ocr-{len(out)+1:03d}",
                "atom_type": "text",
                "page": page_num,
                "y_start": round(ys_u, 4),
                "y_end": round(ye_u, 4),
                "x_start": round(xs_u, 4),
                "x_end": round(xe_u, 4),
                "content": text[:500],
                "ocr_text": text[:500],
                "parent_block_id": None,
                "bound_cw_pgs": [],
                "is_locked": False,
            }
        )
    return out


def _should_drop_pdf_for_ocr(pa: dict[str, Any], oa: dict[str, Any]) -> bool:
    """OCR 局部框不应误删同行 PDF 续字；仅当 OCR 明显覆盖该字层碎片时才丢。"""
    pt = (pa.get("content") or pa.get("ocr_text") or "").strip()
    ot = (oa.get("content") or oa.get("ocr_text") or "").strip()
    if _is_pinyin_only_atom(oa) and _has_cjk(pt):
        return False
    if _containment_ratio(pa, oa) > 0.78:
        return True
    if pt and ot and pt in ot:
        return True
    iou = _iou(oa, pa)
    if iou > 0.5:
        return len(ot) >= len(pt)
    if iou > 0.28:
        return len(ot) >= max(1, len(pt)) and len(ot) >= len(pt) * 1.05
    return False


def _pdf_atom_superseded_by_ocr_row(
    pa: dict[str, Any], ocr_atoms: list[dict[str, Any]]
) -> bool:
    """PDF 宽条若混有拼音+汉字，且同行已有 OCR 细框，则丢弃 PDF 大框。"""
    if pa.get("atom_type") not in ("text", "title"):
        return False
    if not _find_pinyin_cjk_split_in_text(_atom_text(pa)):
        return False
    px0, px1 = float(pa["x_start"]), float(pa["x_end"])
    py = _y_center(pa)
    ocr_row = [
        o
        for o in ocr_atoms
        if o.get("atom_type") in ("text", "title")
        and abs(_y_center(o) - py) <= 0.035
        and float(o["x_end"]) > px0 + 0.02
        and float(o["x_start"]) < px1 - 0.02
    ]
    if len(ocr_row) >= 2:
        return True
    if len(ocr_row) == 1:
        ow = float(ocr_row[0]["x_end"]) - float(ocr_row[0]["x_start"])
        pw = px1 - px0
        if pw > max(ow * 1.25, 0.45):
            return True
    return False


def _instruction_tail_hanzi_from_text(text: str) -> str | None:
    """从被 OCR 覆盖的 PDF 大字层里抽出题干续行汉字（如「用力的方向。」）。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if not _has_cjk(ln) or _looks_like_pinyin(ln):
            continue
        if _segment_semantic_kind([ln]) == "instruction_tail":
            return ln
    plain = re.sub(r"\s+", "", text)
    if not plain or not _has_cjk(plain):
        return None
    m = re.search(r"手(用力的方向[。．]?)$", plain)
    if m:
        tail = m.group(1)
        return tail if tail.endswith("。") else tail + "。"
    if len(lines) == 1 and _looks_like_numbered_instruction(lines[0]):
        m2 = re.search(r"([^\d\.．、]{2,12}。)$", plain)
        if m2:
            tail = m2.group(1)
            if 3 <= len(tail) <= 10 and _segment_semantic_kind([tail]) == "instruction_tail":
                return tail
    return None


def _salvage_hanzi_atom_for_pinyin_only_ocr(
    pa: dict[str, Any],
    ocr_texts: list[dict[str, Any]],
    *,
    suffix: str = "-salv",
) -> dict[str, Any] | None:
    """PDF 大框因 OCR 重叠被删时，为下方「仅拼音」OCR 行补回汉字原子。"""
    tail_hz = _instruction_tail_hanzi_from_text(_atom_text(pa))
    if not tail_hz:
        return None
    best_oa: dict[str, Any] | None = None
    best_score = -999.0
    hz_len = len(re.sub(r"\s+", "", tail_hz))
    for oa in ocr_texts:
        if not _is_pinyin_only_atom(oa):
            continue
        oy0 = float(oa["y_start"])
        if oy0 < float(pa["y_start"]) + _atom_height(pa) * 0.22:
            continue
        if oy0 > float(pa["y_end"]) + 0.14:
            continue
        py_syl = len(_pinyin_syllables(_atom_text(oa)))
        if abs(py_syl - hz_len) > 2:
            continue
        gap = oy0 - float(pa["y_end"])
        score = -abs(gap) - abs(py_syl - hz_len) * 0.05
        if score > best_score:
            best_score = score
            best_oa = oa
    if best_oa is None:
        return None
    line_h = max(0.022, min(0.035, _atom_height(best_oa) + 0.012))
    est_w = max(0.12, min(0.45, hz_len * 0.028 + 0.04))
    return {
        "atom_id": f"{pa.get('atom_id', 'pdf')}{suffix}",
        "atom_type": "text",
        "page": int(pa.get("page") or 1),
        "x_start": round(float(best_oa["x_start"]), 4),
        "x_end": round(min(1.0, float(best_oa["x_start"]) + est_w), 4),
        "y_start": round(float(best_oa["y_end"]) - 0.003, 4),
        "y_end": round(float(best_oa["y_end"]) + line_h, 4),
        "content": tail_hz[:500],
        "ocr_text": tail_hz[:500],
        "parent_block_id": None,
        "bound_cw_pgs": [],
        "is_locked": False,
    }


def merge_pdf_text_and_ocr(
    pdf_atoms: list[dict[str, Any]], ocr_atoms: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """去重：OCR 与字层重叠时保留更完整的一方，同行续字留给行合并。"""
    if not ocr_atoms:
        return list(pdf_atoms)
    if not pdf_atoms:
        return list(ocr_atoms)

    ocr_texts = [o for o in ocr_atoms if o.get("atom_type") in ("text", "title")]
    kept_pdf: list[dict[str, Any]] = []
    salvaged: list[dict[str, Any]] = []
    for pa in pdf_atoms:
        if pa.get("atom_type") == "image":
            kept_pdf.append(pa)
            continue
        if pa.get("atom_type") not in ("text", "title"):
            kept_pdf.append(pa)
            continue
        if _pdf_atom_superseded_by_ocr_row(pa, ocr_texts):
            continue
        drop = any(
            _should_drop_pdf_for_ocr(pa, oa)
            for oa in ocr_texts
        )
        if not drop:
            kept_pdf.append(pa)
            continue
        salv = _salvage_hanzi_atom_for_pinyin_only_ocr(pa, ocr_texts)
        if salv:
            salvaged.append(salv)

    merged = kept_pdf + salvaged + list(ocr_atoms)
    merged.sort(key=lambda a: (a["page"], a["y_start"], a["x_start"]))
    return merged


def _image_region_overlaps_atoms(
    region: dict[str, Any],
    atoms: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.22,
) -> bool:
    """插图候选与已有 image 框重合则跳过。"""
    for a in atoms:
        if a.get("atom_type") != "image":
            continue
        if _iou(region, a) >= iou_threshold:
            return True
        if _containment_ratio(region, a) >= 0.55 or _containment_ratio(a, region) >= 0.55:
            return True
    return False


def is_spurious_image_atom(atom: dict[str, Any]) -> bool:
    """页眉页脚空白条、极扁噪声等，不当插图原子。"""
    if atom.get("atom_type") != "image":
        return False
    x0 = float(atom.get("x_start", 0))
    y0 = float(atom.get("y_start", 0))
    x1 = float(atom.get("x_end", 1))
    y1 = float(atom.get("y_end", 1))
    w, h = max(0.0, x1 - x0), max(0.0, y1 - y0)
    area = w * h
    if area < 0.004:
        return True
    aspect = w / max(1e-6, h)
    if aspect >= 6.0 and h < 0.10:
        return True
    if aspect <= 0.15 and w < 0.10:
        return True
    if y0 >= 0.86 and w >= 0.30 and h <= 0.14:
        return True
    if y1 <= 0.14 and w >= 0.45 and h <= 0.12:
        return True
    return False


def _atom_area(atom: dict[str, Any]) -> float:
    try:
        w = float(atom.get("x_end") or 0) - float(atom.get("x_start") or 0)
        h = float(atom.get("y_end") or 0) - float(atom.get("y_start") or 0)
    except (TypeError, ValueError):
        return 1e-9
    return max(1e-9, w * h)


def _image_overlaps_text_atoms(
    atom: dict[str, Any],
    text_atoms: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.32,
    prefer_keep_image: bool = False,
) -> bool:
    """插图框与「图外正文」高度重合时视为误检。

    保留场景：
    - 图内地名/注记（文字大半落在插图内）
    - 资料袋正文绕排：大文字框包住内嵌地图（插图几乎全在文字框内，但文字框明显更大）
    """
    img_area = max(1e-9, _atom_area(atom))
    for t in text_atoms:
        if t.get("atom_type") not in ("text", "title"):
            continue
        # 文字大部分落在插图内 → 图内地名/注记，保留插图
        if _containment_ratio(t, atom) >= 0.55:
            continue
        text_area = max(1e-9, _atom_area(t))
        # 小块图内文字（面积远小于插图）即使 IoU 偏高也保留插图
        if text_area < img_area * 0.35 and _containment_ratio(t, atom) >= 0.25:
            continue
        img_in_text = _containment_ratio(atom, t)
        # LLM 插图 + 正文绕排：大文字框包住内嵌地图时保留插图（CV 噪声框不适用）
        if (
            prefer_keep_image
            and img_area >= 0.03
            and img_in_text >= 0.40
            and text_area >= img_area * 1.2
        ):
            continue
        if _iou(atom, t) >= iou_threshold:
            return True
        if img_in_text >= 0.45:
            return True
    return False


def _is_llm_layout_image_atom(atom: dict[str, Any]) -> bool:
    if atom.get("detect_source") == "layout_llm":
        return True
    aid = str(atom.get("atom_id") or "")
    return "-llm-" in aid or str(atom.get("content") or "").startswith("[插图]")


def filter_image_atom_candidates(
    images: list[dict[str, Any]],
    text_atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for img in images:
        if is_spurious_image_atom(img):
            continue
        # 豆包版面插图：资料袋正文常整框包住地图，禁止再用文字重叠杀掉
        if _is_llm_layout_image_atom(img):
            out.append(img)
            continue
        if _image_overlaps_text_atoms(img, text_atoms):
            continue
        out.append(img)
    return out


def detect_illustration_atoms_from_bgr(
    bgr: Any,
    page_num: int,
    existing_atoms: list[dict[str, Any]],
    *,
    min_area_ratio: float = 0.012,
    min_side_ratio: float = 0.06,
) -> list[dict[str, Any]]:
    """扫描页插图检测：在非 OCR 文字区用 OpenCV 找照片/示意图块。"""
    try:
        import cv2  # type: ignore
        import numpy as np
    except ImportError:
        return []

    if bgr is None:
        return []
    img_h, img_w = bgr.shape[:2]
    if img_w < 32 or img_h < 32:
        return []

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]

    mask_text = np.zeros((img_h, img_w), dtype=np.uint8)
    pad_y = max(6, int(img_h * 0.012))
    pad_x = max(6, int(img_w * 0.012))
    for a in existing_atoms:
        if a.get("atom_type") not in ("text", "title"):
            continue
        # 短标签（地名等）少挖空，避免把线划地图裁成碎片
        tw = float(a.get("x_end", 0)) - float(a.get("x_start", 0))
        th = float(a.get("y_end", 0)) - float(a.get("y_start", 0))
        if tw * th < 0.012 and max(tw, th) < 0.22:
            continue
        x0 = int(float(a["x_start"]) * img_w)
        y0 = int(float(a["y_start"]) * img_h)
        x1 = int(float(a["x_end"]) * img_w)
        y1 = int(float(a["y_end"]) * img_h)
        cv2.rectangle(
            mask_text,
            (max(0, x0 - pad_x), max(0, y0 - pad_y)),
            (min(img_w, x1 + pad_x), min(img_h, y1 + pad_y)),
            255,
            -1,
        )

    _, content_gray = cv2.threshold(gray, 235, 255, cv2.THRESH_BINARY_INV)
    # 低饱和线划地图（浅灰/淡蓝描边）也要检出
    _, content_sat = cv2.threshold(sat, 16, 255, cv2.THRESH_BINARY)
    edges = cv2.Canny(gray, 40, 120)
    _, content_edge = cv2.threshold(edges, 1, 255, cv2.THRESH_BINARY)
    content = cv2.bitwise_or(content_gray, content_sat)
    content = cv2.bitwise_or(content, content_edge)
    content[mask_text > 0] = 0

    kx = max(5, img_w // 64)
    ky = max(5, img_h // 64)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky))
    content = cv2.morphologyEx(content, cv2.MORPH_CLOSE, kernel, iterations=2)
    content = cv2.morphologyEx(content, cv2.MORPH_OPEN, kernel, iterations=1)

    contours, _ = cv2.findContours(content, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    page_area = float(img_w * img_h)
    out: list[dict[str, Any]] = []
    idx = 0
    for cnt in contours or []:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw * ch < min_area_ratio * page_area:
            continue
        if cw < img_w * min_side_ratio and ch < img_h * min_side_ratio:
            continue
        if cw < 24 or ch < 24:
            continue
        xs_u = round(x / img_w, 4)
        ys_u = round(y / img_h, 4)
        xe_u = round((x + cw) / img_w, 4)
        ye_u = round((y + ch) / img_h, 4)
        if (xe_u - xs_u) < 0.04 or (ye_u - ys_u) < 0.04:
            continue
        # 页脚/页眉式细长条：宽但极扁，多为装饰线/空白噪声，不当插图
        aspect = (xe_u - xs_u) / max(1e-6, ye_u - ys_u)
        if aspect >= 8.0 and (ye_u - ys_u) < 0.08:
            continue
        if (ye_u - ys_u) >= 0.55 and (xe_u - xs_u) >= 0.85:
            # 接近整页的大块通常是背景/扫描噪声
            continue
        idx += 1
        candidate = {
            "atom_id": f"A{page_num:03d}-imgcv-{idx:03d}",
            "atom_type": "image",
            "page": page_num,
            "x_start": xs_u,
            "y_start": ys_u,
            "x_end": xe_u,
            "y_end": ye_u,
            "content": "[插图]",
            "ocr_text": "",
            "parent_block_id": None,
            "bound_cw_pgs": [],
            "is_locked": False,
        }
        if _image_region_overlaps_atoms(candidate, existing_atoms + out):
            continue
        out.append(candidate)
    return out


def detect_illustration_atoms_from_page_image(
    image_path: Path,
    page_num: int,
    existing_atoms: list[dict[str, Any]],
    *,
    min_area_ratio: float = 0.012,
    min_side_ratio: float = 0.06,
) -> list[dict[str, Any]]:
    """扫描页插图检测（从文件路径加载 BGR）。"""
    try:
        import cv2  # type: ignore
    except ImportError:
        return []
    bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if bgr is None:
        return []
    return detect_illustration_atoms_from_bgr(
        bgr,
        page_num,
        existing_atoms,
        min_area_ratio=min_area_ratio,
        min_side_ratio=min_side_ratio,
    )


def fill_vertical_gaps(
    atoms: list[dict[str, Any]], page_num: int, min_gap: float = 0.045
) -> list[dict[str, Any]]:
    """在 0–1 竖直方向未覆盖的条带插入可选中占位原子，避免大块「点不到」。"""
    if not atoms:
        return [
            {
                "atom_id": f"A{page_num:03d}-GAP-001",
                "atom_type": "text",
                "page": page_num,
                "x_start": 0.0,
                "x_end": 1.0,
                "y_start": 0.0,
                "y_end": 1.0,
                "content": "[整页未识别，请用外部工具补原子或检查素材]",
                "ocr_text": "",
                "parent_block_id": None,
                "bound_cw_pgs": [],
                "is_locked": False,
            }
        ]
    intervals = sorted(
        [(float(a["y_start"]), float(a["y_end"])) for a in atoms],
        key=lambda t: t[0],
    )
    merged_iv: list[tuple[float, float]] = []
    for y0, y1 in intervals:
        if merged_iv and y0 <= merged_iv[-1][1]:
            merged_iv[-1] = (merged_iv[-1][0], max(merged_iv[-1][1], y1))
        else:
            merged_iv.append((y0, y1))
    fillers: list[dict[str, Any]] = []
    cursor = 0.0
    gidx = 0
    for y0, y1 in merged_iv:
        if y0 - cursor >= min_gap:
            gidx += 1
            fillers.append(
                {
                    "atom_id": f"A{page_num:03d}-GAP-{gidx:02d}",
                    "atom_type": "text",
                    "page": page_num,
                    "x_start": 0.0,
                    "x_end": 1.0,
                    "y_start": round(cursor, 4),
                    "y_end": round(y0, 4),
                    "content": "[未拆分区域，可整块绑定]",
                    "ocr_text": "",
                    "parent_block_id": None,
                    "bound_cw_pgs": [],
                    "is_locked": False,
                }
            )
        cursor = max(cursor, y1)
    if 1.0 - cursor >= min_gap:
        gidx += 1
        fillers.append(
            {
                "atom_id": f"A{page_num:03d}-GAP-{gidx:02d}",
                "atom_type": "text",
                "page": page_num,
                "x_start": 0.0,
                "x_end": 1.0,
                "y_start": round(cursor, 4),
                "y_end": 1.0,
                "content": "[未拆分区域，可整块绑定]",
                "ocr_text": "",
                "parent_block_id": None,
                "bound_cw_pgs": [],
                "is_locked": False,
            }
        )
    atoms = atoms + fillers
    atoms.sort(key=lambda a: (a["page"], a["y_start"], a["x_start"]))
    return atoms


def _reassign_atom_ids(page_num: int, atoms: list[dict[str, Any]], *, id_prefix: str = "A") -> None:
    for i, a in enumerate(atoms, start=1):
        a["atom_id"] = f"{id_prefix}{page_num:03d}-{i:03d}"


def _reassign_image_atom_ids_only(
    page_num: int, atoms: list[dict[str, Any]], *, id_prefix: str = "A"
) -> None:
    """保留 text/title 的 atom_id；仅为 image（及无 id 的）分配不冲突编号。"""
    used: set[str] = set()
    for a in atoms:
        if a.get("atom_type") in ("text", "title") and a.get("atom_id"):
            used.add(str(a["atom_id"]))
    n = 1
    for a in atoms:
        if a.get("atom_type") in ("text", "title") and a.get("atom_id"):
            continue
        while True:
            cand = f"{id_prefix}{page_num:03d}-{n:03d}"
            n += 1
            if cand not in used:
                a["atom_id"] = cand
                used.add(cand)
                break


def _load_page_bgr_and_ocr_paths(
    image_path: Path | None,
    *,
    use_ocr: bool,
) -> tuple[Any | None, Any | None, int, int, Path | None, Path | None]:
    """返回 (page_bgr, stamp_mask, img_w, img_h, ocr_path, temp_dir)。"""
    import tempfile

    page_bgr = None
    stamp_mask = None
    img_w = img_h = 0
    ocr_path: Path | None = image_path
    temp_ocr_dir: Path | None = None

    if not image_path or not image_path.is_file() or not use_ocr:
        return page_bgr, stamp_mask, img_w, img_h, ocr_path, temp_ocr_dir

    try:
        if _atom_ocr_remove_stamp():
            from .pdf_stamp_remove import prepare_lesson_page_bgr_from_path

            page_bgr, stamp_mask = prepare_lesson_page_bgr_from_path(image_path)
            img_h, img_w = page_bgr.shape[:2]
            import cv2  # type: ignore

            temp_ocr_dir = Path(tempfile.mkdtemp(prefix="tbocr_clean_"))
            ocr_path = temp_ocr_dir / "page.png"
            cv2.imwrite(str(ocr_path), page_bgr)
        else:
            from PIL import Image

            im = Image.open(image_path)
            img_w, img_h = im.size
    except Exception:
        ocr_path = image_path

    return page_bgr, stamp_mask, img_w, img_h, ocr_path, temp_ocr_dir


def _cleanup_temp_ocr_dir(temp_ocr_dir: Path | None, ocr_path: Path | None) -> None:
    if not temp_ocr_dir:
        return
    try:
        if ocr_path and ocr_path.is_file():
            ocr_path.unlink(missing_ok=True)
        temp_ocr_dir.rmdir()
    except OSError:
        pass


def _collect_image_atom_candidates(
    *,
    page_num: int,
    image_path: Path | None,
    page_bgr: Any | None,
    text_atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """第二阶段：OpenCV + Doubao 只产出插图原子，不删改已有文字。"""
    if not image_path or not image_path.is_file():
        return []

    if page_bgr is None:
        try:
            import cv2  # type: ignore

            page_bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        except Exception:
            page_bgr = None

    images: list[dict[str, Any]] = []
    if page_bgr is not None:
        cv_img = detect_illustration_atoms_from_bgr(page_bgr, page_num, text_atoms)
        if cv_img:
            images.extend(cv_img)

    try:
        from ..services.llm.page_layout_extract import (
            extract_page_layout,
            layout_regions_to_image_atoms,
        )

        layout = extract_page_layout(image_path, page_bgr=page_bgr)
        if layout and layout.regions:
            llm_images, _exclude = layout_regions_to_image_atoms(layout, page_num)
            if llm_images:
                images = merge_layout_image_atoms(text_atoms + images, llm_images)
                images = [a for a in images if a.get("atom_type") == "image"]
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "page layout LLM failed page=%s: %s", page_num, exc
        )

    if not images:
        return []
    images = dedup_overlapping_image_atoms(images)
    # 大框「[插图]」包住多张实拍子图时删外包，避免与水母/青蛙等子图重复
    images = drop_mixed_content_wrapper_atoms(images)
    images = filter_image_atom_candidates(images, text_atoms)
    # 版面 LLM 开启时：禁止只落「[插图]」占位——裁剪后强制视觉命名
    try:
        from ..services.llm.page_layout_extract import ensure_illustration_atom_labels

        images = ensure_illustration_atom_labels(
            images, page_bgr=page_bgr, image_path=image_path
        )
    except Exception as exc:
        import logging

        logging.getLogger(__name__).error(
            "illustration naming failed page=%s: %s", page_num, exc
        )
        raise
    return images


def _extract_text_atoms_phase(
    *,
    page_num: int,
    pdf_atoms: list[dict[str, Any]],
    ocr_atoms: list[dict[str, Any]],
    stamp_mask: Any | None,
    img_w: int,
    img_h: int,
    page_bgr: Any | None,
    fill_gaps: bool,
) -> list[dict[str, Any]]:
    """第一阶段：仅文字（PDF 字层 + RapidOCR + 规整），不含插图检测与 LLM 版面。"""
    merged = merge_pdf_text_and_ocr(pdf_atoms, ocr_atoms)
    if stamp_mask is not None and img_w > 0 and img_h > 0:
        merged = drop_atoms_in_stamp_mask(merged, stamp_mask, img_w, img_h)
    merged = [a for a in merged if a.get("atom_type") in ("text", "title")]
    merged = merge_text_fragments_across_inline_images(
        merged, page_bgr=page_bgr
    )
    merged = normalize_extracted_atoms(merged, page_num=page_num, fill_gaps=fill_gaps)
    return merged


def _combine_text_and_image_phases(
    *,
    text_atoms: list[dict[str, Any]],
    image_atoms: list[dict[str, Any]],
    page_num: int,
    page_bgr: Any | None,
    fill_gaps: bool,
    preserve_text: bool = False,
) -> list[dict[str, Any]]:
    """合并两阶段结果；绕排句合并 + 插图去重，不再整页 normalize（避免文字被二次拆碎）。

    preserve_text=True：已有豆包/文字 OCR 结果时只挂插图，禁止绕排合并、RapidOCR 回填与填缝占位，
    否则会改写正文（如「① 观潮」→「阅读①x观潮」）并插入「[未拆分区域…]」虚空框。
    """
    # 去掉历史上误写入的填缝占位，避免再次进入缓存
    cleaned_text = [a for a in text_atoms if not _is_gap_placeholder(a)]
    combined = list(cleaned_text) + list(image_atoms)
    if not combined:
        return combined
    if not preserve_text:
        combined = merge_text_fragments_across_inline_images(
            combined, page_bgr=page_bgr
        )
    combined = dedup_overlapping_image_atoms(combined)
    combined = drop_mixed_content_wrapper_atoms(combined)
    if fill_gaps and not preserve_text:
        combined = fill_vertical_gaps(combined, page_num)
        combined = drop_gap_overlapping_real(combined)
    combined.sort(key=lambda a: (a.get("page", 0), a["y_start"], a["x_start"]))
    return combined


def extract_atoms_for_textbook_page(
    *,
    page_num: int,
    image_path: Path | None,
    pdf_path: Path | None,
    pdf_page_index: int | None,
    use_ocr: bool = True,
    fill_gaps: bool = False,
    id_prefix: str = "A",
    source: str = "old",
    ocr_phase: str = "all",
    existing_text_atoms: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    两阶段 OCR（默认）：
    ① 文字：PDF 字层 + RapidOCR + normalize（不含插图 LLM/OpenCV）；
    ② 插图：OpenCV + Doubao bbox 补 image 原子；
    最后仅做绕排合并与插图去重，不再整页 normalize。
    """
    import tempfile

    import fitz  # type: ignore

    pdf_atoms: list[dict[str, Any]] = []
    ocr_raster: list[dict[str, Any]] = []
    ocr_image_file: list[dict[str, Any]] = []

    page_bgr, stamp_mask, img_w, img_h, ocr_path, temp_ocr_dir = (
        _load_page_bgr_and_ocr_paths(image_path, use_ocr=use_ocr)
    )

    if pdf_path and pdf_path.is_file():
        doc = fitz.open(str(pdf_path))
        idx = pdf_page_index if pdf_page_index is not None else max(0, page_num - 1)
        if idx < 0 or idx >= len(doc):
            doc.close()
            _cleanup_temp_ocr_dir(temp_ocr_dir, ocr_path)
            return []
        page = doc.load_page(idx)
        pdf_atoms = extract_atoms_from_fitz_page(page, page_num)
        if use_ocr and not ocr_image_file:
            try:
                mat = fitz.Matrix(2.0, 2.0)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                td = Path(tempfile.mkdtemp(prefix="tbocr_"))
                tp = td / "raster.png"
                pix.save(str(tp))
                ocr_raster = _rapidocr_atoms_on_image(tp, page_num, pix.width, pix.height)
                tp.unlink(missing_ok=True)
                td.rmdir()
            except Exception:
                pass
        doc.close()

    if ocr_path and ocr_path.is_file() and use_ocr:
        try:
            if img_w <= 0 or img_h <= 0:
                from PIL import Image

                im = Image.open(ocr_path)
                img_w, img_h = im.size
            if img_w > 0 and img_h > 0:
                ocr_image_file = _rapidocr_atoms_on_image(
                    ocr_path, page_num, img_w, img_h
                )
        except Exception:
            pass

    _cleanup_temp_ocr_dir(temp_ocr_dir, ocr_path)

    ocr_atoms = ocr_image_file if ocr_image_file else ocr_raster

    phase = (ocr_phase or "all").strip().lower()
    if phase not in ("all", "text", "images"):
        phase = "all"

    if phase == "images" and existing_text_atoms is not None:
        text_atoms = [a for a in existing_text_atoms if not _is_gap_placeholder(a)]
        image_atoms = _collect_image_atom_candidates(
            page_num=page_num,
            image_path=image_path,
            page_bgr=page_bgr,
            text_atoms=text_atoms,
        )
        merged = _combine_text_and_image_phases(
            text_atoms=text_atoms,
            image_atoms=image_atoms,
            page_num=page_num,
            page_bgr=page_bgr,
            fill_gaps=False,
            preserve_text=True,
        )
    elif _atom_ocr_two_phase() and phase in ("all", "text", "images"):
        text_atoms = _extract_text_atoms_phase(
            page_num=page_num,
            pdf_atoms=pdf_atoms,
            ocr_atoms=ocr_atoms,
            stamp_mask=stamp_mask,
            img_w=img_w,
            img_h=img_h,
            page_bgr=page_bgr,
            fill_gaps=False,
        )
        if phase == "text":
            merged = text_atoms
        else:
            image_atoms = _collect_image_atom_candidates(
                page_num=page_num,
                image_path=image_path,
                page_bgr=page_bgr,
                text_atoms=text_atoms,
            )
            if phase == "images":
                merged = _combine_text_and_image_phases(
                    text_atoms=text_atoms,
                    image_atoms=image_atoms,
                    page_num=page_num,
                    page_bgr=page_bgr,
                    fill_gaps=fill_gaps,
                )
            else:
                merged = _combine_text_and_image_phases(
                    text_atoms=text_atoms,
                    image_atoms=image_atoms,
                    page_num=page_num,
                    page_bgr=page_bgr,
                    fill_gaps=fill_gaps,
                )
    else:
        merged = merge_pdf_text_and_ocr(pdf_atoms, ocr_atoms)
        if stamp_mask is not None and img_w > 0 and img_h > 0:
            merged = drop_atoms_in_stamp_mask(merged, stamp_mask, img_w, img_h)
        if image_path and image_path.is_file():
            if page_bgr is None:
                try:
                    import cv2  # type: ignore

                    page_bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
                except Exception:
                    page_bgr = None
            if page_bgr is not None:
                illus = detect_illustration_atoms_from_bgr(page_bgr, page_num, merged)
                if illus:
                    merged = merged + illus
                    merged = dedup_overlapping_image_atoms(merged)
                    merged.sort(key=lambda a: (a["page"], a["y_start"], a["x_start"]))
            try:
                from ..services.llm.page_layout_extract import (
                    extract_page_layout,
                    layout_regions_to_image_atoms,
                )

                layout = extract_page_layout(image_path, page_bgr=page_bgr)
                if layout and layout.regions:
                    llm_images, exclude_regions = layout_regions_to_image_atoms(
                        layout, page_num
                    )
                    merged = drop_atoms_overlapping_exclude_regions(
                        merged, exclude_regions
                    )
                    if llm_images:
                        merged = merge_layout_image_atoms(merged, llm_images)
                        merged = dedup_overlapping_image_atoms(merged)
                        merged.sort(
                            key=lambda a: (a["page"], a["y_start"], a["x_start"])
                        )
            except Exception:
                pass
        merged = merge_text_fragments_across_inline_images(
            merged, page_bgr=page_bgr
        )
        merged = normalize_extracted_atoms(merged, page_num=page_num, fill_gaps=fill_gaps)

    if not merged and image_path and image_path.is_file():
        merged = [
            {
                "atom_id": f"A{page_num:03d}-FULL-001",
                "atom_type": "image",
                "page": page_num,
                "x_start": 0.0,
                "y_start": 0.0,
                "x_end": 1.0,
                "y_end": 1.0,
                "content": "[整页图像]",
                "ocr_text": "",
                "parent_block_id": None,
                "bound_cw_pgs": [],
                "is_locked": False,
            }
        ]
    if not merged:
        return []

    # 挂插图到已有文字时：保留文字 atom_id，只给新插图编号，避免文字比对锚点漂移
    if phase == "images" and existing_text_atoms is not None:
        _reassign_image_atom_ids_only(page_num, merged, id_prefix=id_prefix)
    else:
        _reassign_atom_ids(page_num, merged, id_prefix=id_prefix)
    for a in merged:
        a["source"] = source
    return merged


def extract_atoms_for_lesson_page_image(
    *,
    page_index: int,
    image_path: Path,
    pdf_path: Path | None = None,
    pdf_page_index: int | None = None,
    use_ocr: bool = True,
    fill_gaps: bool = True,
    source: str = "old",
    ocr_phase: str = "all",
    existing_text_atoms: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """从 lesson_pages 落盘的 PNG 提取原子（可选 PDF 字层补充）。"""
    return extract_atoms_for_textbook_page(
        page_num=int(page_index),
        image_path=image_path,
        pdf_path=pdf_path,
        pdf_page_index=pdf_page_index,
        use_ocr=use_ocr,
        fill_gaps=fill_gaps,
        id_prefix="A",
        source=source,
        ocr_phase=ocr_phase,
        existing_text_atoms=existing_text_atoms,
    )
