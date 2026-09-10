"""教材原子整理：规则兜底（页码删除、活动条合并等），与视觉 LLM 互补。"""
from __future__ import annotations

import re
from typing import Any, Literal

from .config import atom_curate_heuristics_mode

_PAGE_NUM_RE = re.compile(r"^\d{1,3}$")
_DIALOGUE_MARKERS = ("……", "…", "...", "——")
_PYNINISH_RE = re.compile(
    r"^[a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜüɑ\s·]+$"
)
_ILLUSTRATION_MARKER = "[插图]"


def _text(atom: dict[str, Any]) -> str:
    return (atom.get("content") or atom.get("ocr_text") or "").strip()


def _strip_leading_illustration_lines(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    while lines and lines[0] == _ILLUSTRATION_MARKER:
        lines.pop(0)
    return "\n".join(lines).strip()


def _body_text(atom: dict[str, Any]) -> str:
    """规则判断用正文：去掉 OCR 误入 text 框的 leading [插图] 行。"""
    return _strip_leading_illustration_lines(_text(atom))


def _bbox(atom: dict[str, Any]) -> dict[str, float]:
    b = atom.get("bbox") or {}
    return {
        "x0": float(b.get("x_start", 0)),
        "y0": float(b.get("y_start", 0)),
        "x1": float(b.get("x_end", 1)),
        "y1": float(b.get("y_end", 1)),
    }


def _area(atom: dict[str, Any]) -> float:
    b = _bbox(atom)
    return max(0.0, b["x1"] - b["x0"]) * max(0.0, b["y1"] - b["y0"])


def _y_center(atom: dict[str, Any]) -> float:
    b = _bbox(atom)
    return (b["y0"] + b["y1"]) / 2


def _x_center(atom: dict[str, Any]) -> float:
    b = _bbox(atom)
    return (b["x0"] + b["x1"]) / 2


def _height(atom: dict[str, Any]) -> float:
    b = _bbox(atom)
    return b["y1"] - b["y0"]


def _width(atom: dict[str, Any]) -> float:
    b = _bbox(atom)
    return b["x1"] - b["x0"]


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ba, bb = _bbox(a), _bbox(b)
    return (
        ba["x0"] < bb["x1"]
        and ba["x1"] > bb["x0"]
        and ba["y0"] < bb["y1"]
        and ba["y1"] > bb["y0"]
    )


def _iou(a: dict[str, Any], b: dict[str, Any]) -> float:
    ba, bb = _bbox(a), _bbox(b)
    ix0 = max(ba["x0"], bb["x0"])
    iy0 = max(ba["y0"], bb["y0"])
    ix1 = min(ba["x1"], bb["x1"])
    iy1 = min(ba["y1"], bb["y1"])
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(1e-9, (ba["x1"] - ba["x0"]) * (ba["y1"] - ba["y0"]))
    area_b = max(1e-9, (bb["x1"] - bb["x0"]) * (bb["y1"] - bb["y0"]))
    return inter / (area_a + area_b - inter)


def _containment_ratio(inner: dict[str, Any], outer: dict[str, Any]) -> float:
    bi, bo = _bbox(inner), _bbox(outer)
    ix0 = max(bi["x0"], bo["x0"])
    iy0 = max(bi["y0"], bo["y0"])
    ix1 = min(bi["x1"], bo["x1"])
    iy1 = min(bi["y1"], bo["y1"])
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inner_area = max(1e-9, (bi["x1"] - bi["x0"]) * (bi["y1"] - bi["y0"]))
    return (iw * ih) / inner_area


def _looks_like_dialogue(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    plain = re.sub(r"\s+", "", t)
    # 气泡内短问句（如「什么环境？」）或问句前半（如「蚕宝宝喜欢」）
    if len(plain) <= 14 and not _looks_like_lesson_topic_question(t):
        if "？" in t or "?" in t:
            if not any(k in plain for k in ("说一说", "什么情况下", "用到了", "理由")):
                return True
        if plain.endswith("喜欢") or plain.endswith("需要") or plain.endswith("可以"):
            return True
    if _looks_like_body_question(t):
        return False
    if _looks_like_lesson_topic_question(t):
        return False
    if _looks_like_activity_instruction(t) or _looks_like_summary_statement(t):
        return False
    if _looks_like_instructional_body_paragraph(t):
        return False
    if re.search(r"\([a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùü\s]+\)", t):
        if re.search(r"[\u4e00-\u9fff]", plain):
            if (
                _is_mixed_pinyin_hanzi_line(t)
                and "？" not in t
                and "?" not in t
                and not any(m in t for m in _DIALOGUE_MARKERS)
            ):
                return False
            return True
    if any(m in t for m in _DIALOGUE_MARKERS):
        return True
    if t.startswith("“") or t.startswith('"') or t.startswith("「"):
        return True
    if t.startswith("这") and ("是" in t[:6]):
        return True
    if "特征" in t and "植物" in t:
        return True
    if plain.startswith("利用") and any(k in plain for k in ("可以", "能否", "用筛", "分离")):
        return True
    if plain.startswith("我") and any(k in plain for k in ("带来", "拿了", "来说", "发现")):
        return True
    if plain.startswith("我") and len(plain) >= 5:
        if "……" in t or plain.endswith(("，", "。")):
            return True
        if any(k in plain for k in ("带来", "拿了", "来说", "我们", "你们")):
            return True
    if any(plain.startswith(p) for p in ("听说", "为了", "怎样")):
        return True
    if len(plain) <= 16 and plain.endswith("……"):
        return True
    return False


def _looks_like_instructional_body_paragraph(text: str) -> bool:
    """页顶说明段（非气泡）：如「一些物体混合在一起后…可以使用一定的方法…」。"""
    plain = re.sub(r"\s+", "", text or "")
    if len(plain) < 12:
        return False
    if any(m in text for m in ("……", "…", "...")):
        return False
    if plain.startswith(("利用", "我", "他", "她", "这", "那")):
        return False
    cues = ("混合在一起", "保持着", "可以使用", "将它们", "分离", "各自")
    return sum(1 for k in cues if k in plain) >= 2


def _looks_like_pinyin_line(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 80:
        return False
    if re.search(r"[\u4e00-\u9fff]", t):
        return False
    plain = re.sub(r"\s+", "", t)
    if not plain:
        return False
    if _PYNINISH_RE.match(plain):
        return True
    inner = re.sub(r"^[\(\（]+|[\)\）]+$", "", plain)
    return bool(inner and _PYNINISH_RE.match(inner))


def _is_short_scene_label(text: str) -> bool:
    """插图内极短标牌（非说一说/引导句/拼音行）。"""
    plain = re.sub(r"\s+", "", text)
    if not plain or len(plain) > 8:
        return False
    if _looks_like_dialogue(text):
        return False
    if _PAGE_NUM_RE.match(plain):
        return False
    if _looks_like_pinyin_line(text):
        return False
    if _looks_like_say_prompt(text):
        return False
    if _looks_like_body_question(text):
        return False
    if _looks_like_activity_instruction(text):
        return False
    if _looks_like_summary_statement(text):
        return False
    if any(k in plain for k in ("带来", "这是", "铜钱", "兰花", "辣椒", "到了", "你看")):
        return False
    if re.search(r"[\u4e00-\u9fff]", plain):
        return len(plain) <= 4
    return len(plain) <= 10


def _is_scene_label_atom(atom: dict[str, Any]) -> bool:
    if atom.get("atom_type") not in ("text", "title"):
        return False
    plain = re.sub(r"\s+", "", _text(atom))
    if plain in _SCENE_SIGN_WORDS:
        return True
    # 插图内标牌必含「角」；窄横条台词（如「我带来」）不是标牌
    if "角" not in plain:
        return False
    if not _is_short_scene_label(_text(atom)):
        return False
    b = _bbox(atom)
    h, w = _height(atom), _width(atom)
    return h > w * 1.05 or w < 0.09


def _are_peer_gallery_images(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """页内多张学生作品/步骤照竖向或同列排列 → 禁止合并。"""
    if a.get("atom_type") != "image" or b.get("atom_type") != "image":
        return False
    if _is_page_number_atom(a) or _is_page_number_atom(b):
        return False
    aa, ab = _area(a), _area(b)
    if aa < 0.012 or ab < 0.012 or aa > 0.38 or ab > 0.38:
        return False
    ratio = aa / max(ab, 1e-9)
    if ratio < 0.45 or ratio > 2.2:
        return False
    ba, bb = _bbox(a), _bbox(b)
    if abs(ba["x0"] - bb["x0"]) > 0.14:
        return False
    if abs((ba["x1"] - ba["x0"]) - (bb["x1"] - bb["x0"])) > 0.18:
        return False
    if ba["y0"] <= bb["y0"]:
        gap = bb["y0"] - ba["y1"]
    else:
        gap = ba["y0"] - bb["y1"]
    return -0.04 <= gap <= 0.14


def collect_vertical_gallery_image_codes(atoms: list[dict[str, Any]]) -> set[str]:
    images = [
        a
        for a in atoms
        if a.get("atom_type") == "image" and not _is_page_number_atom(a)
    ]
    codes: set[str] = set()
    for i, a in enumerate(images):
        for b in images[i + 1:]:
            if _are_peer_gallery_images(a, b):
                codes.add(str(a["atom_code"]))
                codes.add(str(b["atom_code"]))
    return codes


def collect_image_merge_skip_codes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> set[str]:
    """外包重复框与竖向作品照：不参与插图合并。"""
    codes: set[str] = set()
    codes.update(detect_wrapper_atom_deletes(atoms, protected=protected))
    codes.update(detect_duplicate_image_deletes(atoms, protected=protected))
    codes.update(collect_vertical_gallery_image_codes(atoms))
    return codes


def _is_mixed_pinyin_hanzi_line(text: str) -> bool:
    t = text.strip()
    plain = re.sub(r"\s+", "", t)
    if not plain or len(plain) > 36:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", t)) and bool(re.search(r"[a-zA-Z]", t))


def _subtitle_bar_y_overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    ba, bb = _bbox(a), _bbox(b)
    inter = min(ba["y1"], bb["y1"]) - max(ba["y0"], bb["y0"])
    narrower = min(ba["y1"] - ba["y0"], bb["y1"] - bb["y0"])
    return inter / max(narrower, 1e-9)


def _is_lesson_title_top_band(atom: dict[str, Any]) -> bool:
    """页顶课节大标题色带（与下方活动小标题条分开）。"""
    return _bbox(atom)["y0"] < 0.135


def _is_subtitle_activity_band(atom: dict[str, Any]) -> bool:
    """页顶下方浅蓝活动/小标题条（小人+拼音+汉字）。"""
    b = _bbox(atom)
    if atom.get("atom_type") == "image" and b["x0"] < 0.22 and _area(atom) < 0.12:
        return 0.13 <= b["y0"] < 0.38 and b["y1"] < 0.42
    return 0.13 <= b["y0"] < 0.23 and b["y1"] < 0.25


def _is_instruction_evidence_block(atom: dict[str, Any]) -> bool:
    """插图下方独立说明：拼音行 +「搜集…证据。」等，非气泡台词。"""
    text = _text(atom)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    b = _bbox(atom)
    if b["y0"] < 0.44:
        return False
    if len(lines) >= 2 and _looks_like_pinyin_line(lines[0]):
        hanzi = re.sub(r"\s+", "", lines[-1])
        if hanzi.endswith("。") and any(
            k in hanzi for k in ("搜集", "证据", "填写", "记录", "观察")
        ):
            return True
    plain = re.sub(r"\s+", "", text)
    return bool(
        plain.endswith("。")
        and any(k in plain for k in ("搜集", "证据"))
        and b["y0"] >= 0.46
    )


def _dialogue_should_not_pair_with(
    atom_a: dict[str, Any],
    atom_b: dict[str, Any],
    atoms: list[dict[str, Any]] | None = None,
) -> bool:
    """上下相邻但属于「气泡 + 独立说明块」→ 禁止并组。"""
    top, bottom = (
        (atom_a, atom_b) if _y_center(atom_a) <= _y_center(atom_b) else (atom_b, atom_a)
    )
    top_text = _text(top)
    if (
        _is_task_instruction_line(top, atoms=atoms)
        or _looks_like_activity_instruction(top_text)
        or _looks_like_instructional_body_paragraph(top_text)
    ) and (
        _is_dialogue_bubble_fragment(bottom, atoms)
        or _looks_like_dialogue(_text(bottom))
        or _is_likely_dialogue_bubble_line(bottom, atoms)
    ):
        return True
    if _is_instruction_evidence_block(top) or _is_instruction_evidence_block(bottom):
        return True
    gap = _bbox(bottom)["y0"] - _bbox(top)["y1"]
    if gap > 0.012 and _is_instruction_evidence_block(bottom):
        return True
    bt = _text(bottom)
    blines = [ln.strip() for ln in bt.splitlines() if ln.strip()]
    if gap > 0.012 and len(blines) >= 2 and _looks_like_pinyin_line(blines[0]):
        if re.search(r"[\u4e00-\u9fff]", blines[-1]):
            return True
    return False


def _is_subtitle_bar_band(atom: dict[str, Any]) -> bool:
    """活动条/小标题条出现在页顶、页底或页顶下方的二级条。"""
    b = _bbox(atom)
    y0, h = b["y0"], _height(atom)
    plain = re.sub(r"\s+", "", _text(atom))
    if y0 > 0.68:
        return True
    if y0 < 0.26:
        return True
    # 课节标题正下方活动条（如 y≈0.15–0.22），不含页中对话气泡带（y≈0.28–0.55）
    if 0.12 <= y0 < 0.24 and h < 0.09 and len(plain) >= 6:
        return True
    return False


def _is_likely_dialogue_bubble_line(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]] | None = None,
) -> bool:
    """页中短行更可能是气泡 OCR 碎段，而非活动小标题/说明句。"""
    if atom.get("atom_type") not in ("text", "title"):
        return False
    text = _body_text(atom)
    plain = re.sub(r"\s+", "", text)
    if _is_process_record_block_content(atom, atoms):
        return False
    if re.match(r"^\d+\.", plain):
        return False
    if plain.endswith("，"):
        return True
    if not plain or len(plain) > 24:
        return False
    b = _bbox(atom)
    if b["y0"] < 0.22 or b["y0"] > 0.78:
        return False
    if atoms is not None and _text_beside_subtitle_bar_icon(atom, atoms):
        return False
    if _looks_like_activity_instruction(text):
        return False
    if _looks_like_instructional_body_paragraph(text):
        return False
    if re.search(r"\([a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùü\s]+\)", text) and re.search(
        r"[\u4e00-\u9fff]", plain
    ):
        return True
    if ("？" in text or "?" in text) and len(plain) <= 16:
        if any(k in plain for k in ("什么情况下", "说一说", "用到了", "理由")):
            return False
        if _looks_like_lesson_topic_question(text):
            return False
        return True
    if any(m in text for m in ("……", "…", "...")):
        return True
    if plain.endswith(("吧", "呢", "啊", "呀", "哦")):
        return True
    if re.search(r"[吧呢啊呀哦][！？!?]?$", plain):
        return True
    if any(plain.startswith(p) for p in ("听说", "为了", "怎样", "什么", "哪个", "哪些", "这样的")):
        return True
    if "叫作" in plain or "叫做" in plain:
        return True
    if any(
        plain.endswith(p)
        for p in ("记录", "准", "温度", "来做", "化出来", "观察", "变化")
    ):
        return True
    if "，" in plain or "," in plain:
        if not plain.endswith(("，", "。", "！", "？")):
            return True
    if atoms is not None:
        for other in atoms:
            if other is atom or other.get("atom_type") not in ("text", "title"):
                continue
            if _dialogue_bubble_geometry_match(atom, other):
                return True
    return len(plain) <= 10 and not plain.endswith(("。", "！", "？"))


def _is_section_subheading_text(atom: dict[str, Any]) -> bool:
    """二级小标题条汉字（如「认识植物角中的植物」），非气泡台词。"""
    if atom.get("atom_type") not in ("text", "title"):
        return False
    if _is_likely_dialogue_bubble_line(atom):
        return False
    text = _body_text(atom)
    if not text:
        return False
    plain = re.sub(r"\s+", "", text)
    if not plain or not re.search(r"[\u4e00-\u9fff]", plain):
        return False
    if len(plain) < 6 or len(plain) > 18:
        return False
    if _height(atom) > 0.12 or _width(atom) > 0.88:
        return False
    b = _bbox(atom)
    if b["y0"] > 0.42 and not _is_subtitle_bar_band(atom):
        return False
    if plain.endswith(("，", "。", "！", "？")):
        return False
    if any(m in text for m in ("……", "…", "...")):
        return False
    if "，" in plain and not plain.endswith("，"):
        return False
    if _looks_like_dialogue(text) or _looks_like_body_question(text):
        return False
    # 气泡台词常以「我/他/她」等人称开头，不是小标题
    if plain and plain[0] in "我他她它你们":
        return False
    return True


def _is_subtitle_bar_text_shape(atom: dict[str, Any]) -> bool:
    """小标题/活动条汉字条形态（不含独立拼音行）。"""
    if _is_scene_label_atom(atom):
        return False
    if _is_page_number_atom(atom):
        return False
    text = _text(atom)
    if _is_subtitle_activity_band(atom):
        if _is_mixed_pinyin_hanzi_line(text):
            return True
        if _is_section_subheading_text(atom):
            return True
        plain = re.sub(r"\s+", "", text)
        if 8 <= len(plain) <= 24 and re.search(r"[\u4e00-\u9fff]", plain):
            if plain.endswith(("？", "?")):
                return False
            return True
    if not text or _looks_like_dialogue(text) or _looks_like_body_question(text):
        return False
    if _looks_like_summary_statement(text):
        return False
    if _looks_like_activity_instruction(text) and not _is_subtitle_activity_band(atom):
        return False
    if _looks_like_lesson_topic_question(text):
        return False
    if _height(atom) > 0.22 or _width(atom) > 0.85:
        return False
    plain = re.sub(r"\s+", "", text)
    if plain and plain[0] in "我他她它你时的向而但":
        return False
    if any(k in plain for k in ("带来", "这是", "到了", "你看", "我们发现", "铜钱")):
        return False
    if len(plain) > 32:
        return False
    if _is_mixed_pinyin_hanzi_line(text):
        return True
    if re.search(r"[\u4e00-\u9fff]", plain) and len(plain) <= 18:
        return _bbox(atom)["x0"] >= 0.05
    return False


def _text_beside_subtitle_bar_icon(
    text_atom: dict[str, Any], atoms: list[dict[str, Any]]
) -> bool:
    tb = _bbox(text_atom)
    for icon in atoms:
        if icon is text_atom or icon.get("atom_type") != "image":
            continue
        if _is_page_number_atom(icon) and not _area(icon) < 0.10:
            continue
        ib = _bbox(icon)
        if not _is_subtitle_bar_icon_band(icon):
            continue
        if ib["x0"] > 0.22 or _area(icon) > 0.12:
            continue
        if tb["x0"] < ib["x0"] - 0.04:
            continue
        gap = tb["x0"] - ib["x1"]
        if gap > 0.18:
            continue
        if _subtitle_bar_y_overlap(icon, text_atom) < 0.25:
            continue
        return True
    return False


def _is_subtitle_bar_pinyin_line(
    atom: dict[str, Any], atoms: list[dict[str, Any]] | None = None,
) -> bool:
    text = _text(atom)
    if not _looks_like_pinyin_line(text):
        return False
    if atoms is not None:
        if _text_beside_subtitle_bar_icon(atom, atoms):
            return True
        for other in atoms:
            if other is atom or not (
                _is_subtitle_bar_text_shape(other)
                or _is_section_subheading_text(other)
            ):
                continue
            if (
                _looks_like_summary_statement(_text(other))
                or _looks_like_activity_instruction(_text(other))
                or _looks_like_lesson_topic_question(_text(other))
                or _looks_like_body_question(_text(other))
            ):
                continue
            if not _same_text_column(atom, other, min_overlap=0.28):
                continue
            if abs(_y_center(atom) - _y_center(other)) > 0.08:
                continue
            if _is_subtitle_bar_band(other) or _text_beside_subtitle_bar_icon(
                other, atoms
            ):
                return True
    return False


def _is_subtitle_bar_text(
    atom: dict[str, Any], atoms: list[dict[str, Any]] | None = None,
) -> bool:
    """活动条/小标题条：左侧小人右侧浅蓝条内拼音+汉字（可同框）。"""
    if atom.get("atom_type") not in ("text", "title"):
        return False
    if _is_likely_dialogue_bubble_line(atom, atoms):
        return False
    if _atom_has_lesson_topic_question(atom):
        return False
    if _is_lesson_title_top_band(atom):
        return False
    if _is_subtitle_activity_band(atom):
        text = _text(atom)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if (
            len(lines) >= 2
            and _looks_like_pinyin_line(lines[0])
            and re.search(r"[\u4e00-\u9fff]", lines[-1])
        ):
            return True
    if _is_subtitle_bar_pinyin_line(atom, atoms):
        return True
    if _is_section_subheading_text(atom):
        if atoms is not None and _text_beside_subtitle_bar_icon(atom, atoms):
            return True
        return _is_subtitle_bar_band(atom)
    if not _is_subtitle_bar_text_shape(atom):
        return False
    if atoms is not None and _text_beside_subtitle_bar_icon(atom, atoms):
        return True
    return _is_subtitle_bar_band(atom)


def _is_dialogue_bubble_fragment(
    atom: dict[str, Any], atoms: list[dict[str, Any]] | None = None,
) -> bool:
    """气泡内 OCR 碎段（含无标点断句、字词断行）。"""
    if atom.get("atom_type") not in ("text", "title"):
        return False
    text = _body_text(atom)
    if _is_subtitle_bar_text(atom, atoms) or _is_section_subheading_text(atom):
        return False
    if _is_process_record_block_content(atom, atoms):
        return False
    if _is_task_instruction_line(atom, text=text, atoms=atoms):
        return False
    if _looks_like_activity_instruction(text):
        return False
    # 明确的气泡台词（含对话标点/省略号），不受小标题判断拦截
    if _looks_like_dialogue(text):
        return True
    if _is_scene_label_atom(atom):
        return False
    if _is_instruction_evidence_block(atom):
        return False
    if not text or _looks_like_pinyin_line(text):
        return False
    plain = re.sub(r"\s+", "", text)
    if re.search(r"\([a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùü\s]+\)", text) and re.search(
        r"[\u4e00-\u9fff]", plain
    ):
        return True
    if _looks_like_dialogue(text):
        return True
    plain = re.sub(r"\s+", "", text)
    if not re.search(r"[\u4e00-\u9fff]", plain):
        return False
    if len(plain) > 18:
        return False
    if any(m in text for m in ("……", "…", "...")):
        return True
    if len(plain) <= 12 and plain.endswith(("。", "！", "？")):
        return _bbox(atom)["y0"] > 0.28
    # 气泡内断行碎段：以连接词/续写词开头
    if plain and plain[0] in "时的向是而但却很备个":
        return True
    if _is_likely_dialogue_bubble_line(atom, atoms):
        return True
    return len(plain) <= 10 and not plain.endswith(("。", "！", "？"))


def _icon_pairs_subtitle_bar(
    icon: dict[str, Any], atoms: list[dict[str, Any]]
) -> bool:
    if icon.get("atom_type") != "image":
        return False
    ib = _bbox(icon)
    if ib["x0"] > 0.16 or _area(icon) > 0.10:
        return False
    for a in atoms:
        if a is icon:
            continue
        if not _is_subtitle_bar_text(a, atoms):
            continue
        tb = _bbox(a)
        if tb["x0"] < ib["x0"] - 0.02:
            continue
        gap = tb["x0"] - ib["x1"]
        if gap > 0.16:
            continue
        if _subtitle_bar_y_overlap(icon, a) < 0.30:
            continue
        return True
    return False


def _is_subtitle_bar_icon_band(atom: dict[str, Any]) -> bool:
    """活动小标题条左侧小人图标所在纵向带（含页顶条、页中条与页底条）。"""
    if _is_subtitle_activity_band(atom):
        return True
    b = _bbox(atom)
    if atom.get("atom_type") != "image" or b["x0"] >= 0.22 or _area(atom) >= 0.12:
        return False
    if _area(atom) >= 0.022 and min(_width(atom), _height(atom)) >= 0.075:
        return False
    if b["y0"] > 0.66 and b["y1"] < 0.94:
        return True
    if 0.36 <= b["y0"] < 0.58 and b["y1"] < 0.62:
        return True
    return False


def _is_subtitle_bar_left_icon(
    atom: dict[str, Any], atoms: list[dict[str, Any]] | None = None
) -> bool:
    """活动小标题条左侧圆形小人图标。"""
    if atom.get("atom_type") != "image" or atoms is None:
        return False
    if _is_lesson_title_banner_image(atom):
        return False
    if _is_page_number_atom(atom, atoms) and not _icon_pairs_subtitle_bar(atom, atoms):
        return False
    if not _is_subtitle_bar_icon_band(atom):
        return False
    if _area(atom) > 0.10 or _bbox(atom)["x0"] > 0.22:
        return False
    for a in atoms:
        if a is atom or a.get("atom_type") not in ("text", "title"):
            continue
        if not _is_subtitle_bar_text(a, atoms):
            continue
        ib, tb = _bbox(atom), _bbox(a)
        if tb["x0"] < ib["x0"] - 0.04:
            continue
        if tb["x0"] - ib["x1"] > 0.20:
            continue
        if _subtitle_bar_y_overlap(atom, a) >= 0.25:
            return True
    return False


def _is_activity_bar_icon(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]] | None = None,
) -> bool:
    if atom.get("atom_type") != "image":
        return False
    if _is_lesson_title_banner_image(atom):
        return False
    if _is_subtitle_bar_left_icon(atom, atoms):
        return True
    b = _bbox(atom)
    if _area(atom) > 0.55 or _height(atom) > 0.35:
        return False
    pairs_bar = atoms is not None and _icon_pairs_subtitle_bar(atom, atoms)
    if _is_page_number_atom(atom, atoms) and not pairs_bar:
        return False
    w, h = _width(atom), _height(atom)
    if pairs_bar and _area(atom) < 0.12:
        return _is_subtitle_activity_band(atom) and b["x0"] < 0.22
    if atoms is not None:
        if not _is_subtitle_activity_band(atom):
            return False
        if b["x0"] < 0.22 and _area(atom) < 0.12:
            for a in atoms:
                if a is atom:
                    continue
                if not _is_subtitle_bar_text_shape(a):
                    continue
                tb = _bbox(a)
                gap = tb["x0"] - b["x1"]
                if gap <= 0.18 and _subtitle_bar_y_overlap(atom, a) >= 0.25:
                    return True
    if b["x1"] > 0.2 or w > 0.16 or h > 0.18:
        return False
    if atom.get("atom_type") == "image" and _area(atom) < 0.04:
        if b["x0"] < 0.14:
            return True
    if 0.015 < h < 0.16 and w < 0.14 and b["x0"] < 0.12:
        return True
    return False


def _is_activity_bar_text(
    atom: dict[str, Any], atoms: list[dict[str, Any]] | None = None,
) -> bool:
    return _is_subtitle_bar_text(atom, atoms)


def collect_dialogue_codes(atoms: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for a in atoms:
        if _looks_like_dialogue(_text(a)):
            out.add(str(a["atom_code"]))
    return out


def _is_sidebar_mascot_image(atom: dict[str, Any]) -> bool:
    """页边旁批小人/指南车等小插图，非页码装饰。"""
    if atom.get("atom_type") != "image":
        return False
    b = _bbox(atom)
    if _x_center(atom) < 0.72:
        return False
    if _area(atom) >= 0.14 or _height(atom) > 0.22:
        return False
    return b["y0"] > 0.38 and b["y1"] < 0.94


def _is_instructional_inset_image(
    atom: dict[str, Any], atoms: list[dict[str, Any]] | None = None
) -> bool:
    """页内实验/步骤插图（培养皿、盐结晶等小照片），非页角装饰。"""
    if atom.get("atom_type") != "image":
        return False
    if _is_sidebar_mascot_image(atom):
        return False
    b = _bbox(atom)
    area = _area(atom)
    w, h = _width(atom), _height(atom)
    if area < 0.012:
        return False
    if area >= 0.010 and min(w, h) >= 0.065 and max(w, h) >= 0.09:
        return True
    if area >= 0.018 and min(w, h) >= 0.08 and max(w, h) >= 0.11:
        return True
    if b["y0"] < 0.68 and 0.06 <= b["x0"] and b["x1"] <= 0.94 and area >= 0.015:
        return True
    if atoms:
        for other in atoms:
            if other is atom or other.get("atom_type") not in ("text", "title"):
                continue
            ob = _bbox(other)
            plain = re.sub(r"\s+", "", _text(other))
            if len(plain) < 4:
                continue
            x_inter = min(b["x1"], ob["x1"]) - max(b["x0"], ob["x0"])
            min_w = max(min(w, ob["x1"] - ob["x0"]), 1e-9)
            if x_inter / min_w < 0.12:
                continue
            y_gap = min(abs(ob["y0"] - b["y1"]), abs(b["y0"] - ob["y1"]))
            if y_gap <= 0.16:
                return True
    return False


def _is_left_column_showcase_image(
    atom: dict[str, Any], atoms: list[dict[str, Any]]
) -> bool:
    """页中左侧竖向作品展示照（如力与形变页底三根彩泥），非页角装饰。"""
    if atom.get("atom_type") != "image":
        return False
    b = _bbox(atom)
    if b["x0"] >= 0.36 or b["y0"] < 0.48 or b["y1"] > 0.92:
        return False
    area = _area(atom)
    if area < 0.006 or area > 0.14:
        return False
    for other in atoms:
        if other is atom or other.get("atom_type") != "image":
            continue
        ob = _bbox(other)
        x_inter = min(b["x1"], ob["x1"]) - max(b["x0"], ob["x0"])
        narrower = min(b["x1"] - b["x0"], ob["x1"] - ob["x0"])
        if narrower <= 0 or x_inter / narrower < 0.42:
            continue
        if ob["y1"] <= b["y0"] + 0.015 and 0.02 <= b["y0"] - ob["y1"] <= 0.22:
            return True
    return False


def _is_page_number_atom(
    atom: dict[str, Any], atoms: list[dict[str, Any]] | None = None
) -> bool:
    """页脚/页角页码数字或桌椅装饰小图块。"""
    text = _text(atom)
    plain = re.sub(r"\s+", "", text)
    b = _bbox(atom)
    if _PAGE_NUM_RE.match(plain):
        if b["y0"] >= 0.65 and _height(atom) <= 0.18 and _width(atom) <= 0.25:
            return True
    if atom.get("atom_type") == "image":
        if _is_sidebar_mascot_image(atom):
            return False
        if atoms is not None and _is_instructional_inset_image(atom, atoms):
            return False
        if atoms is not None and _is_left_column_showcase_image(atom, atoms):
            return False
        area = _area(atom)
        if area >= 0.09:
            return False
        if b["y0"] >= 0.70 and b["x0"] < 0.25 and area < 0.10:
            return True
        if b["y0"] >= 0.70 and b["x1"] > 0.85 and area < 0.08:
            return True
        if b["y0"] >= 0.82 and b["x0"] < 0.22 and area < 0.08:
            return True
        if b["y0"] >= 0.88 and b["x1"] > 0.88 and area < 0.06:
            return True
    return False


def detect_page_number_codes(atoms: list[dict[str, Any]]) -> list[str]:
    """页脚页码、页角装饰数字与小图标 → 一律删除，不参与合并。"""
    out: list[str] = []
    sorted_codes = sorted(atoms, key=lambda x: str(x.get("atom_code", "")))
    last_code = str(sorted_codes[-1]["atom_code"]) if sorted_codes else ""

    for a in atoms:
        code = str(a["atom_code"])
        if _is_page_number_atom(a, atoms):
            if a.get("atom_type") == "image" and _icon_pairs_subtitle_bar(a, atoms):
                continue
            out.append(code)
            continue
        plain = re.sub(r"\s+", "", _text(a))
        b = _bbox(a)
        if code == last_code and _PAGE_NUM_RE.match(plain) and b["y0"] >= 0.65:
            out.append(code)

    return out


def strip_page_numbers_from_curate_plan(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """从合并组剔除页码原子，并强制加入 delete_codes。"""
    page_codes = set(detect_page_number_codes(atoms))
    if not page_codes:
        return plan

    merge_out: list[dict[str, Any]] = []
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip() and str(c).strip() not in page_codes
        ]
        if len(codes) < 2:
            continue
        merge_out.append({**g, "atom_codes": codes})

    delete_set = set(plan.get("delete_codes") or [])
    delete_set.update(page_codes)
    for g in merge_out:
        for c in g.get("atom_codes") or []:
            delete_set.discard(c)

    warnings = list(plan.get("warnings") or [])
    if page_codes:
        warnings.append(f"规则：页码/页角装饰 {len(page_codes)} 个仅删除、禁止合并")

    return {
        "merge_groups": merge_out,
        "delete_codes": sorted(delete_set),
        "warnings": warnings,
    }


def strip_invalid_illustration_merges_from_plan(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """剔除错误合并：竖向作品照、外包框、插图+引导文字。"""
    gallery = collect_vertical_gallery_image_codes(atoms)
    wrapper_noise = collect_image_merge_skip_codes(atoms, protected=set())
    by_code = {str(a["atom_code"]): a for a in atoms}

    merge_out: list[dict[str, Any]] = []
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        gallery_hits = [c for c in codes if c in gallery]
        if len(gallery_hits) >= 2:
            continue
        if any(c in wrapper_noise for c in codes):
            continue
        img_codes = [
            c for c in codes if by_code.get(c, {}).get("atom_type") == "image"
        ]
        txt_codes = [
            c
            for c in codes
            if by_code.get(c, {}).get("atom_type") in ("text", "title")
        ]
        if img_codes and txt_codes:
            bad_text = [
                c
                for c in txt_codes
                if c in by_code
                and not _is_subtitle_bar_text(by_code[c], atoms)
                and not _is_definite_subtitle_module_atom(by_code[c], atoms)
                and not _is_scene_label_atom(by_code[c])
                and (
                    _looks_like_say_prompt(_text(by_code[c]))
                    or _looks_like_summary_statement(_text(by_code[c]))
                    or _looks_like_body_question(_text(by_code[c]))
                    or _looks_like_activity_instruction(_text(by_code[c]))
                    or _atom_has_lesson_topic_question(by_code[c])
                    or _text_has_mixed_lesson_topic_and_subtitle(_text(by_code[c]))
                    or _is_prompt_text_line_candidate(by_code[c])
                )
            ]
            if bad_text:
                continue
        merge_out.append({**g, "atom_codes": codes})

    warnings = list(plan.get("warnings") or [])
    dropped = len(plan.get("merge_groups") or []) - len(merge_out)
    if dropped:
        warnings.append(
            f"规则：剔除 {dropped} 组错误插图合并（作品照/外包框/引导文字）"
        )

    return {
        "merge_groups": merge_out,
        "delete_codes": list(plan.get("delete_codes") or []),
        "warnings": warnings,
    }


def restrict_curate_deletes_to_page_numbers(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """AI 整理：仅删除页码/页角装饰，不删插图与文字（避免不可恢复丢失）。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    page_codes = set(detect_page_number_codes(atoms))
    safe_deletes = {
        c
        for c in page_codes
        if not (
            (a := by_code.get(c))
            and a.get("atom_type") == "image"
            and _is_instructional_inset_image(a, atoms)
        )
    }
    raw_deletes = [str(c).strip() for c in (plan.get("delete_codes") or []) if str(c).strip()]
    dropped = [c for c in raw_deletes if c not in safe_deletes]
    warnings = list(plan.get("warnings") or [])
    if dropped:
        warnings.append(
            f"规则：AI 整理仅删页码，已忽略 {len(dropped)} 个非页码删除项"
        )
    skipped_illus = page_codes - safe_deletes
    if skipped_illus:
        warnings.append(
            f"规则：保留 {len(skipped_illus)} 个实验/步骤插图（非页角装饰）"
        )
    return {
        **plan,
        "delete_codes": sorted(safe_deletes),
        "warnings": warnings,
    }


def strip_image_from_text_merge_groups(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """禁止把场景插图并入对话/文字合并组。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    merge_out: list[dict[str, Any]] = []
    dropped = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        has_img = any(by_code.get(c, {}).get("atom_type") == "image" for c in codes)
        has_txt = any(
            by_code.get(c, {}).get("atom_type") in ("text", "title") for c in codes
        )
        if has_img and has_txt:
            txt_atoms = [
                by_code[c]
                for c in codes
                if c in by_code and by_code[c].get("atom_type") in ("text", "title")
            ]
            img_atoms = [
                by_code[c]
                for c in codes
                if c in by_code and by_code[c].get("atom_type") == "image"
            ]
            allowed_txt = txt_atoms and all(
                _is_subtitle_bar_text(a, atoms)
                or _is_scene_label_atom(a)
                or _looks_like_pinyin_line(_text(a))
                for a in txt_atoms
            )
            allowed_bar = allowed_txt and img_atoms and all(
                _is_title_bar_image(a, atoms) for a in img_atoms
            )
            allowed_scene = allowed_txt and img_atoms and all(
                _is_scene_label_atom(a) for a in txt_atoms
            ) and any(
                _area(a) > 0.06 and _height(a) > 0.12 for a in img_atoms
            )
            if allowed_bar or allowed_scene:
                merge_out.append({**g, "atom_codes": codes})
                continue
            dropped += 1
            continue
        merge_out.append({**g, "atom_codes": codes})
    warnings = list(plan.get("warnings") or [])
    if dropped:
        warnings.append(f"规则：已剔除 {dropped} 组插图+文字错误合并")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def strip_sidebar_mascot_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """旁批小人/指南车插图单独保留，禁止并入主场景图。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    merge_out: list[dict[str, Any]] = []
    dropped = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        imgs = [
            by_code[c]
            for c in codes
            if c in by_code and by_code[c].get("atom_type") == "image"
        ]
        if len(imgs) >= 2 and any(_is_sidebar_mascot_image(a) for a in imgs):
            dropped += 1
            continue
        merge_out.append({**g, "atom_codes": codes})
    warnings = list(plan.get("warnings") or [])
    if dropped:
        warnings.append(f"规则：已保留 {dropped} 组旁批小人插图（禁止并入主图）")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def strip_cross_column_text_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """左右不同气泡/不同栏的碎段不得合并为一行。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    merge_out: list[dict[str, Any]] = []
    dropped = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        atoms_in = [by_code[c] for c in codes if c in by_code]
        if len(atoms_in) < 2:
            continue
        if any(a.get("atom_type") == "image" for a in atoms_in):
            merge_out.append({**g, "atom_codes": codes})
            continue
        xs = [_x_center(a) for a in atoms_in]
        if max(xs) - min(xs) > 0.20:
            if _is_body_text_wrap_merge_group(atoms_in, atoms):
                merge_out.append({**g, "atom_codes": codes})
                continue
            dropped += 1
            continue
        merge_out.append({**g, "atom_codes": codes})
    warnings = list(plan.get("warnings") or [])
    if dropped:
        warnings.append(f"规则：已剔除 {dropped} 组跨列错误文字合并")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def strip_activity_bar_polluted_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """活动条/小标题条仅合并左侧图标+同条拼音汉字，剔除误夹带的对话或插图。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    merge_out: list[dict[str, Any]] = []
    dropped = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        has_bar = any(
            c in by_code
            and (
                _is_activity_bar_icon(by_code[c])
                or _is_activity_bar_text(by_code[c])
            )
            for c in codes
        )
        if not has_bar:
            merge_out.append({**g, "atom_codes": codes})
            continue
        bar_atom_codes = {
            c
            for c in codes
            if c in by_code
            and (
                _is_activity_bar_icon(by_code[c], atoms)
                or _is_activity_bar_text(by_code[c], atoms)
            )
        }
        icons = [
            by_code[c]
            for c in codes
            if c in by_code and _is_activity_bar_icon(by_code[c], atoms)
        ]
        for c in codes:
            if c in bar_atom_codes or c not in by_code:
                continue
            if _looks_like_pinyin_line(_text(by_code[c])) and icons:
                if any(
                    _subtitle_bar_y_overlap(icon, by_code[c]) >= 0.20 for icon in icons
                ):
                    bar_atom_codes.add(c)
        pollutants = [c for c in codes if c in by_code and c not in bar_atom_codes]
        if pollutants:
            kept = [c for c in codes if c in bar_atom_codes]
            if len(kept) >= 2:
                merge_out.append(
                    {
                        **g,
                        "atom_codes": kept,
                        "reason": "活动条：左侧小人+拼音+条内汉字合并",
                    }
                )
            dropped += 1
            continue
        merge_out.append({**g, "atom_codes": codes})
    warnings = list(plan.get("warnings") or [])
    if dropped:
        warnings.append(f"规则：已修正 {dropped} 组活动条误合并（剔除对话/插图）")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def strip_subtitle_dialogue_cross_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """小标题条不得与下方对话气泡合并为一组。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    merge_out: list[dict[str, Any]] = []
    dropped = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        atoms_in = [by_code[c] for c in codes if c in by_code]
        has_subtitle = any(
            _is_subtitle_bar_text(a, atoms) or _is_section_subheading_text(a)
            for a in atoms_in
        )
        has_dialogue = any(
            _looks_like_dialogue(_text(a))
            or _is_dialogue_bubble_fragment(a, atoms)
            for a in atoms_in
            if a.get("atom_type") in ("text", "title")
        )
        if has_subtitle and has_dialogue:
            kept = [
                c
                for c in codes
                if c in by_code
                and (
                    _is_activity_bar_icon(by_code[c], atoms)
                    or _is_subtitle_bar_text(by_code[c], atoms)
                    or _is_section_subheading_text(by_code[c])
                    or _looks_like_pinyin_line(_text(by_code[c]))
                )
            ]
            if len(kept) >= 2:
                merge_out.append(
                    {
                        **g,
                        "atom_codes": kept,
                        "reason": "活动条：左侧小人+拼音+条内汉字合并",
                    }
                )
            dropped += 1
            continue
        merge_out.append({**g, "atom_codes": codes})
    warnings = list(plan.get("warnings") or [])
    if dropped:
        warnings.append(f"规则：已拆分 {dropped} 组小标题与对话错误合并")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def inject_mandatory_curate_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """补全必须合并：小标题行、单气泡内文字碎段（sanitize 后兜底）。"""
    delete_codes = set(plan.get("delete_codes") or [])
    mandatory: list[dict[str, Any]] = []
    mandatory.extend(detect_dialogue_merge_groups(atoms))
    mandatory.extend(
        detect_activity_bar_merge_groups(atoms, protected=delete_codes)
    )
    mandatory.extend(
        detect_lesson_title_bar_merge_groups(atoms, protected=delete_codes)
    )

    merge_out: list[dict[str, Any]] = list(plan.get("merge_groups") or [])
    used_codes: set[str] = set()
    for g in merge_out:
        used_codes.update(g.get("atom_codes") or [])

    added = 0
    for g in mandatory:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip() and str(c).strip() not in delete_codes
        ]
        if len(codes) < 2:
            continue
        code_set = set(codes)
        if code_set.issubset(used_codes):
            continue
        overlap = used_codes.intersection(code_set)
        if overlap:
            trimmed: list[dict[str, Any]] = []
            for mg in merge_out:
                kept = [
                    str(c).strip()
                    for c in (mg.get("atom_codes") or [])
                    if str(c).strip() and str(c).strip() not in code_set
                ]
                if len(kept) >= 2:
                    trimmed.append({**mg, "atom_codes": kept})
            merge_out = trimmed
            used_codes = set()
            for mg in merge_out:
                used_codes.update(mg.get("atom_codes") or [])
            if code_set.issubset(used_codes):
                continue
        merge_out.append({**g, "atom_codes": codes})
        used_codes.update(codes)
        added += 1

    warnings = list(plan.get("warnings") or [])
    if added:
        warnings.append(f"规则：补全 {added} 组必须合并（小标题行/单气泡文字）")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def sanitize_atom_curate_plan(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    plan = strip_page_numbers_from_curate_plan(plan, atoms)
    plan = strip_invalid_illustration_merges_from_plan(plan, atoms)
    plan = strip_image_from_text_merge_groups(plan, atoms)
    plan = strip_sidebar_mascot_merges(plan, atoms)
    plan = strip_activity_bar_polluted_merges(plan, atoms)
    plan = strip_subtitle_dialogue_cross_merges(plan, atoms)
    plan = strip_cross_column_text_merges(plan, atoms)
    plan = restrict_curate_deletes_to_page_numbers(plan, atoms)
    plan = inject_mandatory_curate_merges(plan, atoms)
    return strip_subtitle_dialogue_cross_merges(plan, atoms)


def _looks_like_body_question(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    plain = re.sub(r"\s+", "", t)
    # 气泡内短问句（如「什么环境？」）不是页内引导问句
    if ("？" in t or "?" in t) and len(plain) <= 14:
        if not _looks_like_lesson_topic_question(t):
            if not any(k in plain for k in ("说一说", "什么情况下", "用到了", "理由")):
                return False
    if "？" in t or "?" in t:
        return True
    if "说一说" in t or "理由" in t:
        return True
    if "什么情况下" in t or "用到了" in t:
        return True
    return False


def _looks_like_lesson_topic_question(text: str) -> bool:
    """标题下短问句：物体是怎么变形的？"""
    t = text.strip()
    if not t:
        return False
    candidates = [t]
    if "\n" in t:
        candidates = [
            ln.strip()
            for ln in t.splitlines()
            if ln.strip() and re.search(r"[\u4e00-\u9fff]", ln)
        ] + [t]
    for c in candidates:
        plain = re.sub(r"\s+", "", c)
        if not plain or len(plain) > 28:
            continue
        if "？" not in c and "?" not in c:
            continue
        if any(k in plain for k in ("是怎么", "是怎样", "什么是", "为什么", "哪些", "怎样")):
            return True
    return False


def _atom_has_lesson_topic_question(atom: dict[str, Any]) -> bool:
    t = _text(atom)
    for ln in t.splitlines():
        if _looks_like_lesson_topic_question(ln):
            return True
    return _looks_like_lesson_topic_question(t)


def _text_has_mixed_lesson_topic_and_subtitle(text: str) -> bool:
    """单框内同时含引导问句与小标题条汉字（如力与形变 A001-002）。"""
    hanzi = [
        re.sub(r"\s+", "", ln)
        for ln in text.splitlines()
        if ln.strip()
        and re.search(r"[\u4e00-\u9fff]", ln)
        and not ln.strip().startswith("[")
    ]
    if len(hanzi) < 2:
        return False
    has_topic = any(
        ("？" in ln or "?" in ln)
        and any(k in ln for k in ("是怎么", "是怎样", "什么是", "为什么", "哪些", "怎样"))
        for ln in hanzi
    )
    has_subtitle = any(
        6 <= len(ln) <= 20
        and not ln.endswith(("？", "?", "。", "，", "！", "!"))
        and not any(k in ln for k in ("是怎么", "是怎样", "说一说"))
        for ln in hanzi
    )
    return has_topic and has_subtitle


def _spans_lesson_title_and_subtitle_band(atom: dict[str, Any]) -> bool:
    """OCR 框纵向跨越页顶大标题色带与下方活动小标题条（如混合与分离页左条）。"""
    b = _bbox(atom)
    return b["y0"] < 0.135 and b["y1"] > 0.20 and _height(atom) > 0.14


def _text_has_mixed_lesson_title_and_subtitle_bar(text: str) -> bool:
    """单框内同时含课节大标题与活动小标题（如「3 混合与分离」+「分离盐和芝麻」，无引导问句）。"""
    if _text_has_mixed_lesson_topic_and_subtitle(text):
        return True
    hanzi = [
        re.sub(r"\s+", "", ln)
        for ln in text.splitlines()
        if ln.strip()
        and re.search(r"[\u4e00-\u9fff]", ln)
        and not ln.strip().startswith("[")
    ]
    if len(hanzi) < 2:
        return False
    first = hanzi[0]
    has_title = bool(
        re.match(r"^\d", first)
        or (len(first) <= 14 and not first.endswith(("？", "?", "。", "，")))
    )
    has_subtitle = any(
        5 <= len(ln) <= 24
        and ln != first
        and not ln.endswith(("？", "?", "。", "，", "！", "!"))
        and not any(k in ln for k in ("是怎么", "是怎样", "说一说"))
        for ln in hanzi[1:]
    )
    return has_title and has_subtitle


def _is_inline_pinyin_parenthetical(atom: dict[str, Any]) -> bool:
    """条内汉字间括号拼音标注，如「芝 ( zhī ) 麻」。"""
    if atom.get("atom_type") not in ("text", "title"):
        return False
    t = _text(atom).strip()
    if not t:
        return False
    return bool(re.fullmatch(r"[\(（][a-zA-Z\u0100-\u024f\s·'’]+[\)）]", t))


def _is_subtitle_bar_trailing_hanzi_fragment(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]],
    anchor: dict[str, Any],
) -> bool:
    """活动条汉字被括号拼音拆成尾字碎框（如单独的「麻」）。"""
    if atom.get("atom_type") not in ("text", "title"):
        return False
    plain = re.sub(r"\s+", "", _text(atom))
    if not plain or len(plain) > 3 or not re.search(r"[\u4e00-\u9fff]", plain):
        return False
    if _subtitle_bar_y_overlap(anchor, atom) < 0.25:
        return False
    ab = _bbox(atom)
    for other in atoms:
        if other is atom:
            continue
        if other.get("atom_type") not in ("text", "title"):
            continue
        op = re.sub(r"\s+", "", _text(other))
        if len(op) < 4 or not re.search(r"[\u4e00-\u9fff]", op):
            continue
        if not _is_subtitle_activity_band(other) and _subtitle_bar_y_overlap(
            anchor, other
        ) < 0.25:
            continue
        ob = _bbox(other)
        if ob["x1"] <= ab["x0"] + 0.06 and abs(_y_center(atom) - _y_center(other)) < 0.05:
            return True
    return False


def _looks_like_say_prompt(text: str) -> bool:
    return "说一说" in (text or "").strip()


def _looks_like_activity_instruction(text: str) -> bool:
    t = (text or "").strip()
    plain = re.sub(r"\s+", "", t)
    if not t:
        return False
    if any(plain.startswith(p) for p in ("为了", "听说", "这样的", "怎样才能")):
        return False
    if "叫作" in plain or "叫做" in plain:
        return False
    strong = (
        "查阅资料",
        "向他人请教",
        "与同学",
        "交流",
        "讨论",
        "汇报",
        "分享",
        "观点",
        "试一试",
        "填写",
        "查阅",
        "请教",
    )
    if any(k in t for k in strong):
        return True
    weak = (
        "观察",
        "记录",
        "实验",
        "挤压",
        "拉伸",
        "捏一捏",
        "摸一摸",
    )
    if any(k in t for k in weak):
        return plain.endswith("。") or "请" in t or len(plain) > 14
    return False


def _is_task_instruction_line(
    atom: dict[str, Any],
    text: str | None = None,
    atoms: list[dict[str, Any]] | None = None,
) -> bool:
    """页内任务说明句（非气泡台词），如「查阅资料或向他人请教怎样养蚕。」。"""
    t = (text if text is not None else _text(atom)).strip()
    plain = re.sub(r"\s+", "", t)
    if not plain or len(plain) > 48:
        return False
    if re.match(r"^\d+\.\s*", t):
        return False
    if plain.endswith("，"):
        return False
    if _looks_like_activity_instruction(t):
        return True
    if _looks_like_instructional_body_paragraph(t):
        return True
    if _is_likely_dialogue_bubble_line(atom, atoms):
        return False
    if _looks_like_dialogue(t):
        return False
    if _looks_like_say_prompt(t):
        return True
    if plain.endswith(("。", ".")) and not plain.endswith("？"):
        if any(
            k in plain
            for k in (
                "查阅资料",
                "向他人",
                "请教",
                "查阅",
                "填写",
                "试一试",
                "完成",
                "准备",
                "养蚕",
            )
        ):
            return True
    return False


def _looks_like_summary_statement(text: str) -> bool:
    t = (text or "").strip()
    plain = re.sub(r"\s+", "", t)
    if not plain or len(plain) > 40:
        return False
    if "可以改变" in plain or "能够改变" in plain:
        return True
    if plain.endswith("。") and ("力" in plain or "形状" in plain):
        return len(plain) <= 32
    return False


def _is_prompt_text_line_candidate(atom: dict[str, Any]) -> bool:
    """引导问句、说一说、活动说明、总结句等需拼音+汉字合并的文本行。"""
    if _is_likely_dialogue_bubble_line(atom):
        return False
    t = _text(atom)
    if not t or _looks_like_dialogue(t):
        return False
    b = _bbox(atom)
    if _height(atom) > 0.16 or _width(atom) > 0.92:
        return False
    if _looks_like_lesson_topic_question(t) and b["y0"] < 0.28:
        return True
    if _atom_has_lesson_topic_question(atom) and b["y0"] < 0.28:
        return True
    if _looks_like_pinyin_line(t):
        if 0.12 <= b["y0"] <= 0.58:
            return True
        if b["y0"] > 0.65:
            return True
    if _looks_like_say_prompt(t) and b["y0"] >= 0.06:
        return True
    if _looks_like_activity_instruction(t) and 0.14 <= b["y0"] <= 0.58:
        return True
    if _looks_like_summary_statement(t):
        return True
    is_guide = _looks_like_body_question(t)
    is_pinyin = _looks_like_pinyin_line(t)
    if is_guide and b["y0"] >= 0.45:
        return True
    if b["y0"] > 0.72 and not _is_activity_bar_icon(atom):
        return is_pinyin or is_guide
    return False


def _is_demo_photo_grid_layout(candidates: list[dict[str, Any]]) -> bool:
    """多张步骤/展示示意照（2×2 或 3+ 张）→ 禁止合并。"""
    if len(candidates) < 3:
        return False
    ys = [_y_center(a) for a in candidates]
    if max(ys) - min(ys) > 0.085:
        return True
    row_bands: list[list[dict[str, Any]]] = []
    for a in sorted(candidates, key=_y_center):
        placed = False
        for band in row_bands:
            if abs(_y_center(a) - _y_center(band[0])) <= 0.07:
                band.append(a)
                placed = True
                break
        if not placed:
            row_bands.append([a])
    multi_row = sum(1 for b in row_bands if len(b) >= 2) >= 2
    if multi_row:
        return True
    return len(candidates) >= 3 and len(row_bands) >= 2


def _is_lesson_title_banner_image(atom: dict[str, Any]) -> bool:
    """顶部课节标题色块底图（宽条背景，非场景插图）。"""
    if atom.get("atom_type") != "image":
        return False
    if _is_page_number_atom(atom):
        return False
    if _spans_lesson_title_and_subtitle_band(atom):
        return False
    b = _bbox(atom)
    if b["y0"] > 0.14:
        return False
    w, h = _width(atom), _height(atom)
    if w < 0.28 or h > 0.18 or h < 0.035:
        return False
    return b["x0"] < 0.16


def _is_title_bar_image(atom: dict[str, Any], atoms: list[dict[str, Any]] | None = None) -> bool:
    return (
        _is_lesson_title_banner_image(atom)
        or (
            _is_activity_bar_icon(atom, atoms)
            and _is_subtitle_activity_band(atom)
        )
        or (
            atom.get("atom_type") == "image"
            and _bbox(atom)["y0"] < 0.20
            and _bbox(atom)["x0"] < 0.18
            and _area(atom) < 0.10
            and not _is_page_number_atom(atom)
            and not _is_lesson_title_banner_image(atom)
        )
    )


def _is_lesson_title_text(atom: dict[str, Any]) -> bool:
    """顶部课节标题条内文字（序号圈旁汉字，非活动条）。"""
    b = _bbox(atom)
    if b["y0"] > 0.14 or _height(atom) > 0.12:
        return False
    if _is_subtitle_activity_band(atom):
        return False
    text = _text(atom)
    if not text or _looks_like_dialogue(text) or _looks_like_body_question(text):
        return False
    if _looks_like_summary_statement(text) or _looks_like_activity_instruction(text):
        return False
    if _looks_like_lesson_topic_question(text):
        return False
    if atom.get("atom_type") == "title":
        return True
    if _is_mixed_pinyin_hanzi_line(text):
        return True
    if _looks_like_pinyin_line(text):
        return True
    plain = re.sub(r"\s+", "", text)
    if _PAGE_NUM_RE.match(plain) and len(plain) <= 2:
        return True
    return b["x0"] < 0.55 and len(plain) <= 12


def _are_horizontal_neighbors(left: dict[str, Any], right: dict[str, Any]) -> bool:
    la, rb = _bbox(left), _bbox(right)
    if rb["x0"] < la["x0"]:
        left, right = right, left
        la, rb = _bbox(left), _bbox(right)
    gap = rb["x0"] - la["x1"]
    if gap > 0.10:
        return False
    x_inter = min(la["x1"], rb["x1"]) - max(la["x0"], rb["x0"])
    narrower = min(la["x1"] - la["x0"], rb["x1"] - rb["x0"])
    if gap <= 0.06:
        return True
    return x_inter / max(narrower, 1e-9) > 0.05


def _is_small_illustration_candidate(atom: dict[str, Any]) -> bool:
    if _looks_like_dialogue(_text(atom)):
        return False
    plain = re.sub(r"\s+", "", _text(atom))
    if _PAGE_NUM_RE.match(plain):
        return False
    b = _bbox(atom)
    w, h = _width(atom), _height(atom)
    area = _area(atom)
    if atom.get("atom_type") == "image":
        return 0.012 < area < 0.28 and h < 0.38 and w < 0.55
    return 0.02 < area < 0.28 and h < 0.35 and w < 0.52 and b["y0"] > 0.50


def _text_column_overlap_ratio(a: dict[str, Any], b: dict[str, Any]) -> float:
    ba, bb = _bbox(a), _bbox(b)
    x_inter = min(ba["x1"], bb["x1"]) - max(ba["x0"], bb["x0"])
    narrower = min(ba["x1"] - ba["x0"], bb["x1"] - bb["x0"])
    return x_inter / max(narrower, 1e-9)


def _same_text_column(a: dict[str, Any], b: dict[str, Any], *, min_overlap: float = 0.32) -> bool:
    return _text_column_overlap_ratio(a, b) >= min_overlap


def _is_pinyin_hanzi_vertical_pair(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if not _same_text_column(a, b, min_overlap=0.28):
        return False
    if abs(_y_center(a) - _y_center(b)) > 0.075:
        return False
    ta, tb = _text(a), _text(b)
    ha = bool(re.search(r"[\u4e00-\u9fff]", ta))
    hb = bool(re.search(r"[\u4e00-\u9fff]", tb))
    pa = _looks_like_pinyin_line(ta)
    pb = _looks_like_pinyin_line(tb)
    return (pa and hb) or (pb and ha)


def _dialogue_wrap_should_stay_separate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """对话框被插图隔开的左右断句，不合并为一个对话原子。"""
    left, right = (a, b) if _x_center(a) <= _x_center(b) else (b, a)
    if abs(_y_center(left) - _y_center(right)) > 0.05:
        return False
    bl, br = _bbox(left), _bbox(right)
    gap = br["x0"] - bl["x1"]
    if gap < 0.05:
        return False
    lt = re.sub(r"\s+", "", _text(left))
    rt = re.sub(r"\s+", "", _text(right))
    if rt.startswith("的") and len(lt) <= 6 and not lt.endswith(("？", "。", "！", "；")):
        if len(rt) > 1 and rt[1] not in "有是这在那我他她它":
            return True
    if lt.endswith("每天") and rt.startswith("的"):
        return True
    return False


def _dialogue_bubble_geometry_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """仅按位置判断两行是否可能在同一气泡（不含说明句/跨栏护栏）。"""
    if _dialogue_wrap_should_stay_separate(a, b):
        return False
    ay, by = _y_center(a), _y_center(b)
    ax, bx = _x_center(a), _x_center(b)
    y_gap = abs(ay - by)
    x_overlap = _text_column_overlap_ratio(a, b)

    if y_gap <= 0.065:
        if x_overlap >= 0.38:
            return True
        if abs(ax - bx) < 0.14:
            return True
        left, right = (a, b) if ax <= bx else (b, a)
        bl, br = _bbox(left), _bbox(right)
        horiz_gap = br["x0"] - bl["x1"]
        if (
            horiz_gap < 0.08
            and abs(ax - bx) < 0.26
            and re.search(r"[\u4e00-\u9fff]", _body_text(left))
            and re.search(r"[\u4e00-\u9fff]", _body_text(right))
        ):
            return True
        return False

    if y_gap > 0.16:
        return False

    plain_a = re.sub(r"\s+", "", _body_text(a))
    plain_b = re.sub(r"\s+", "", _body_text(b))
    if (
        re.search(r"[\u4e00-\u9fff]", plain_a)
        and re.search(r"[\u4e00-\u9fff]", plain_b)
        and len(plain_a) <= 18
        and len(plain_b) <= 18
    ):
        if x_overlap >= 0.18:
            return True
        if abs(ax - bx) < 0.22:
            return True
        if abs(ax - bx) < 0.12 and y_gap <= 0.10:
            return True

    return False


def _same_dialogue_bubble(
    a: dict[str, Any],
    b: dict[str, Any],
    atoms: list[dict[str, Any]] | None = None,
) -> bool:
    if _dialogue_should_not_pair_with(a, b, atoms):
        return False
    return _dialogue_bubble_geometry_match(a, b)


def _expand_dialogue_pool(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seeds = [a for a in atoms if _is_dialogue_bubble_fragment(a, atoms)]
    pool = list(seeds)
    seen = {str(a["atom_code"]) for a in pool}
    for a in atoms:
        ac = str(a["atom_code"])
        if ac in seen:
            continue
        if a.get("atom_type") == "image":
            continue
        t = _body_text(a)
        if not t or _looks_like_pinyin_line(t):
            continue
        if _is_subtitle_bar_text(a, atoms) or _is_section_subheading_text(a):
            continue
        if _looks_like_lesson_topic_question(t):
            continue
        if _looks_like_instructional_body_paragraph(t):
            continue
        if _looks_like_activity_instruction(t) or _is_task_instruction_line(a, atoms=atoms):
            continue
        if _looks_like_body_question(t) and len(re.sub(r"\s+", "", t)) > 14:
            continue
        for seed in seeds:
            if _same_dialogue_bubble(a, seed, atoms):
                pool.append(a)
                seen.add(ac)
                break
    return pool


def _dialogue_affinity_clusters(
    atoms: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """按 _same_dialogue_bubble 成对亲和做连通分量（不依赖固定列距阈值）。"""
    if len(atoms) < 2:
        return []
    parent = list(range(len(atoms)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(len(atoms)):
        for j in range(i + 1, len(atoms)):
            if _same_dialogue_bubble(atoms[i], atoms[j], atoms):
                union(i, j)

    buckets: dict[int, list[dict[str, Any]]] = {}
    for i, atom in enumerate(atoms):
        buckets.setdefault(find(i), []).append(atom)
    return [cluster for cluster in buckets.values() if len(cluster) >= 2]


def _is_dialogue_bubble_merge_eligible(
    atom: dict[str, Any], atoms: list[dict[str, Any]]
) -> bool:
    """仅气泡 OCR 碎段可参与 bubble_id / 亲和 merge；说明/小标题/场景插图排除。"""
    if atom.get("atom_type") not in ("text", "title"):
        return False
    if _is_definite_subtitle_module_atom(atom, atoms):
        return False
    t = _body_text(atom)
    if _looks_like_instructional_body_paragraph(t):
        return False
    if _is_task_instruction_line(atom, atoms=atoms):
        return False
    if _looks_like_activity_instruction(t):
        return False
    if _is_subtitle_bar_text(atom, atoms) or _is_section_subheading_text(atom):
        return False
    return (
        _looks_like_dialogue(t)
        or _is_dialogue_bubble_fragment(atom, atoms)
        or _is_likely_dialogue_bubble_line(atom, atoms)
    )


def _filter_dialogue_bubble_merge_codes(
    codes: list[str], atoms: list[dict[str, Any]]
) -> list[str]:
    by_code = {str(a["atom_code"]): a for a in atoms}
    return [
        c
        for c in codes
        if c in by_code and _is_dialogue_bubble_merge_eligible(by_code[c], atoms)
    ]


def _merge_group_has_dialogue_text(
    codes: list[str],
    atoms: list[dict[str, Any]],
) -> bool:
    """合并组内是否含对话气泡台词（仅此类组做亲和拆分）。"""
    return len(_filter_dialogue_bubble_merge_codes(codes, atoms)) >= 1


def _split_merge_group_by_dialogue_affinity(
    codes: list[str],
    atoms: list[dict[str, Any]],
) -> list[list[str]]:
    """把误并的多气泡/跨栏合并组拆成若干 _same_dialogue_bubble 连通分量。"""
    eligible = _filter_dialogue_bubble_merge_codes(codes, atoms)
    if len(eligible) < 2:
        return []

    by_code = {str(a["atom_code"]): a for a in atoms}
    text_pool = [by_code[c] for c in eligible if c in by_code]
    if len(text_pool) < 2:
        return []

    clusters = _dialogue_affinity_clusters(text_pool)
    if not clusters:
        return []

    if len(clusters) == 1:
        return [[str(a["atom_code"]) for a in clusters[0]]]

    # 保留合并组内原子相对顺序
    code_order = {c: i for i, c in enumerate(codes)}
    out: list[list[str]] = []
    for cluster in clusters:
        sub = sorted(
            (str(a["atom_code"]) for a in cluster),
            key=lambda c: code_order.get(c, 0),
        )
        if len(sub) >= 2:
            out.append(sub)
    return out


def _refine_dialogue_merge_groups_in_plan(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """校验所有 merge 组：跨气泡误并由亲和图拆回各自连通分量。"""
    merge_out: list[dict[str, Any]] = []
    split_groups = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        subgroups = _split_merge_group_by_dialogue_affinity(codes, atoms)
        if not subgroups:
            merge_out.append({**g, "atom_codes": codes})
            continue
        if len(subgroups) > 1:
            split_groups += 1
        reason = g.get("reason") or "同一对话气泡内 OCR 碎段合并"
        for sub in subgroups:
            if len(sub) >= 2:
                merge_out.append({**g, "atom_codes": sub, "reason": reason})
    warnings = list(plan.get("warnings") or [])
    if split_groups:
        warnings.append(
            f"规则：已按气泡亲和拆分 {split_groups} 组跨气泡/跨栏误合并"
        )
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def detect_dialogue_merge_groups(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一蓝色气泡内被 OCR 拆碎的多段台词 → 合并为一个对话原子。"""
    pool = [
        a
        for a in _expand_dialogue_pool(atoms)
        if a.get("atom_type") in ("text", "title")
    ]
    if not pool:
        return []
    groups: list[dict[str, Any]] = []
    for cluster in _dialogue_affinity_clusters(pool):
        cluster = [
            c
            for c in cluster
            if not _is_instruction_evidence_block(c)
            and not _is_prompt_text_line_candidate(c)
        ]
        if len(cluster) < 2:
            continue
        if any(
            (_is_subtitle_bar_text(c, atoms) or _is_section_subheading_text(c))
            and not _looks_like_dialogue(_text(c))
            for c in cluster
        ):
            continue
        codes = [str(c["atom_code"]) for c in cluster]
        groups.append(
            {
                "atom_codes": codes,
                "reason": "同一对话气泡内 OCR 碎段合并",
            }
        )
    return groups


def _is_body_text_wrap_merge_group(
    group_atoms: list[dict[str, Any]], all_atoms: list[dict[str, Any]]
) -> bool:
    """合并组是否为正文绕 inline 插图断句。"""
    from ...parsers.textbook_atom_extract import (
        images_on_same_row_between,
        looks_like_sentence_wrap_continuation,
    )

    images = [a for a in all_atoms if a.get("atom_type") == "image"]
    if len(group_atoms) < 2 or not images:
        return False
    ordered = sorted(group_atoms, key=lambda a: float(a.get("x_start") or 0))
    for i in range(len(ordered) - 1):
        left, right = ordered[i], ordered[i + 1]
        gap = float(right["x_start"]) - float(left["x_end"])
        if gap < 0.08:
            continue
        if images_on_same_row_between(left, right, images) and (
            looks_like_sentence_wrap_continuation(left, right)
        ):
            return True
    return False


def detect_body_text_wrap_merges(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[dict[str, Any]]:
    """正文句被 inline 插图隔开的多段 OCR 碎框 → 合并为一行。"""
    from ...parsers.textbook_atom_extract import (
        images_on_same_row_between,
        looks_like_sentence_wrap_continuation,
    )

    images = [a for a in atoms if a.get("atom_type") == "image"]
    texts = sorted(
        [
            a
            for a in atoms
            if a.get("atom_type") in ("text", "title")
            and str(a["atom_code"]) not in protected
        ],
        key=lambda a: (_y_center(a), float(a["x_start"])),
    )
    if len(texts) < 2 or not images:
        return []

    groups: list[dict[str, Any]] = []
    used: set[str] = set()
    i = 0
    while i < len(texts):
        ac = str(texts[i]["atom_code"])
        if ac in used:
            i += 1
            continue
        chain = [texts[i]]
        j = i + 1
        while j < len(texts):
            bc = str(texts[j]["atom_code"])
            if bc in used:
                j += 1
                continue
            left, right = chain[-1], texts[j]
            if abs(_y_center(left) - _y_center(right)) > 0.038:
                break
            gap = float(right["x_start"]) - float(left["x_end"])
            if gap < 0.04:
                chain.append(right)
                j += 1
                continue
            if images_on_same_row_between(
                left, right, images
            ) and looks_like_sentence_wrap_continuation(left, right):
                chain.append(right)
                j += 1
                continue
            break
        if len(chain) >= 2:
            codes = [str(c["atom_code"]) for c in chain]
            used.update(codes)
            groups.append(
                {
                    "atom_codes": codes,
                    "reason": "正文绕插图断句：左右碎段合并",
                }
            )
        i += 1
    return groups


def detect_body_question_line_merges(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[dict[str, Any]]:
    """引导问句、说一说、活动说明、总结句：拼音+汉字合并为一行。"""
    groups: list[dict[str, Any]] = []
    used: set[str] = set()
    candidates = [
        a
        for a in atoms
        if str(a["atom_code"]) not in protected
        and str(a["atom_code"]) not in used
        and _is_prompt_text_line_candidate(a)
        and not _is_subtitle_bar_text(a, atoms)
    ]

    for a in candidates:
        ac = str(a["atom_code"])
        if ac in used:
            continue
        band = [a]
        for b in candidates:
            bc = str(b["atom_code"])
            if bc in used or bc == ac:
                continue
            if not _same_text_column(a, b, min_overlap=0.30):
                continue
            if abs(_y_center(a) - _y_center(b)) <= 0.065:
                band.append(b)
            elif _is_pinyin_hanzi_vertical_pair(a, b):
                band.append(b)
        if len(band) >= 2:
            codes = [str(c["atom_code"]) for c in band]
            used.update(codes)
            groups.append(
                {
                    "atom_codes": codes,
                    "reason": "引导/说明/总结句：拼音与汉字合并",
                }
            )
    return groups


def detect_lesson_title_subtitle_vertical_span_deletes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    """页顶大标题与下方小标题被 OCR 合成一条竖向插图/文字大框 → 删除。"""
    deletes: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if _spans_lesson_title_and_subtitle_band(a):
            deletes.append(code)
            continue
        if _text_has_mixed_lesson_title_and_subtitle_bar(_text(a)):
            deletes.append(code)
    return deletes


def detect_title_body_span_deletes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    """小标题与正文被 OCR 合成一个竖向大框（如 A001-002）→ 删除。"""
    deletes: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected or _looks_like_dialogue(_text(a)):
            continue
        if a.get("atom_type") not in ("text", "title"):
            continue
        b = _bbox(a)
        h = _height(a)
        if b["y0"] < 0.18 and b["y1"] > 0.42:
            deletes.append(code)
            continue
        if h > 0.32:
            deletes.append(code)
            continue
    return deletes


def detect_bottom_strip_deletes(atoms: list[dict[str, Any]]) -> list[str]:
    """
    页底横向大条（页码+右侧小人被框在一起）→ 整框删除；页码丢弃，小人应另有碎框与主图合并。
    """
    deletes: list[str] = []
    for a in atoms:
        b = _bbox(a)
        w, h = _width(a), _height(a)
        if _looks_like_activity_instruction(_text(a)) or _is_prompt_text_line_candidate(a):
            continue
        if b["y0"] > 0.74 and w > 0.38 and h < 0.28:
            deletes.append(str(a["atom_code"]))
    return deletes


def detect_noise_box_deletes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    """
    整页外包框、竖向 OCR 垃圾条（A001-006/007 类）→ 删除。
    """
    deletes: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if _looks_like_dialogue(_text(a)):
            continue
        b = _bbox(a)
        w, h = _width(a), _height(a)
        area = _area(a)

        if a.get("atom_type") == "image":
            if w > 0.92 and h > 0.88:
                deletes.append(code)
            continue

        if area > 0.38 or (w > 0.85 and h > 0.5):
            deletes.append(code)
            continue

        if h > 0.14 and w < 0.14:
            plain = re.sub(r"\s+", "", _text(a))
            if b["x1"] <= 0.15 and h > 0.14 and len(plain) <= 4:
                deletes.append(code)
                continue
            if _is_short_scene_label(_text(a)) or (
                re.search(r"[\u4e00-\u9fff]", plain) and len(plain) <= 8
            ):
                continue
            deletes.append(code)
            continue

        if h > 0.18 and w < 0.1:
            deletes.append(code)
            continue

    return deletes


def detect_oversized_spanning_text_codes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    """过大且与上下其他文字重叠的 OCR 框（蔓延到标题/插图/对话）。"""
    deletes: list[str] = []
    texts = [
        a
        for a in atoms
        if a.get("atom_type") in ("text", "title")
        and _text(a)
        and str(a["atom_code"]) not in protected
        and not _looks_like_dialogue(_text(a))
    ]
    all_real = [
        a
        for a in atoms
        if str(a["atom_code"]) not in protected
        and (a.get("atom_type") in ("text", "title", "image") or _area(a) > 0.02)
    ]
    for a in texts:
        code = str(a["atom_code"])
        if _is_prompt_text_line_candidate(a):
            continue
        if _looks_like_activity_instruction(_text(a)) and _height(a) <= 0.22:
            continue
        h = _height(a)
        if h < 0.11:
            continue
        ay = _y_center(a)
        for other in texts:
            if other["atom_code"] == a["atom_code"]:
                continue
            oy = _y_center(other)
            if abs(ay - oy) > 0.07 and _overlaps(a, other):
                if _height(other) < h * 0.7:
                    deletes.append(code)
                    break
        if code in deletes:
            continue
        children = [
            b
            for b in all_real
            if str(b["atom_code"]) != code
            and _containment_ratio(b, a) >= 0.62
        ]
        if not children:
            continue
        if any(_looks_like_dialogue(_text(c)) for c in children):
            deletes.append(code)
            continue
        if any(_is_section_subheading_text(c) for c in children) and any(
            _looks_like_dialogue(_text(c)) or _is_dialogue_bubble_fragment(c, atoms)
            for c in children
        ):
            deletes.append(code)
            continue
        lines = [ln.strip() for ln in _text(a).splitlines() if ln.strip()]
        if len(lines) >= 2:
            tail_dialogue = any(
                "，" in ln or "……" in ln or "…" in ln or "?" in ln or "？" in ln
                for ln in lines[1:]
            )
            head_plain = re.sub(r"\s+", "", lines[0])
            if len(head_plain) >= 6 and tail_dialogue:
                deletes.append(code)
                continue
        if len(children) >= 2 and h > max(_height(c) * 1.35 for c in children):
            deletes.append(code)
    return deletes


def _is_nested_image_wrapper_pair(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    """插图里外双层框：删外包、留内层紧框（非主图+角标 inset）。"""
    if outer.get("atom_type") != "image" or inner.get("atom_type") != "image":
        return False
    ao, ai = _area(outer), _area(inner)
    if ao <= ai * 1.03:
        return False
    if _containment_ratio(inner, outer) < 0.68:
        return False
    if ao / max(ai, 1e-9) > 3.0:
        return False
    if _iou(outer, inner) < 0.10:
        return False
    return True


def _duplicate_image_drop_code(a: dict[str, Any], b: dict[str, Any]) -> str:
    """重叠插图对中应丢弃的 atom_code。"""
    ca, cb = str(a["atom_code"]), str(b["atom_code"])
    aa, ab = _area(a), _area(b)
    if aa > ab * 1.03 and _containment_ratio(b, a) >= 0.65:
        return ca
    if ab > aa * 1.03 and _containment_ratio(a, b) >= 0.65:
        return cb
    if "-xref" in ca and "-xref" not in cb:
        return ca
    if "-xref" in cb and "-xref" not in ca:
        return cb
    if aa > ab * 1.02 and _containment_ratio(b, a) >= 0.75:
        return ca
    if ab > aa * 1.02 and _containment_ratio(a, b) >= 0.75:
        return cb
    sa = re.search(r"-(\d+)", ca)
    sb = re.search(r"-(\d+)", cb)
    na = int(sa.group(1)) if sa else 0
    nb = int(sb.group(1)) if sb else 0
    return ca if na > nb else cb


def _is_overlapping_image_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """两插图框高度重合（PDF 双份提取或里外双层框）。"""
    if a.get("atom_type") != "image" or b.get("atom_type") != "image":
        return False
    if _iou(a, b) >= 0.52:
        return True
    cr_ab = _containment_ratio(a, b)
    cr_ba = _containment_ratio(b, a)
    if cr_ab >= 0.82 and cr_ba >= 0.82:
        return True
    aa, ab = _area(a), _area(b)
    outer, inner = (a, b) if aa >= ab else (b, a)
    if _containment_ratio(inner, outer) >= 0.65 and _iou(a, b) >= 0.35:
        return True
    return _is_nested_image_wrapper_pair(outer, inner)


def detect_duplicate_image_deletes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    """重叠插图删重复/外包框，保留更紧的一框。"""
    deletes: list[str] = []
    images = [
        a
        for a in atoms
        if str(a["atom_code"]) not in protected
        and a.get("atom_type") == "image"
        and _area(a) > 0.015
    ]
    seen: set[str] = set()
    for i in range(len(images)):
        for j in range(i + 1, len(images)):
            a, b = images[i], images[j]
            ca, cb = str(a["atom_code"]), str(b["atom_code"])
            if ca in seen or cb in seen:
                continue
            if not _is_overlapping_image_duplicate(a, b):
                continue
            drop = _duplicate_image_drop_code(a, b)
            deletes.append(drop)
            seen.add(drop)
    return deletes


def detect_wrapper_atom_deletes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    """里外双层外包框：大框内含多个子原子时删外包。"""
    deletes: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if _looks_like_dialogue(_text(a)):
            continue
        if a.get("atom_type") != "image" and _area(a) < 0.11:
            continue
        if a.get("atom_type") == "image" and _area(a) < 0.015:
            continue
        inners = [
            b
            for b in atoms
            if str(b["atom_code"]) != code
            and str(b["atom_code"]) not in protected
            and _containment_ratio(b, a) >= 0.74
        ]
        if len(inners) >= 2:
            if a.get("atom_type") == "image":
                inner_types = {b.get("atom_type") for b in inners}
                if inner_types <= {"text", "title"}:
                    continue
            deletes.append(code)
            continue
        if len(inners) == 1:
            inner = inners[0]
            if _is_nested_image_wrapper_pair(a, inner):
                deletes.append(code)
                continue
            if _area(a) > 0.18:
                if _iou(a, inner) > 0.20 and _area(a) > _area(inner) * 1.05:
                    deletes.append(code)
                    continue
            pi = re.sub(r"\s+", "", _text(inner))
            po = re.sub(r"\s+", "", _text(a))
            if (
                len(pi) >= 4
                and pi in po
                and _height(a) > _height(inner) * 1.35
            ):
                deletes.append(code)
    return deletes


def detect_activity_bar_span_deletes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    """活动条蔓延大框：内含贴条内汉字小框时删大框。"""
    deletes: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if _looks_like_dialogue(_text(a)):
            continue
        if a.get("atom_type") == "image" and _area(a) > 0.12:
            continue
        h = _height(a)
        if h < 0.11:
            continue
        b = _bbox(a)
        plain = re.sub(r"\s+", "", _text(a))
        children = [
            c
            for c in atoms
            if str(c["atom_code"]) != code
            and str(c["atom_code"]) not in protected
            and _containment_ratio(c, a) >= 0.50
            and _height(c) < h * 0.62
        ]
        if not children:
            continue
        for c in children:
            pc = re.sub(r"\s+", "", _text(c))
            if len(pc) >= 4 and pc in plain:
                deletes.append(code)
                break
            if _is_activity_bar_text(c) or (
                len(pc) <= 22
                and re.search(r"[\u4e00-\u9fff]", pc or "")
                and not _looks_like_activity_instruction(_text(c))
            ):
                deletes.append(code)
                break
        if code in deletes:
            continue
        if _looks_like_activity_instruction(_text(a)) and h > 0.13:
            bar_like = [
                c
                for c in children
                if not _looks_like_activity_instruction(_text(c))
                and len(re.sub(r"\s+", "", _text(c))) <= 22
            ]
            if bar_like:
                deletes.append(code)
    return deletes


def detect_subtitle_bar_icon_fragment_merges(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[dict[str, Any]]:
    """小标题条左侧圆形图标被 OCR 拆成多块 → 先合并。"""
    icons = [
        a
        for a in atoms
        if str(a["atom_code"]) not in protected
        and a.get("atom_type") == "image"
        and _bbox(a)["x0"] < 0.16
        and _area(a) < 0.07
        and not _is_page_number_atom(a)
    ]
    if len(icons) < 2:
        return []
    groups: list[dict[str, Any]] = []
    used: set[str] = set()
    for a in icons:
        ac = str(a["atom_code"])
        if ac in used:
            continue
        cluster = [a]
        for b in icons:
            bc = str(b["atom_code"])
            if bc in used or bc == ac:
                continue
            if (
                _iou(a, b) > 0.12
                or _containment_ratio(b, a) >= 0.40
                or _containment_ratio(a, b) >= 0.40
            ):
                cluster.append(b)
        if len(cluster) < 2:
            continue
        codes = [str(c["atom_code"]) for c in cluster]
        used.update(codes)
        groups.append(
            {
                "atom_codes": codes,
                "reason": "小标题条左侧图标碎块合并",
            }
        )
    return groups


def detect_activity_bar_merge_groups(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    活动条/课节条：左侧小人图标 + 上方拼音 + 条内汉字 → 合并为一原子。
    """
    prot = protected or set()
    groups: list[dict[str, Any]] = []
    used: set[str] = set()
    all_icons = [
        a
        for a in atoms
        if str(a["atom_code"]) not in prot
        and a.get("atom_type") == "image"
        and (
            _is_subtitle_bar_left_icon(a, atoms)
            or _is_activity_bar_icon(a, atoms)
        )
    ]
    icon_clusters: list[list[dict[str, Any]]] = []
    icon_seen: set[str] = set()
    for icon in all_icons:
        ic = str(icon["atom_code"])
        if ic in icon_seen:
            continue
        cluster = [icon]
        for other in all_icons:
            oc = str(other["atom_code"])
            if oc in icon_seen or oc == ic:
                continue
            if (
                _iou(icon, other) > 0.12
                or _containment_ratio(other, icon) >= 0.40
                or _containment_ratio(icon, other) >= 0.40
            ):
                cluster.append(other)
        for c in cluster:
            icon_seen.add(str(c["atom_code"]))
        icon_clusters.append(cluster)

    for cluster in icon_clusters:
        icon_codes = [str(c["atom_code"]) for c in cluster]
        if all(c in prot for c in icon_codes):
            continue
        if any(c in used for c in icon_codes):
            continue
        anchor = cluster[0]
        ib = _bbox(anchor)
        band_y0 = ib["y0"] - 0.14
        band_y1 = ib["y1"] + 0.14
        for a in atoms:
            if _is_subtitle_bar_text_shape(a) or _is_section_subheading_text(a):
                band_y0 = min(band_y0, _bbox(a)["y0"] - 0.02)
                band_y1 = max(band_y1, _bbox(a)["y1"] + 0.02)
        candidates: list[dict[str, Any]] = []

        for a in atoms:
            ac = str(a["atom_code"])
            if ac in used or ac in icon_codes or ac in prot:
                continue
            if not _is_subtitle_bar_text(a, atoms):
                if _is_inline_pinyin_parenthetical(a) and _subtitle_bar_y_overlap(
                    anchor, a
                ) >= 0.25:
                    pass
                elif _is_subtitle_bar_trailing_hanzi_fragment(a, atoms, anchor):
                    pass
                else:
                    continue
            if not (
                _is_subtitle_activity_band(a)
                or _subtitle_bar_y_overlap(anchor, a) >= 0.25
            ):
                continue
            is_bar_line = (
                _is_subtitle_bar_text(a, atoms)
                or _is_inline_pinyin_parenthetical(a)
                or _is_subtitle_bar_trailing_hanzi_fragment(a, atoms, anchor)
            )
            if not is_bar_line and (
                _looks_like_dialogue(_text(a))
                or _is_dialogue_bubble_fragment(a, atoms)
            ):
                continue
            tb = _bbox(a)
            if tb["y1"] < band_y0 or tb["y0"] > band_y1:
                continue
            if tb["x1"] < ib["x0"] - 0.05:
                continue
            if tb["x0"] > ib["x1"] + 0.62:
                continue
            candidates.append(a)

        if not candidates:
            continue
        codes = icon_codes + [str(c["atom_code"]) for c in candidates]
        used.update(codes)
        groups.append(
            {
                "atom_codes": codes,
                "reason": "活动条：左侧小人+拼音+条内汉字合并",
            }
        )

    return groups


def detect_lesson_title_bar_merge_groups(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[dict[str, Any]]:
    """课节标题条：顶部序号圈 + 拼音 + 汉字（如「1 植物角」）→ 一行合并。"""
    candidates: list[dict[str, Any]] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if _spans_lesson_title_and_subtitle_band(a):
            continue
        b = _bbox(a)
        if b["y0"] > 0.14:
            continue
        if _is_lesson_title_text(a):
            candidates.append(a)
            continue
        if a.get("atom_type") == "image" and b["x0"] < 0.18 and _area(a) < 0.10:
            if _is_page_number_atom(a):
                continue
            if _is_subtitle_activity_band(a):
                continue
            candidates.append(a)
            continue
        if _looks_like_pinyin_line(_text(a)) and b["y0"] < 0.14 and b["x0"] < 0.70:
            candidates.append(a)

    if len(candidates) < 2:
        return []

    left_band = [a for a in candidates if _bbox(a)["x0"] < 0.72]
    if len(left_band) < 2:
        return []

    codes = [str(c["atom_code"]) for c in left_band]
    code_set = set(codes)
    for a in atoms:
        ac = str(a["atom_code"])
        if ac in protected or ac in code_set:
            continue
        if not _is_lesson_title_banner_image(a):
            continue
        for c in left_band:
            if (
                _iou(a, c) > 0.04
                or _containment_ratio(c, a) >= 0.25
                or _containment_ratio(a, c) >= 0.25
            ):
                codes.append(ac)
                code_set.add(ac)
                break

    return [
        {
            "atom_codes": codes,
            "reason": "课节标题条：序号圈+拼音+汉字一行合并",
        }
    ]


def detect_illustration_cluster_merges(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[dict[str, Any]]:
    """主插图 + 角 inset 小圆图 / 部分重叠插图块合并。"""
    images: list[dict[str, Any]] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if _looks_like_dialogue(_text(a)):
            continue
        if _is_page_number_atom(a):
            continue
        plain = re.sub(r"\s+", "", _text(a))
        if _PAGE_NUM_RE.match(plain):
            continue
        if a.get("atom_type") != "image" and _area(a) < 0.045:
            continue
        if _bbox(a)["y0"] > 0.88:
            continue
        if (
            _bbox(a)["y0"] > 0.74
            and _width(a) > 0.38
            and _height(a) < 0.28
        ):
            continue
        images.append(a)

    if len(images) < 2:
        return []

    groups: list[dict[str, Any]] = []
    used: set[str] = set()
    mains = sorted(
        [a for a in images if _area(a) > 0.055],
        key=lambda x: -_area(x),
    )

    for main in mains:
        mc = str(main["atom_code"])
        if mc in used:
            continue
        cluster_codes = [mc]
        for other in images:
            oc = str(other["atom_code"])
            if oc in used or oc == mc:
                continue
            if _area(other) >= _area(main) * 0.92:
                continue
            if _are_peer_gallery_images(main, other):
                continue
            if _containment_ratio(other, main) >= 0.72:
                cluster_codes.append(oc)
            elif (
                _iou(main, other) > 0.10
                and _area(other) < _area(main) * 0.42
                and _overlaps(main, other)
                and _containment_ratio(other, main) >= 0.35
            ):
                cluster_codes.append(oc)
        if len(cluster_codes) < 2:
            continue
        used.update(cluster_codes)
        groups.append(
            {
                "atom_codes": cluster_codes,
                "reason": "场景插图：主图与角 inset/重叠块合并",
            }
        )

    return groups


def detect_paired_small_illustration_merges(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[dict[str, Any]]:
    """页底左右对比两张示意（拉/推）→ 合并；2×2 步骤图等多张禁止合并。"""
    candidates = [
        a
        for a in atoms
        if str(a["atom_code"]) not in protected
        and _is_small_illustration_candidate(a)
        and _bbox(a)["y0"] > 0.60
    ]
    if len(candidates) != 2:
        return []
    if _is_demo_photo_grid_layout(candidates):
        return []

    a, b = candidates[0], candidates[1]
    if abs(_y_center(a) - _y_center(b)) > 0.075:
        return []
    if not _are_horizontal_neighbors(a, b):
        return []

    return [
        {
            "atom_codes": [str(a["atom_code"]), str(b["atom_code"])],
            "reason": "页底左右对比示意小图合并",
        }
    ]


def _is_scene_label_image_inset(
    fragment: dict[str, Any],
    illustration: dict[str, Any],
    atoms: list[dict[str, Any]] | None = None,
) -> bool:
    """插图内嵌小图块（如竖牌「植物角」被 OCR 成 image）。"""
    if fragment.get("atom_type") != "image":
        return False
    if _is_page_number_atom(fragment):
        return False
    if _is_sidebar_mascot_image(fragment):
        return False
    if atoms is not None and _icon_pairs_subtitle_bar(fragment, atoms):
        return False
    if _area(fragment) > 0.03 or _area(fragment) < 0.0003:
        return False
    if _containment_ratio(fragment, illustration) < 0.80:
        return False
    if _area(fragment) > _area(illustration) * 0.12:
        return False
    return True


def _scene_label_above_illustration(
    label: dict[str, Any], illustration: dict[str, Any]
) -> bool:
    """标牌在插图上方/顶边（如墙上「植物角」+ 下方场景图）。"""
    if illustration.get("atom_type") != "image":
        return False
    lb, ib = _bbox(label), _bbox(illustration)
    x_overlap = min(lb["x1"], ib["x1"]) - max(lb["x0"], ib["x0"])
    if x_overlap < 0.02:
        return False
    if ib["y0"] < lb["y0"] - 0.04:
        return False
    if ib["y0"] > lb["y1"] + 0.12:
        return False
    return _height(illustration) >= 0.10 and _width(illustration) >= 0.20


def detect_scene_label_into_illustration_merges(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    used: set[str] = set()

    illustrations = [
        a
        for a in atoms
        if str(a["atom_code"]) not in protected
        and not _looks_like_dialogue(_text(a))
        and (a.get("atom_type") == "image" or _area(a) > 0.08)
        and _height(a) > 0.15
        and _area(a) > 0.06
        and _width(a) > 0.25
        and _bbox(a)["y0"] > 0.10
    ]
    labels = [
        a
        for a in atoms
        if str(a["atom_code"]) not in protected
        and _is_scene_label_atom(a)
    ]
    dialogue_codes: set[str] = set()
    for g in detect_dialogue_merge_groups(atoms):
        dialogue_codes.update(g.get("atom_codes") or [])

    for img in illustrations:
        ic = str(img["atom_code"])
        if ic in used:
            continue
        label_codes: list[str] = []
        for lab in labels:
            lc = str(lab["atom_code"])
            if lc in used or lc == ic or lc in dialogue_codes:
                continue
            if (
                _overlaps(img, lab)
                or _containment_ratio(lab, img) >= 0.55
                or _scene_label_above_illustration(lab, img)
            ):
                label_codes.append(lc)
        for other in atoms:
            oc = str(other["atom_code"])
            if oc in used or oc == ic or oc in protected:
                continue
            if _is_scene_label_image_inset(other, img, atoms):
                label_codes.append(oc)
        if not label_codes:
            continue
        codes = [ic] + label_codes
        used.update(codes)
        groups.append(
            {
                "atom_codes": codes,
                "reason": "场景插图：标牌/角标属于画面背景，并入主插图",
            }
        )

    return groups


def detect_illustration_with_mascot_merges(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    mains = [
        a
        for a in atoms
        if str(a["atom_code"]) not in protected
        and not _looks_like_dialogue(_text(a))
        and not _is_page_number_atom(a)
        and (a.get("atom_type") == "image" or _area(a) > 0.1)
        and _bbox(a)["y0"] > 0.12
        and _bbox(a)["y1"] < 0.92
        and _x_center(a) < 0.78
        and 0.08 < _area(a) < 0.55
        and not (
            _bbox(a)["y0"] > 0.74
            and _width(a) > 0.38
            and _height(a) < 0.28
        )
    ]
    mascots = [
        a
        for a in atoms
        if str(a["atom_code"]) not in protected
        and not _looks_like_dialogue(_text(a))
        and not _is_page_number_atom(a)
        and _x_center(a) > 0.76
        and _area(a) < 0.12
        and _bbox(a)["y0"] > 0.58
        and not _PAGE_NUM_RE.match(re.sub(r"\s+", "", _text(a)))
    ]

    used: set[str] = set()
    for main in sorted(mains, key=lambda x: -_area(x)):
        mc = str(main["atom_code"])
        if mc in used:
            continue
        my0, my1 = _bbox(main)["y0"], _bbox(main)["y1"]
        mates: list[str] = []
        for m in mascots:
            sc = str(m["atom_code"])
            if sc in used:
                continue
            mb = _bbox(m)
            sy0, sy1 = mb["y0"], mb["y1"]
            if sy1 < my0 - 0.12:
                continue
            if sy0 > my1 + 0.15 and sy0 > 0.85:
                continue
            mates.append(sc)
        if not mates:
            continue
        codes = [mc] + mates
        used.update(codes)
        groups.append(
            {
                "atom_codes": codes,
                "reason": "主插图与右侧小卡通/指南车合并",
            }
        )

    return groups


def build_heuristic_curate_supplement(
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """规则层建议；先算合并组，再算删除（避免删掉待合并文字）。"""
    dialogue_codes = collect_dialogue_codes(atoms)
    page_number_codes = set(detect_page_number_codes(atoms))
    merge_groups: list[dict[str, Any]] = []

    merge_groups.extend(detect_dialogue_merge_groups(atoms))

    protected: set[str] = set(dialogue_codes)
    merge_protected: set[str] = set(protected) | page_number_codes
    merge_protected.update(
        collect_image_merge_skip_codes(atoms, protected=protected)
    )
    for g in merge_groups:
        merge_protected.update(g.get("atom_codes") or [])

    title_groups = detect_lesson_title_bar_merge_groups(atoms, protected=merge_protected)
    merge_groups.extend(title_groups)
    for g in title_groups:
        merge_protected.update(g.get("atom_codes") or [])

    bar_groups = detect_activity_bar_merge_groups(atoms, protected=merge_protected)
    merge_groups.extend(bar_groups)
    for g in bar_groups:
        merge_protected.update(g.get("atom_codes") or [])

    merge_groups.extend(
        detect_body_text_wrap_merges(atoms, protected=merge_protected)
    )
    for g in merge_groups:
        merge_protected.update(g.get("atom_codes") or [])

    merge_groups.extend(
        detect_body_question_line_merges(atoms, protected=merge_protected)
    )
    for g in merge_groups:
        merge_protected.update(g.get("atom_codes") or [])

    cluster_groups = detect_illustration_cluster_merges(atoms, protected=merge_protected)
    merge_groups.extend(cluster_groups)
    for g in cluster_groups:
        merge_protected.update(g.get("atom_codes") or [])

    paired_groups = detect_paired_small_illustration_merges(
        atoms, protected=merge_protected
    )
    merge_groups.extend(paired_groups)
    for g in paired_groups:
        merge_protected.update(g.get("atom_codes") or [])

    merge_groups.extend(
        detect_scene_label_into_illustration_merges(atoms, protected=merge_protected)
    )
    for g in merge_groups:
        merge_protected.update(g.get("atom_codes") or [])

    delete_codes: list[str] = []
    delete_codes.extend(detect_bottom_strip_deletes(atoms))
    delete_codes.extend(
        detect_lesson_title_subtitle_vertical_span_deletes(atoms, protected=protected)
    )
    delete_codes.extend(
        detect_title_body_span_deletes(atoms, protected=protected)
    )
    delete_codes.extend(detect_noise_box_deletes(atoms, protected=protected))
    delete_codes.extend(
        detect_oversized_spanning_text_codes(atoms, protected=protected)
    )
    delete_codes.extend(
        detect_wrapper_atom_deletes(atoms, protected=protected)
    )
    delete_codes.extend(
        detect_activity_bar_span_deletes(atoms, protected=protected)
    )
    delete_codes.extend(
        detect_duplicate_image_deletes(atoms, protected=protected)
    )
    delete_codes.extend(detect_page_number_codes(atoms))

    delete_codes = [c for c in delete_codes if c not in protected]

    warnings: list[str] = []
    if delete_codes:
        warnings.append(f"规则：建议删除 {len(delete_codes)} 个噪点/页码/异常框")
    if merge_groups:
        warnings.append(f"规则：建议 {len(merge_groups)} 组合并")
    if dialogue_codes:
        warnings.append(f"规则：保留 {len(dialogue_codes)} 个对话原子")

    return sanitize_atom_curate_plan(
        {
            "merge_groups": merge_groups,
            "delete_codes": sorted(set(delete_codes)),
            "warnings": warnings,
        },
        atoms,
    )


def merge_curate_plans(
    llm_plan: dict[str, Any],
    heuristic_plan: dict[str, Any],
) -> dict[str, Any]:
    """合并 LLM 与规则；启发式合并组优先补漏（旧逻辑，保留兼容）。"""
    merge_out: list[dict[str, Any]] = list(heuristic_plan.get("merge_groups") or [])
    used_codes: set[str] = set()
    for g in merge_out:
        used_codes.update(g.get("atom_codes") or [])

    for g in llm_plan.get("merge_groups") or []:
        codes = [c for c in (g.get("atom_codes") or []) if c not in used_codes]
        if len(codes) < 2:
            continue
        if used_codes.intersection(codes):
            continue
        merge_out.append({**g, "atom_codes": codes})
        used_codes.update(codes)

    delete_set = set(heuristic_plan.get("delete_codes") or [])
    delete_set.update(llm_plan.get("delete_codes") or [])
    for g in merge_out:
        for c in g.get("atom_codes") or []:
            delete_set.discard(c)

    warnings = list(heuristic_plan.get("warnings") or []) + list(
        llm_plan.get("warnings") or []
    )
    return {
        "merge_groups": merge_out,
        "delete_codes": sorted(delete_set),
        "warnings": warnings,
    }


def _roles_by_code(atom_roles: list[dict[str, Any]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in atom_roles or []:
        code = str(raw.get("atom_code") or "").strip()
        role = str(raw.get("role") or "").strip()
        if code and role:
            out[code] = role
    return out


_LLM_BAR_ROLES = frozenset({"lesson_title_bar", "subtitle_bar"})
_LLM_DIALOGUE_ROLES = frozenset({"dialogue_bubble", "dialogue_fragment"})
_LLM_SCENE_ROLES = frozenset({"scene_label", "scene_inset"})
_SCENE_SIGN_WORDS = frozenset({"植物角", "动物角", "科学角", "图书角"})


def _text_has_mixed_dialogue_and_scene_label(text: str) -> bool:
    """同一 OCR 框内既有气泡台词又有插图内标牌（如「我带来」+「植物角」）。"""
    if not text:
        return False
    lines = [
        re.sub(r"\s+", "", ln)
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("[")
    ]
    if not lines:
        return False
    dialogue_lines = [
        ln
        for ln in lines
        if any(k in ln for k in ("带来", "铜钱", "兰花", "辣椒"))
        or "，" in ln
        or "……" in ln
        or "…" in ln
        or ln.endswith(("，", "。", "！", "？"))
    ]
    scene_lines = [
        ln
        for ln in lines
        if ln in _SCENE_SIGN_WORDS
        or (len(ln) <= 6 and "角" in ln and ln not in dialogue_lines)
    ]
    if dialogue_lines and scene_lines:
        return True
    plain = re.sub(r"\s+", "", text)
    return bool(
        dialogue_lines
        and any(w in plain for w in _SCENE_SIGN_WORDS)
        and len(plain) <= 36
    )


def detect_mixed_dialogue_scene_label_span_codes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    """气泡台词与远处「植物角」标牌粘在同一 OCR 大框 → 待拆分（非直接删除）。"""
    splits: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if a.get("atom_type") not in ("text", "title"):
            continue
        text = _text(a)
        if not text:
            continue
        if _text_has_mixed_dialogue_and_scene_label(text):
            splits.append(code)
    return splits


def _next_y_start_below_atom(
    atom: dict[str, Any], atoms: list[dict[str, Any]]
) -> float:
    """当前原子下方最近一行的 y_start（用于估算插图下边界）。"""
    b = _bbox(atom)
    below = [
        _bbox(a)["y0"]
        for a in atoms
        if str(a["atom_code"]) != str(atom["atom_code"])
        and _bbox(a)["y0"] > b["y1"] + 0.004
    ]
    return min(below) if below else min(b["y1"] + 0.32, 0.96)


def _looks_like_bubble_opener_line(text: str) -> bool:
    """气泡内问句前半/短续写（如「蚕宝宝喜欢」），非页内任务说明。"""
    t = (text or "").strip()
    plain = re.sub(r"\s+", "", t)
    if not plain or len(plain) > 14:
        return False
    if _looks_like_activity_instruction(t) or _looks_like_instructional_body_paragraph(t):
        return False
    if plain.endswith(("？", "?", "。", "！", "；")):
        return False
    if plain.endswith(("喜欢", "需要", "可以", "觉得", "认为")):
        return True
    if any(m in t for m in _DIALOGUE_MARKERS):
        return True
    return len(plain) <= 10 and re.search(r"[\u4e00-\u9fff]", plain)


def _text_has_mixed_instruction_and_bubble(text: str) -> bool:
    """单 OCR 框：任务说明句 + 气泡台词（如「查阅资料…」+「蚕宝宝喜欢」）。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    hanzi = [
        ln
        for ln in lines
        if re.search(r"[\u4e00-\u9fff]", ln) and not _looks_like_pinyin_line(ln)
    ]
    if len(hanzi) < 2:
        return False
    has_inst = any(
        _looks_like_activity_instruction(ln)
        or _looks_like_instructional_body_paragraph(ln)
        for ln in hanzi
    )
    has_bubble = any(
        _looks_like_bubble_opener_line(ln)
        or _looks_like_dialogue(ln)
        or (ln.endswith("？") and len(re.sub(r"\s+", "", ln)) <= 14)
        for ln in hanzi
    )
    return has_inst and has_bubble


def build_instruction_bubble_split_parts(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将「查阅资料…」+「蚕宝宝喜欢」粘连大框拆成：说明句、气泡碎段。"""
    text = _text(atom)
    if not _text_has_mixed_instruction_and_bubble(text):
        return []

    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("[")]
    b = _bbox(atom)
    h = max(b["y1"] - b["y0"], 0.04)
    inst_lines: list[str] = []
    bubble_lines: list[str] = []
    for ln in lines:
        if _looks_like_pinyin_line(ln):
            continue
        if (
            not bubble_lines
            and (
                _is_task_instruction_line(atom, text=ln, atoms=atoms)
                or _looks_like_activity_instruction(ln)
                or _looks_like_instructional_body_paragraph(ln)
            )
        ):
            inst_lines.append(ln)
        else:
            bubble_lines.append(ln)
    if not inst_lines or not bubble_lines:
        return []

    inst_h = h * len(inst_lines) / max(len(lines), 1)
    split_y = b["y0"] + max(inst_h, h * 0.38)
    parts: list[dict[str, Any]] = [
        {
            "atom_type": atom.get("atom_type") or "text",
            "content": "\n".join(inst_lines)[:500],
            "ocr_text": "\n".join(inst_lines)[:500],
            "bbox": {
                "x_start": round(b["x0"], 4),
                "y_start": round(b["y0"], 4),
                "x_end": round(b["x1"], 4),
                "y_end": round(split_y, 4),
            },
        },
        {
            "atom_type": atom.get("atom_type") or "text",
            "content": "\n".join(bubble_lines)[:500],
            "ocr_text": "\n".join(bubble_lines)[:500],
            "bbox": {
                "x_start": round(b["x0"], 4),
                "y_start": round(split_y, 4),
                "x_end": round(b["x1"], 4),
                "y_end": round(b["y1"], 4),
            },
        },
    ]
    return parts


def detect_instruction_bubble_span_codes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    splits: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if build_instruction_bubble_split_parts(a, atoms):
            splits.append(code)
    return splits


def build_mixed_dialogue_scene_split_parts(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将「我带来 + 植物角」粘连大框拆成：气泡台词、场景标牌、（可选）主插图。"""
    text = _text(atom)
    if not _text_has_mixed_dialogue_and_scene_label(text):
        return []

    b = _bbox(atom)
    w = max(b["x1"] - b["x0"], 0.01)
    h = b["y1"] - b["y0"]
    has_illus = "[插图" in text

    lines = [
        re.sub(r"\s+", "", ln)
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("[")
    ]

    def _line_is_scene_sign(ln: str) -> bool:
        return ln in _SCENE_SIGN_WORDS or (len(ln) <= 6 and "角" in ln)

    dialogue_lines = [ln for ln in lines if not _line_is_scene_sign(ln)]
    scene_lines = [ln for ln in lines if _line_is_scene_sign(ln)]

    parts: list[dict[str, Any]] = []
    if dialogue_lines:
        dlg_w = min(w * 0.42, 0.22)
        parts.append(
            {
                "atom_type": "text",
                "content": dialogue_lines[0][:500],
                "ocr_text": dialogue_lines[0][:500],
                "bbox": {
                    "x_start": round(b["x0"], 4),
                    "y_start": round(b["y0"], 4),
                    "x_end": round(b["x0"] + dlg_w, 4),
                    "y_end": round(b["y1"], 4),
                },
            }
        )
    if scene_lines:
        scene_w = min(w * 0.16, 0.12)
        parts.append(
            {
                "atom_type": "text",
                "content": scene_lines[0][:500],
                "ocr_text": scene_lines[0][:500],
                "bbox": {
                    "x_start": round(b["x1"] - scene_w, 4),
                    "y_start": round(b["y0"], 4),
                    "x_end": round(b["x1"], 4),
                    "y_end": round(b["y1"], 4),
                },
            }
        )
    if has_illus or (w > 0.32 and h < 0.12):
        next_y = _next_y_start_below_atom(atom, atoms)
        ill_y0 = b["y1"] + 0.004
        ill_y1 = max(ill_y0 + 0.14, next_y - 0.006)
        if ill_y1 - ill_y0 >= 0.10:
            parts.append(
                {
                    "atom_type": "image",
                    "content": "[插图]",
                    "ocr_text": "[插图]",
                    "bbox": {
                        "x_start": round(max(0.06, b["x0"] - 0.08), 4),
                        "y_start": round(ill_y0, 4),
                        "x_end": round(min(0.92, b["x1"] + 0.06), 4),
                        "y_end": round(ill_y1, 4),
                    },
                }
            )
    return parts if len(parts) >= 2 else []


def build_lesson_topic_subtitle_split_parts(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将「物体是怎么变形的？」+「改变橡皮泥的形状」粘连大框拆成独立原子。"""
    text = _text(atom)
    if not _text_has_mixed_lesson_topic_and_subtitle(text):
        return []

    b = _bbox(atom)
    h = max(b["y1"] - b["y0"], 0.04)
    has_illus = "[插图" in text
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("[")
    ]
    topic_lines: list[str] = []
    subtitle_lines: list[str] = []
    phase = "topic"
    for ln in lines:
        plain = re.sub(r"\s+", "", ln)
        if phase == "topic":
            topic_lines.append(ln)
            if ("？" in plain or "?" in plain) and re.search(r"[\u4e00-\u9fff]", ln):
                phase = "subtitle"
            continue
        subtitle_lines.append(ln)

    parts: list[dict[str, Any]] = []
    split_y = b["y0"] + h * 0.46
    if topic_lines:
        parts.append(
            {
                "atom_type": "text",
                "content": "\n".join(topic_lines)[:500],
                "ocr_text": "\n".join(topic_lines)[:500],
                "bbox": {
                    "x_start": round(b["x0"], 4),
                    "y_start": round(b["y0"], 4),
                    "x_end": round(b["x1"], 4),
                    "y_end": round(split_y, 4),
                },
            }
        )
    if has_illus:
        icon_w = min(0.12, (b["x1"] - b["x0"]) * 0.28)
        parts.append(
            {
                "atom_type": "image",
                "content": "[插图]",
                "ocr_text": "[插图]",
                "bbox": {
                    "x_start": round(b["x0"], 4),
                    "y_start": round(split_y, 4),
                    "x_end": round(b["x0"] + icon_w, 4),
                    "y_end": round(b["y1"], 4),
                },
            }
        )
    if subtitle_lines:
        text_x0 = round(b["x0"] + (0.12 if has_illus else 0.0), 4)
        parts.append(
            {
                "atom_type": "text",
                "content": "\n".join(subtitle_lines)[:500],
                "ocr_text": "\n".join(subtitle_lines)[:500],
                "bbox": {
                    "x_start": text_x0,
                    "y_start": round(split_y, 4),
                    "x_end": round(b["x1"], 4),
                    "y_end": round(b["y1"], 4),
                },
            }
        )
    return parts if len(parts) >= 2 else []


def build_title_subtitle_band_split_parts(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将「3 混合与分离」+「分离盐和芝麻」等无问句粘连大框拆成大标题 / 小标题。"""
    text = _text(atom)
    if not (
        _text_has_mixed_lesson_title_and_subtitle_bar(text)
        or _spans_lesson_title_and_subtitle_band(atom)
    ):
        return []

    b = _bbox(atom)
    h = max(b["y1"] - b["y0"], 0.04)
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("[")
    ]
    title_lines: list[str] = []
    subtitle_lines: list[str] = []
    if _text_has_mixed_lesson_topic_and_subtitle(text):
        return build_lesson_topic_subtitle_split_parts(atom, atoms)

    hanzi_lines = [
        (i, ln)
        for i, ln in enumerate(lines)
        if re.search(r"[\u4e00-\u9fff]", ln)
    ]
    if len(hanzi_lines) < 2:
        if not _spans_lesson_title_and_subtitle_band(atom):
            return []
        split_y = b["y0"] + h * 0.42
        return [
            {
                "atom_type": atom.get("atom_type") or "text",
                "content": text[:500],
                "ocr_text": text[:500],
                "bbox": {
                    "x_start": round(b["x0"], 4),
                    "y_start": round(b["y0"], 4),
                    "x_end": round(b["x1"], 4),
                    "y_end": round(split_y, 4),
                },
            },
            {
                "atom_type": atom.get("atom_type") or "text",
                "content": text[:500],
                "ocr_text": text[:500],
                "bbox": {
                    "x_start": round(b["x0"], 4),
                    "y_start": round(split_y, 4),
                    "x_end": round(b["x1"], 4),
                    "y_end": round(b["y1"], 4),
                },
            },
        ]

    subtitle_idx = hanzi_lines[1][0]
    for i, ln in hanzi_lines[1:]:
        plain = re.sub(r"\s+", "", ln)
        if 5 <= len(plain) <= 24 and not plain.endswith(("？", "?", "。", "，")):
            subtitle_idx = i
            break
    title_lines = lines[:subtitle_idx]
    subtitle_lines = lines[subtitle_idx:]
    if not title_lines or not subtitle_lines:
        return []

    split_y = b["y0"] + h * (len(title_lines) / max(len(lines), 1))
    parts: list[dict[str, Any]] = []
    if title_lines:
        parts.append(
            {
                "atom_type": "text",
                "content": "\n".join(title_lines)[:500],
                "ocr_text": "\n".join(title_lines)[:500],
                "bbox": {
                    "x_start": round(b["x0"], 4),
                    "y_start": round(b["y0"], 4),
                    "x_end": round(b["x1"], 4),
                    "y_end": round(split_y, 4),
                },
            }
        )
    has_illus = "[插图" in text or atom.get("atom_type") == "image"
    if has_illus and subtitle_lines:
        icon_w = min(0.12, (b["x1"] - b["x0"]) * 0.28)
        parts.append(
            {
                "atom_type": "image",
                "content": "[插图]",
                "ocr_text": "[插图]",
                "bbox": {
                    "x_start": round(b["x0"], 4),
                    "y_start": round(split_y, 4),
                    "x_end": round(b["x0"] + icon_w, 4),
                    "y_end": round(b["y1"], 4),
                },
            }
        )
    if subtitle_lines:
        text_x0 = round(b["x0"] + (0.12 if has_illus else 0.0), 4)
        parts.append(
            {
                "atom_type": "text",
                "content": "\n".join(subtitle_lines)[:500],
                "ocr_text": "\n".join(subtitle_lines)[:500],
                "bbox": {
                    "x_start": text_x0,
                    "y_start": round(split_y, 4),
                    "x_end": round(b["x1"], 4),
                    "y_end": round(b["y1"], 4),
                },
            }
        )
    return parts if len(parts) >= 2 else []


def detect_lesson_title_subtitle_band_span_codes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    splits: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if build_title_subtitle_band_split_parts(a, atoms):
            splits.append(code)
    return splits


def detect_lesson_topic_subtitle_span_codes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    splits: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if build_lesson_topic_subtitle_split_parts(a, atoms):
            splits.append(code)
    return splits


def collect_curate_split_specs(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str] | None = None,
) -> list[dict[str, Any]]:
    """返回 [{source_code, parts}, ...] 供 apply 阶段拆分。"""
    prot = protected or set()
    specs = collect_mixed_dialogue_scene_split_specs(atoms, protected=prot)
    seen = {s["source_code"] for s in specs}
    for a in atoms:
        code = str(a["atom_code"])
        if code in prot or code in seen:
            continue
        parts = build_instruction_bubble_split_parts(a, atoms)
        if parts:
            specs.append({"source_code": code, "parts": parts})
            seen.add(code)
            continue
        parts = build_lesson_topic_subtitle_split_parts(a, atoms)
        if parts:
            specs.append({"source_code": code, "parts": parts})
            seen.add(code)
            continue
        parts = build_title_subtitle_band_split_parts(a, atoms)
        if parts:
            specs.append({"source_code": code, "parts": parts})
            seen.add(code)
    return specs


def _is_protected_illustration_atom(atom: dict[str, Any] | None) -> bool:
    """主插图 image 原子不可被整理流程删除。"""
    if not atom or atom.get("atom_type") != "image":
        return False
    if _is_page_number_atom(atom) or _is_sidebar_mascot_image(atom):
        return False
    if _is_instructional_inset_image(atom):
        return True
    return _area(atom) > 0.05 and _height(atom) > 0.15 and _width(atom) > 0.20


def collect_mixed_dialogue_scene_split_specs(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str] | None = None,
) -> list[dict[str, Any]]:
    """返回 [{source_code, parts}, ...] 供 apply 阶段拆分。"""
    prot = protected or set()
    specs: list[dict[str, Any]] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in prot:
            continue
        parts = build_mixed_dialogue_scene_split_parts(a, atoms)
        if parts:
            specs.append({"source_code": code, "parts": parts})
    return specs


def _atom_is_dialogue_like(
    atom: dict[str, Any], atoms: list[dict[str, Any]]
) -> bool:
    if _atom_has_mixed_dialogue_scene(atom):
        return False
    if _looks_like_dialogue(_text(atom)) or _is_dialogue_bubble_fragment(atom, atoms):
        return True
    plain = re.sub(r"\s+", "", _text(atom))
    return bool(
        plain
        and re.search(r"[\u4e00-\u9fff]", plain)
        and any(k in plain for k in ("带来", "铜钱", "兰花", "辣椒", "这是"))
    )


def _atom_has_mixed_dialogue_scene(atom: dict[str, Any]) -> bool:
    return _text_has_mixed_dialogue_and_scene_label(_text(atom))


def _atom_is_scene_label_like(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]] | None = None,
    roles: dict[str, str] | None = None,
) -> bool:
    code = str(atom.get("atom_code", ""))
    if roles and roles.get(code) in _LLM_SCENE_ROLES:
        return True
    if _is_scene_label_atom(atom):
        return True
    plain = re.sub(r"\s+", "", _text(atom))
    if plain in _SCENE_SIGN_WORDS and not _looks_like_dialogue(_text(atom)):
        return True
    if atoms is not None and atom.get("atom_type") == "image":
        for other in atoms:
            if other is atom or other.get("atom_type") != "image":
                continue
            if _area(other) > 0.06 and _is_scene_label_image_inset(atom, other, atoms):
                return True
    return False


def strip_dialogue_scene_label_cross_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """禁止气泡台词与插图内「植物角」等标牌 merge 到同一组。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    roles = _roles_by_code(atom_roles)
    merge_out: list[dict[str, Any]] = []
    dropped = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        if any(
            c in by_code and _atom_has_mixed_dialogue_scene(by_code[c])
            for c in codes
        ):
            dlg_codes = [
                c
                for c in codes
                if c in by_code
                and not _atom_has_mixed_dialogue_scene(by_code[c])
                and _atom_is_dialogue_like(by_code[c], atoms)
            ]
            if len(dlg_codes) >= 2:
                merge_out.append(
                    {
                        **g,
                        "atom_codes": dlg_codes,
                        "reason": "同一对话气泡内 OCR 碎段",
                    }
                )
            dropped += 1
            continue
        atoms_in = [by_code[c] for c in codes if c in by_code]
        has_dialogue = any(_atom_is_dialogue_like(a, atoms) for a in atoms_in)
        has_scene = any(
            _atom_is_scene_label_like(a, atoms, roles) for a in atoms_in
        )
        if not (has_dialogue and has_scene):
            merge_out.append({**g, "atom_codes": codes})
            continue
        dlg_codes = [
            c
            for c in codes
            if c in by_code
            and _atom_is_dialogue_like(by_code[c], atoms)
            and not _atom_is_scene_label_like(by_code[c], atoms, roles)
        ]
        scene_codes = [
            c
            for c in codes
            if c in by_code
            and _atom_is_scene_label_like(by_code[c], atoms, roles)
        ]
        if len(dlg_codes) >= 2:
            merge_out.append(
                {
                    **g,
                    "atom_codes": dlg_codes,
                    "reason": "同一对话气泡内 OCR 碎段",
                }
            )
        if len(scene_codes) >= 2:
            merge_out.append(
                {
                    **g,
                    "atom_codes": scene_codes,
                    "reason": "场景插图：标牌属于画面背景",
                }
            )
        dropped += 1
    warnings = list(plan.get("warnings") or [])
    if dropped:
        warnings.append(f"规则：已拆分 {dropped} 组气泡与场景标牌错误合并")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def strip_lesson_title_polluted_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """课节大标题不得与引导问句/小标题粘连框合并；粘连框应拆分。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    merge_out: list[dict[str, Any]] = []
    dropped = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        if any(
            c in by_code and _spans_lesson_title_and_subtitle_band(by_code[c])
            for c in codes
        ):
            dropped += 1
            continue
        polluted = [
            c
            for c in codes
            if c in by_code
            and (
                _text_has_mixed_lesson_topic_and_subtitle(_text(by_code[c]))
                or _text_has_mixed_lesson_title_and_subtitle_bar(_text(by_code[c]))
            )
        ]
        if polluted:
            dropped += 1
            continue
        y0s = [_bbox(by_code[c])["y0"] for c in codes if c in by_code]
        if y0s and min(y0s) < 0.135 and max(y0s) >= 0.14:
            title_codes = [
                c
                for c in codes
                if c in by_code
                and _bbox(by_code[c])["y0"] < 0.135
                and (
                    _is_lesson_title_text(by_code[c])
                    or by_code[c].get("atom_type") == "title"
                    or _is_lesson_title_banner_image(by_code[c])
                )
                and not _atom_has_lesson_topic_question(by_code[c])
            ]
            if len(title_codes) >= 2:
                merge_out.append(
                    {
                        **g,
                        "atom_codes": title_codes,
                        "reason": "课节标题条：序号圈+拼音+汉字一行合并",
                    }
                )
            dropped += 1
            continue
        cross_topic = [
            c
            for c in codes
            if c in by_code and _atom_has_lesson_topic_question(by_code[c])
        ]
        cross_sub = [
            c
            for c in codes
            if c in by_code
            and (
                _is_subtitle_bar_text(by_code[c], atoms)
                or _is_section_subheading_text(by_code[c])
            )
        ]
        if cross_topic and cross_sub:
            dropped += 1
            continue
        cross_title = [
            c
            for c in codes
            if c in by_code
            and _bbox(by_code[c])["y0"] < 0.135
            and (
                _is_lesson_title_text(by_code[c])
                or _is_lesson_title_banner_image(by_code[c])
                or by_code[c].get("atom_type") == "title"
            )
        ]
        if cross_title and cross_sub:
            dropped += 1
            continue
        merge_out.append({**g, "atom_codes": codes})
    warnings = list(plan.get("warnings") or [])
    if dropped:
        warnings.append(f"规则：已拆分 {dropped} 组大标题与引导问句/小标题错误合并")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def _is_process_record_block_content(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]] | None = None,
) -> bool:
    """记事本块内正文（采访录/计划书/观察记录），不是页中对话气泡。"""
    if atom.get("atom_type") not in ("text", "title"):
        return False
    text = _text(atom)
    plain = re.sub(r"\s+", "", text)
    if not plain:
        return False
    if any(
        k in plain
        for k in (
            "采访录",
            "采访时间",
            "采访对象",
            "采访记录",
            "观察记录",
            "养蚕计划",
            "小组",
        )
    ):
        return True
    if re.match(r"^问[：:]", plain) or re.match(r"^答[：:]", plain):
        return True
    if re.match(r"^\d+\.", plain):
        return True
    if atoms is not None:
        for other in atoms:
            if other is atom or other.get("atom_type") not in ("text", "title"):
                continue
            ot = re.sub(r"\s+", "", _text(other))
            if not any(
                k in ot
                for k in ("采访录", "观察记录", "养蚕计划", "采访记录")
            ):
                continue
            if _text_column_overlap_ratio(atom, other) >= 0.35:
                ob, tb = _bbox(other), _bbox(atom)
                if abs(_y_center(atom) - _y_center(other)) <= 0.42:
                    return True
                if tb["y0"] >= ob["y0"] - 0.02 and tb["y0"] - ob["y1"] <= 0.38:
                    return True
    return False


def _cluster_process_record_atoms(
    rec_atoms: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """同一记事本块内 OCR 碎段：按列对齐 + 纵向邻近聚类。"""
    if len(rec_atoms) < 2:
        return []
    ordered = sorted(
        rec_atoms,
        key=lambda a: (_bbox(a)["y0"], _bbox(a)["x0"]),
    )
    clusters: list[list[dict[str, Any]]] = []
    for atom in ordered:
        placed = False
        for cluster in clusters:
            if not any(
                _text_column_overlap_ratio(atom, member) >= 0.30 for member in cluster
            ):
                continue
            ref = cluster[-1]
            gap = _bbox(atom)["y0"] - _bbox(ref)["y1"]
            if gap <= 0.38:
                cluster.append(atom)
                placed = True
                break
        if not placed:
            clusters.append([atom])
    return clusters


def atom_conflicts_with_record_context_cluster(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]],
    *,
    trusted_role: str = "",
) -> bool:
    """不应归入 record/context 簇的原子（页底小标题条、说明句、气泡等）。"""
    if trusted_role == "process_record_block":
        return _is_definite_subtitle_module_atom(atom, atoms)
    if trusted_role == "problem_context_block":
        return _is_definite_subtitle_module_atom(atom, atoms)
    if _is_process_record_block_content(atom, atoms):
        return False
    if _is_definite_subtitle_module_atom(atom, atoms):
        return True
    if _is_task_instruction_line(atom, atoms=atoms):
        return True
    if (
        _looks_like_dialogue(_text(atom))
        or _is_dialogue_bubble_fragment(atom, atoms)
        or _is_likely_dialogue_bubble_line(atom, atoms)
    ):
        return True
    return False


def _is_definite_subtitle_module_atom(
    atom: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> bool:
    """页顶/页底活动小标题条（小人+条内汉字），不含记事本内标题如「养蚕计划」。"""
    if atom.get("atom_type") == "image":
        return _is_subtitle_bar_left_icon(atom, atoms) or (
            _is_activity_bar_icon(atom, atoms)
            and _is_subtitle_bar_icon_band(atom)
        )
    if _text_beside_subtitle_bar_icon(atom, atoms):
        return True
    b = _bbox(atom)
    if b["y0"] > 0.66 and _is_subtitle_bar_text_shape(atom):
        return True
    if _is_section_subheading_text(atom) and b["y0"] > 0.66:
        return True
    return False


def strip_instruction_from_dialogue_merge_groups(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """说明句不得与气泡 merge；仅保留气泡内 eligible 子组。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    merge_out: list[dict[str, Any]] = []
    stripped = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        inst = [
            c
            for c in codes
            if c in by_code
            and (
                _is_task_instruction_line(by_code[c], atoms=atoms)
                or _looks_like_activity_instruction(_text(by_code[c]))
                or _looks_like_instructional_body_paragraph(_text(by_code[c]))
            )
        ]
        dlg = _filter_dialogue_bubble_merge_codes(codes, atoms)
        if inst and dlg:
            stripped += 1
            subgroups = _split_merge_group_by_dialogue_affinity(dlg, atoms)
            reason = g.get("reason") or "同一对话气泡内 OCR 碎段"
            for sub in subgroups:
                if len(sub) >= 2:
                    merge_out.append({**g, "atom_codes": sub, "reason": reason})
            continue
        merge_out.append({**g, "atom_codes": codes})
    warnings = list(plan.get("warnings") or [])
    if stripped:
        warnings.append(f"规则：已剥离 {stripped} 组说明句与气泡错误合并")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def strip_instruction_dialogue_subtitle_cross_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """说明段/任务说明、对话气泡、小标题条不得跨类型合并；跨类时拆回各类内部子组。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    roles = _roles_by_code(atom_roles)
    merge_out: list[dict[str, Any]] = []
    split_groups = 0
    dropped = 0

    def _looks_like_subtitle_module(a: dict[str, Any]) -> bool:
        return _is_definite_subtitle_module_atom(a, atoms)

    def _classify_code(code: str) -> str:
        if code not in by_code:
            return "other"
        a = by_code[code]
        role = roles.get(code, "")
        if role == "process_record_block":
            if _looks_like_subtitle_module(a):
                return "subtitle"
            b = _bbox(a)
            if b["y0"] < 0.10 and (
                _is_task_instruction_line(a, atoms=atoms)
                or _looks_like_activity_instruction(_text(a))
                or _looks_like_instructional_body_paragraph(_text(a))
            ):
                return "instruction"
            if b["x0"] > 0.48 and (
                _looks_like_dialogue(_text(a))
                or _is_dialogue_bubble_fragment(a, atoms)
                or _is_likely_dialogue_bubble_line(a, atoms)
            ):
                return "dialogue"
            return "record"
        if role == "problem_context_block":
            if _looks_like_subtitle_module(a):
                return "subtitle"
            return "context"
        if role == "instruction_line":
            return "instruction"
        if role in _LLM_DIALOGUE_ROLES:
            if _looks_like_subtitle_module(a):
                return "subtitle"
            if (
                _is_task_instruction_line(a, atoms=atoms)
                or _looks_like_activity_instruction(_text(a))
                or _looks_like_instructional_body_paragraph(_text(a))
            ):
                return "instruction"
            return "dialogue"
        if role in _LLM_BAR_ROLES or role == "subtitle_bar":
            return "subtitle"
        if _looks_like_subtitle_module(a):
            return "subtitle"
        if _is_task_instruction_line(a, atoms=atoms) or _looks_like_activity_instruction(
            _text(a)
        ) or _looks_like_instructional_body_paragraph(_text(a)):
            return "instruction"
        if (
            _looks_like_dialogue(_text(a))
            or _is_dialogue_bubble_fragment(a, atoms)
            or _is_likely_dialogue_bubble_line(a, atoms)
        ):
            return "dialogue"
        return "other"

    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        buckets: dict[str, list[str]] = {
            "instruction": [],
            "dialogue": [],
            "subtitle": [],
            "record": [],
            "context": [],
            "other": [],
        }
        for c in codes:
            buckets[_classify_code(c)].append(c)
        kinds = [k for k, vals in buckets.items() if vals and k != "other"]
        cross = len(kinds) >= 2
        if not cross:
            merge_out.append({**g, "atom_codes": codes})
            continue
        split_groups += 1
        reason = g.get("reason") or ""
        for kind, subset in buckets.items():
            if kind == "other" or len(subset) < 2:
                continue
            if kind == "dialogue":
                for sub in _split_merge_group_by_dialogue_affinity(subset, atoms):
                    if len(sub) >= 2:
                        merge_out.append(
                            {
                                **g,
                                "atom_codes": sub,
                                "reason": reason or "同一对话气泡内 OCR 碎段",
                            }
                        )
            elif kind == "subtitle":
                merge_out.append(
                    {
                        **g,
                        "atom_codes": subset,
                        "reason": reason or "活动小标题条",
                    }
                )
            elif kind in ("record", "context"):
                merge_out.append(
                    {
                        **g,
                        "atom_codes": subset,
                        "reason": reason or "封闭模块整块合并",
                    }
                )
            else:
                dropped += 1
    warnings = list(plan.get("warnings") or [])
    if split_groups:
        warnings.append(
            f"规则：已拆分 {split_groups} 组跨模块（说明/气泡/小标题/记录/情境）错误合并"
        )
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def strip_llm_role_cross_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """拆分标题条与对话气泡的错误合并（优先用大模型 role，无 role 时用启发式）。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    roles = _roles_by_code(atom_roles)
    merge_out: list[dict[str, Any]] = []
    dropped = 0
    for g in plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
        ]
        if len(codes) < 2:
            continue
        atoms_in = [by_code[c] for c in codes if c in by_code]
        if roles:
            role_set = {roles.get(c) for c in codes if roles.get(c)}
            has_bar = bool(role_set & _LLM_BAR_ROLES)
            has_dialogue = bool(role_set & _LLM_DIALOGUE_ROLES)
        else:
            has_bar = any(
                _is_subtitle_bar_text(a, atoms) or _is_section_subheading_text(a)
                for a in atoms_in
            )
            has_dialogue = any(
                _looks_like_dialogue(_text(a))
                or _is_dialogue_bubble_fragment(a, atoms)
                for a in atoms_in
                if a.get("atom_type") in ("text", "title")
            )
        if not (has_bar and has_dialogue):
            merge_out.append({**g, "atom_codes": codes})
            continue
        bar_codes = [
            c
            for c in codes
            if c in by_code
            and (
                roles.get(c) in _LLM_BAR_ROLES
                or _is_activity_bar_icon(by_code[c], atoms)
                or _is_subtitle_bar_text(by_code[c], atoms)
                or _is_section_subheading_text(by_code[c])
                or _looks_like_pinyin_line(_text(by_code[c]))
            )
        ]
        dlg_codes = [
            c
            for c in codes
            if c in by_code
            and c not in bar_codes
            and (
                roles.get(c) in _LLM_DIALOGUE_ROLES
                or _looks_like_dialogue(_text(by_code[c]))
                or _is_dialogue_bubble_fragment(by_code[c], atoms)
            )
        ]
        if len(bar_codes) >= 2:
            merge_out.append(
                {
                    **g,
                    "atom_codes": bar_codes,
                    "reason": "标题条/活动条：图标+拼音+汉字",
                }
            )
        if len(dlg_codes) >= 2:
            merge_out.append(
                {
                    **g,
                    "atom_codes": dlg_codes,
                    "reason": "同一对话气泡内 OCR 碎段",
                }
            )
        dropped += 1
    warnings = list(plan.get("warnings") or [])
    if dropped:
        warnings.append(f"规则：已拆分 {dropped} 组标题条与对话错误合并")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def _extract_llm_protected_merge_groups(
    plan: dict[str, Any],
    atom_roles: list[dict[str, Any]] | None,
    atoms: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 LLM 明确标注 bubble_id / context_id / record_id 的合并组分离出来保护。

    返回 (protected_groups, other_groups)。
    """
    if not atom_roles:
        return [], list(plan.get("merge_groups") or [])

    from collections import defaultdict

    by_code = {str(a["atom_code"]): a for a in (atoms or [])}
    bubble_buckets: dict[str, set[str]] = defaultdict(set)
    context_buckets: dict[str, set[str]] = defaultdict(set)
    record_buckets: dict[str, set[str]] = defaultdict(set)
    for raw in atom_roles:
        code = str(raw.get("atom_code") or "").strip()
        if not code:
            continue
        role = str(raw.get("role") or "").strip()
        bubble_id = str(raw.get("bubble_id") or "").strip()
        context_id = str(raw.get("context_id") or "").strip()
        record_id = str(raw.get("record_id") or "").strip()
        if bubble_id and role in ("dialogue_bubble", "dialogue_fragment"):
            bubble_buckets[bubble_id].add(code)
        if context_id and role == "problem_context_block":
            if atoms and code in by_code and atom_conflicts_with_record_context_cluster(
                by_code[code], atoms, trusted_role="problem_context_block"
            ):
                continue
            context_buckets[context_id].add(code)
        if record_id and role == "process_record_block":
            if atoms and code in by_code and atom_conflicts_with_record_context_cluster(
                by_code[code], atoms, trusted_role="process_record_block"
            ):
                continue
            record_buckets[record_id].add(code)

    protected_groups: list[dict[str, Any]] = []
    other_groups: list[dict[str, Any]] = []
    for g in plan.get("merge_groups") or []:
        codes = set(str(c).strip() for c in (g.get("atom_codes") or []) if str(c).strip())
        if len(codes) < 2:
            continue
        is_bubble = False
        if atoms:
            for bk in bubble_buckets.values():
                if len(bk) < 2:
                    continue
                eligible = set(_filter_dialogue_bubble_merge_codes(sorted(bk), atoms))
                if len(eligible) >= 2 and codes <= eligible:
                    is_bubble = True
                    break
        else:
            is_bubble = any(
                codes <= bk for bk in bubble_buckets.values() if len(bk) >= 2
            )
        is_context = any(codes <= ck for ck in context_buckets.values() if len(ck) >= 2)
        is_record = any(codes <= rk for rk in record_buckets.values() if len(rk) >= 2)
        if is_bubble or is_context or is_record:
            protected_groups.append(g)
        else:
            other_groups.append(g)

    return protected_groups, other_groups


def _extract_llm_bubble_groups(
    plan: dict[str, Any],
    atom_roles: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """兼容旧名：见 _extract_llm_protected_merge_groups。"""
    return _extract_llm_protected_merge_groups(plan, atom_roles)


def _filter_protected_merge_groups(
    protected_groups: list[dict[str, Any]],
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """受保护组合并组也须过跨模块拆分，避免整页 record_id 把页底小标题一并锁死。"""
    if not protected_groups:
        return []
    roles = atom_roles if atom_roles else (plan.get("atom_roles") or [])
    filtered = {
        **plan,
        "merge_groups": protected_groups,
        "warnings": list(plan.get("warnings") or []),
    }
    filtered = strip_instruction_dialogue_subtitle_cross_merges(
        filtered, atoms, atom_roles=roles
    )
    if roles:
        filtered = strip_llm_role_cross_merges(filtered, atoms, roles)
    return filtered.get("merge_groups") or []


def inject_minimal_subtitle_bar_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """minimal 兜底：页顶/页底活动小标题条（小人图标+条内汉字）。"""
    block_codes: set[str] = set()
    for raw in atom_roles or plan.get("atom_roles") or []:
        role = str(raw.get("role") or "").strip()
        if role in ("process_record_block", "problem_context_block"):
            code = str(raw.get("atom_code") or "").strip()
            if code:
                block_codes.add(code)
    used: set[str] = set()
    for g in plan.get("merge_groups") or []:
        used.update(g.get("atom_codes") or [])
    delete_codes = set(plan.get("delete_codes") or []) | set(plan.get("split_codes") or [])
    groups = detect_activity_bar_merge_groups(
        atoms, protected=used | delete_codes | block_codes
    )
    merge_out = list(plan.get("merge_groups") or [])
    added = 0
    for g in groups:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
            and str(c).strip() not in delete_codes
            and str(c).strip() not in block_codes
        ]
        if len(codes) < 2:
            continue
        code_set = set(codes)
        if code_set.intersection(used):
            continue
        merge_out.append({**g, "atom_codes": codes})
        used.update(code_set)
        added += 1
    warnings = list(plan.get("warnings") or [])
    if added:
        warnings.append(f"minimal：补并 {added} 组活动小标题条")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def inject_minimal_dialogue_bubble_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """minimal 兜底：仅补同一气泡 OCR 碎段 merge（不含标题条/大框/采访录等规则）。"""
    block_codes: set[str] = set()
    for raw in atom_roles or plan.get("atom_roles") or []:
        role = str(raw.get("role") or "").strip()
        if role in ("process_record_block", "problem_context_block"):
            code = str(raw.get("atom_code") or "").strip()
            if code:
                block_codes.add(code)
    used: set[str] = set()
    for g in plan.get("merge_groups") or []:
        used.update(g.get("atom_codes") or [])
    delete_codes = set(plan.get("delete_codes") or []) | set(plan.get("split_codes") or [])
    groups = detect_dialogue_merge_groups(atoms)
    merge_out = list(plan.get("merge_groups") or [])
    added = 0
    for g in groups:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip()
            and str(c).strip() not in delete_codes
            and str(c).strip() not in block_codes
        ]
        if len(codes) < 2:
            continue
        code_set = set(codes)
        if code_set.intersection(used):
            continue
        merge_out.append({**g, "atom_codes": codes})
        used.update(code_set)
        added += 1
    warnings = list(plan.get("warnings") or [])
    if added:
        warnings.append(f"minimal：补并 {added} 组气泡 OCR 碎段")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def _restore_llm_protected_merge_groups(
    plan: dict[str, Any],
    working_plan: dict[str, Any],
    protected_groups: list[dict[str, Any]],
    *,
    atoms: list[dict[str, Any]] | None = None,
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把受保护的 LLM 合并组加回（去掉与其他组重复的 code；须过跨模块拆分）。"""
    if not protected_groups:
        return working_plan
    used_codes: set[str] = set()
    for g in working_plan.get("merge_groups") or []:
        used_codes.update(g.get("atom_codes") or [])
    roles = atom_roles if atom_roles else (plan.get("atom_roles") or [])
    for bg in protected_groups:
        to_add = [bg]
        if atoms:
            stripped = strip_instruction_dialogue_subtitle_cross_merges(
                {
                    **plan,
                    "merge_groups": to_add,
                    "warnings": list(plan.get("warnings") or []),
                },
                atoms,
                atom_roles=roles,
            )
            to_add = stripped.get("merge_groups") or []
        for grp in to_add:
            codes = [c for c in (grp.get("atom_codes") or []) if c not in used_codes]
            if len(codes) >= 2:
                working_plan["merge_groups"] = list(
                    working_plan.get("merge_groups") or []
                ) + [{**grp, "atom_codes": codes}]
                used_codes.update(codes)
    return working_plan


def _restore_llm_bubble_merge_groups(
    plan: dict[str, Any],
    working_plan: dict[str, Any],
    bubble_groups: list[dict[str, Any]],
    *,
    atoms: list[dict[str, Any]] | None = None,
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """兼容旧名。"""
    return _restore_llm_protected_merge_groups(
        plan, working_plan, bubble_groups, atoms=atoms, atom_roles=atom_roles
    )


def _sanitize_llm_curate_plan_minimal(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """大模型主路径（默认）：页码/误删护栏 + role 校验；不注入规则 merge。"""
    protected_groups, other_groups = _extract_llm_protected_merge_groups(
        plan, atom_roles, atoms
    )
    protected_groups = _filter_protected_merge_groups(
        protected_groups, plan, atoms, atom_roles
    )
    working_plan = {**plan, "merge_groups": other_groups}

    working_plan = strip_page_numbers_from_curate_plan(working_plan, atoms)
    roles = atom_roles if atom_roles else None
    if roles:
        working_plan = strip_llm_role_cross_merges(working_plan, atoms, roles)
    working_plan = strip_instruction_dialogue_subtitle_cross_merges(
        working_plan, atoms, atom_roles=working_plan.get("atom_roles") or atom_roles
    )
    working_plan = strip_invalid_illustration_merges_from_plan(working_plan, atoms)
    working_plan = restrict_curate_deletes_for_llm(working_plan, atoms, atom_roles)
    from .atom_curate_suggest import supplement_merge_groups_from_atom_roles

    working_plan = supplement_merge_groups_from_atom_roles(
        working_plan, atoms, atom_roles=working_plan.get("atom_roles") or atom_roles
    )
    working_plan = inject_minimal_subtitle_bar_merges(
        working_plan, atoms, atom_roles=working_plan.get("atom_roles") or atom_roles
    )
    working_plan = inject_minimal_dialogue_bubble_merges(
        working_plan, atoms, atom_roles=working_plan.get("atom_roles") or atom_roles
    )
    working_plan = strip_instruction_dialogue_subtitle_cross_merges(
        working_plan, atoms, atom_roles=working_plan.get("atom_roles") or atom_roles
    )
    working_plan = _restore_llm_protected_merge_groups(
        plan, working_plan, protected_groups, atoms=atoms, atom_roles=atom_roles
    )
    working_plan = strip_instruction_dialogue_subtitle_cross_merges(
        working_plan, atoms, atom_roles=working_plan.get("atom_roles") or atom_roles
    )
    working_plan = strip_instruction_from_dialogue_merge_groups(working_plan, atoms)
    working_plan = _refine_dialogue_merge_groups_in_plan(working_plan, atoms)

    if plan.get("atom_roles"):
        working_plan = {**working_plan, "atom_roles": plan.get("atom_roles")}
    working_plan = ensure_all_atoms_accounted_for(working_plan, atoms)
    return working_plan


def _sanitize_llm_curate_plan_full(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """规则增强路径：撤销/补并阈值规则 + 气泡亲和拆分（湘科版调优）。"""
    protected_groups, other_groups = _extract_llm_protected_merge_groups(
        plan, atom_roles, atoms
    )
    protected_groups = _filter_protected_merge_groups(
        protected_groups, plan, atoms, atom_roles
    )
    working_plan = {**plan, "merge_groups": other_groups}

    working_plan = strip_page_numbers_from_curate_plan(working_plan, atoms)
    working_plan = strip_invalid_illustration_merges_from_plan(working_plan, atoms)
    working_plan = strip_lesson_title_polluted_merges(working_plan, atoms)
    working_plan = strip_instruction_dialogue_subtitle_cross_merges(working_plan, atoms)
    working_plan = strip_sidebar_mascot_merges(working_plan, atoms)
    working_plan = strip_dialogue_scene_label_cross_merges(working_plan, atoms, atom_roles)
    roles = atom_roles if atom_roles else None
    if roles:
        working_plan = strip_llm_role_cross_merges(working_plan, atoms, roles)
    working_plan = inject_curate_fallback_merges(working_plan, atoms)
    working_plan = strip_lesson_title_polluted_merges(working_plan, atoms)
    working_plan = strip_instruction_dialogue_subtitle_cross_merges(working_plan, atoms)
    working_plan = restrict_curate_deletes_for_llm(working_plan, atoms, atom_roles)
    working_plan = _restore_llm_protected_merge_groups(plan, working_plan, protected_groups)

    if plan.get("atom_roles"):
        working_plan = {**working_plan, "atom_roles": plan.get("atom_roles")}
    working_plan = _refine_dialogue_merge_groups_in_plan(working_plan, atoms)
    working_plan = ensure_all_atoms_accounted_for(working_plan, atoms)
    return working_plan


def sanitize_llm_curate_plan(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
    *,
    mode: Literal["minimal", "full"] | None = None,
) -> dict[str, Any]:
    """校验 LLM 整理计划。默认 minimal（大模型为主）；full 启用规则补并/撤销。"""
    effective = mode or atom_curate_heuristics_mode()
    if effective == "full":
        return _sanitize_llm_curate_plan_full(plan, atoms, atom_roles)
    return _sanitize_llm_curate_plan_minimal(plan, atoms, atom_roles)


def detect_subtitle_dialogue_span_text_codes(
    atoms: list[dict[str, Any]],
    *,
    protected: set[str],
) -> list[str]:
    """单 OCR 框内同时含小标题汉字与气泡台词（应删后重 OCR 或人工拆）。"""
    deletes: list[str] = []
    for a in atoms:
        code = str(a["atom_code"])
        if code in protected:
            continue
        if a.get("atom_type") not in ("text", "title"):
            continue
        text = _text(a)
        if not text or text.startswith("["):
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        hanzi_lines = [
            ln
            for ln in lines
            if re.search(r"[\u4e00-\u9fff]", ln)
            and not _looks_like_pinyin_line(ln)
        ]
        if not hanzi_lines:
            continue
        check_lines = hanzi_lines[:-1] if len(hanzi_lines) >= 2 else hanzi_lines
        subtitle_like = any(
            len(re.sub(r"\s+", "", ln)) >= 5
            and not any(m in ln for m in ("……", "…", "..."))
            and not ln.rstrip().endswith(("，", "。", "！", "？"))
            for ln in check_lines
        )
        dialogue_like = any(
            "，" in ln or "……" in ln or "…" in ln or ln.endswith("……")
            for ln in hanzi_lines[1:]
        ) or (
            len(hanzi_lines) >= 2
            and hanzi_lines[-1].rstrip().endswith(("，", "……", "…"))
        )
        if subtitle_like and dialogue_like and _height(a) > 0.08:
            deletes.append(code)
    return deletes


def restrict_curate_deletes_for_llm(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """LLM 整理：仅删页码和侧边栏单元标题；其余不删正文与插图。"""
    by_code = {str(a["atom_code"]): a for a in atoms}
    protected: set[str] = set()
    for g in plan.get("merge_groups") or []:
        protected.update(g.get("atom_codes") or [])

    mixed_split_codes = set(
        detect_mixed_dialogue_scene_label_span_codes(atoms, protected=protected)
    )
    mixed_split_codes.update(
        detect_instruction_bubble_span_codes(atoms, protected=protected)
    )
    mixed_split_codes.update(
        detect_lesson_topic_subtitle_span_codes(atoms, protected=protected)
    )
    mixed_split_codes.update(
        detect_lesson_title_subtitle_band_span_codes(atoms, protected=protected)
    )
    span_codes = set(
        detect_oversized_spanning_text_codes(atoms, protected=protected)
    )
    span_codes.update(
        detect_subtitle_dialogue_span_text_codes(atoms, protected=protected)
    )
    span_codes -= mixed_split_codes

    raw_deletes = [str(c).strip() for c in (plan.get("delete_codes") or []) if str(c).strip()]

    # LLM 标注为侧边栏单元标题的原子允许删除，先收集出来
    effective_roles = atom_roles or (plan.get("atom_roles") or [])
    sidebar_codes = {
        str(r.get("atom_code") or "").strip()
        for r in effective_roles
        if str(r.get("role") or "").strip() == "sidebar_unit_title"
        and str(r.get("atom_code") or "").strip() in by_code
        and str(r.get("atom_code") or "").strip() not in protected
    }

    restricted = restrict_curate_deletes_to_page_numbers(plan, atoms)
    delete_set = set(restricted.get("delete_codes") or [])

    # 把 LLM 认定的侧边栏标题加入删除集
    if sidebar_codes:
        delete_set.update(c for c in raw_deletes if c in sidebar_codes)

    warnings = list(plan.get("warnings") or [])
    warnings.extend(restricted.get("warnings") or [])
    if mixed_split_codes:
        warnings.append(
            f"规则：建议拆分 {len(mixed_split_codes)} 个粘连 OCR 框（气泡+标牌 / 引导问句+小标题）"
        )
    if span_codes:
        warnings.append(
            f"规则：检测到 {len(span_codes)} 个蔓延 OCR 框，已保留（靠合并/拆分整理，不自动删除）"
        )
    ignored_content = [
        c
        for c in raw_deletes
        if c in by_code and c not in delete_set
    ]
    if ignored_content:
        warnings.append(
            f"规则：已忽略 {len(ignored_content)} 个非页码删除项（正文/说明/插图不可删）"
        )
    return {
        **plan,
        "delete_codes": sorted(delete_set),
        "split_codes": sorted(mixed_split_codes),
        "warnings": warnings,
    }


def inject_curate_fallback_merges(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """LLM 漏并时补：大标题、小标题条、气泡碎段、插图内标牌。"""
    delete_codes = set(plan.get("delete_codes") or []) | set(plan.get("split_codes") or [])
    mandatory: list[dict[str, Any]] = []
    dialogue_groups = detect_dialogue_merge_groups(atoms)
    mandatory.extend(dialogue_groups)
    dialogue_codes = {c for g in dialogue_groups for c in (g.get("atom_codes") or [])}
    scene_groups = detect_scene_label_into_illustration_merges(
        atoms, protected=delete_codes | dialogue_codes
    )
    mandatory.extend(scene_groups)
    title_groups = detect_lesson_title_bar_merge_groups(atoms, protected=delete_codes)
    mandatory.extend(title_groups)
    title_codes = {c for g in title_groups for c in (g.get("atom_codes") or [])}
    mandatory.extend(
        detect_activity_bar_merge_groups(
            atoms, protected=delete_codes | title_codes | dialogue_codes
        )
    )

    merge_out: list[dict[str, Any]] = list(plan.get("merge_groups") or [])
    used_codes: set[str] = set()
    for g in merge_out:
        used_codes.update(g.get("atom_codes") or [])

    added = 0
    for g in mandatory:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip() and str(c).strip() not in delete_codes
        ]
        if len(codes) < 2:
            continue
        code_set = set(codes)
        if code_set.issubset(used_codes):
            continue
        overlap = used_codes.intersection(code_set)
        if overlap:
            trimmed: list[dict[str, Any]] = []
            for mg in merge_out:
                kept = [
                    str(c).strip()
                    for c in (mg.get("atom_codes") or [])
                    if str(c).strip() and str(c).strip() not in code_set
                ]
                if len(kept) >= 2:
                    trimmed.append({**mg, "atom_codes": kept})
            merge_out = trimmed
            used_codes = set()
            for mg in merge_out:
                used_codes.update(mg.get("atom_codes") or [])
            if code_set.issubset(used_codes):
                continue
        merge_out.append({**g, "atom_codes": codes})
        used_codes.update(codes)
        added += 1

    warnings = list(plan.get("warnings") or [])
    if added:
        warnings.append(f"规则：补全 {added} 组 LLM 漏并（大标题/气泡）")
    return {**plan, "merge_groups": merge_out, "warnings": warnings}


def build_minimal_heuristic_supplement(
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """仅页码删除建议；合并完全交给视觉大模型。"""
    delete_codes = detect_page_number_codes(atoms)
    warnings: list[str] = []
    if delete_codes:
        warnings.append(f"规则：建议删除 {len(delete_codes)} 个页码")
    return {
        "merge_groups": [],
        "delete_codes": sorted(set(delete_codes)),
        "warnings": warnings,
    }


def merge_curate_plans_llm_first(
    llm_plan: dict[str, Any],
    heuristic_plan: dict[str, Any],
) -> dict[str, Any]:
    """大模型合并组优先；规则仅补未覆盖原子且不与 LLM 组重叠的合并。"""
    merge_out: list[dict[str, Any]] = list(llm_plan.get("merge_groups") or [])
    used_codes: set[str] = set()
    for g in merge_out:
        used_codes.update(g.get("atom_codes") or [])

    for g in heuristic_plan.get("merge_groups") or []:
        codes = [
            str(c).strip()
            for c in (g.get("atom_codes") or [])
            if str(c).strip() and str(c).strip() not in used_codes
        ]
        if len(codes) < 2:
            continue
        if used_codes.intersection(codes):
            continue
        merge_out.append({**g, "atom_codes": codes})
        used_codes.update(codes)

    delete_set = set(heuristic_plan.get("delete_codes") or [])
    delete_set.update(llm_plan.get("delete_codes") or [])
    for g in merge_out:
        for c in g.get("atom_codes") or []:
            delete_set.discard(c)

    warnings = list(llm_plan.get("warnings") or []) + list(
        heuristic_plan.get("warnings") or []
    )
    out: dict[str, Any] = {
        "merge_groups": merge_out,
        "delete_codes": sorted(delete_set),
        "warnings": warnings,
    }
    if llm_plan.get("atom_roles"):
        out["atom_roles"] = llm_plan.get("atom_roles")
    return out


def ensure_all_atoms_accounted_for(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
) -> dict[str, Any]:
    """最终兜底：确保每个原子都被 delete_codes 或 merge_groups 覆盖。

    LLM 可能遗漏个别原子，这些原子将在 merge→delete 后从 DB 中消失。
    此函数检测孤雁并发出 warning，阻止静默丢失。
    """
    delete_codes = set(plan.get("delete_codes") or [])
    split_codes = set(plan.get("split_codes") or [])

    accounted: set[str] = set(delete_codes) | set(split_codes)
    for g in plan.get("merge_groups") or []:
        accounted.update(g.get("atom_codes") or [])

    all_codes = {str(a["atom_code"]) for a in atoms if str(a["atom_code"])}
    orphans = all_codes - accounted
    if not orphans:
        return plan

    by_code = {str(a["atom_code"]): a for a in atoms}
    real_orphans = {
        c for c in orphans
        if not (c.endswith("-FULL-001") or "-GAP-" in c)
    }
    page_orphans = {
        c for c in real_orphans
        if c in by_code and _is_page_number_atom(by_code[c])
    }
    content_orphans = real_orphans - page_orphans

    warnings = list(plan.get("warnings") or [])
    if content_orphans:
        warnings.append(
            f"兜底：{len(content_orphans)} 个原子未在整理计划中，将单独保留"
            + f"（{', '.join(sorted(content_orphans)[:6])}"
            + ("…" if len(content_orphans) > 6 else "") + "）"
        )

    return {**plan, "warnings": warnings}

