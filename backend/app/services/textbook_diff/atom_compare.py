"""预览页原子 OCR（对齐新库建块两阶段流程；暂不做锚定）。"""
from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Literal

from ...models import FileBlob, Volume
from ...parsers.pdf_spread import layout_for_volume, render_view_page_png
from ...parsers.textbook_atom_extract import (
    extract_atoms_for_textbook_page,
    is_spurious_image_atom,
)
from ...repo_paths import base_data_dir
from .catalog import _pdf_path_for_volume
from .preview_compare import _norm, _ocr_equivalent

PdfSource = str  # "full" | "draft"
OcrPhase = Literal["text", "images"]
PageSide = Literal["old", "new"]

_log = logging.getLogger(__name__)

_CACHE_VERSION = 17  # v17: 列表方块符漏识补齐
# 比对结果单独版本：升级比对逻辑时不必整页作废 OCR
_TEXT_COMPARE_CACHE_VERSION = 51  # v51: 仅注音/音标差异 →「音标变动注意」
_IMAGE_COMPARE_CACHE_VERSION = 3  # v3: C1 用版面标签+匹配本页文句命名图义
_TEXT_TYPES = frozenset({"text", "title"})
_IMAGE_TYPES = frozenset({"image"})
_FIGURE_ROLES = frozenset({"figure_label", "figure_caption"})

# 全半角/形近标点 OCR 软归一（仅用于判定是否「真改」）
_SOFT_PUNCT_MAP = str.maketrans(
    {
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "［": "[",
        "］": "]",
        "〔": "[",  # 六角括号 ≈ 方括号（注释词条包裹）
        "〕": "]",
        "〖": "[",
        "〗": "]",
        "｛": "{",
        "｝": "}",
        "，": ",",
        "、": ",",
        "。": ".",
        "．": ".",
        "；": ";",
        "：": ":",
        "？": "?",
        "！": "!",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "—": "-",
        "–": "-",
        "−": "-",
        "·": "",
        "•": "",
        "●": "",
    }
)

# 课标题序号：① ≈ 1（OCR/版式常互换，非正文改写）
_CIRCLED_NUM_MAP = str.maketrans(
    {
        "①": "1",
        "②": "2",
        "③": "3",
        "④": "4",
        "⑤": "5",
        "⑥": "6",
        "⑦": "7",
        "⑧": "8",
        "⑨": "9",
        "⒈": "1",
        "⒉": "2",
        "⒊": "3",
        "⒋": "4",
        "⒌": "5",
        "⒍": "6",
        "⒎": "7",
        "⒏": "8",
        "⒐": "9",
        "１": "1",
        "２": "2",
        "３": "3",
        "４": "4",
        "５": "5",
        "６": "6",
        "７": "7",
        "８": "8",
        "９": "9",
        "０": "0",
    }
)
_CIRCLED_NUM_MULTI = (
    ("⑳", "20"),
    ("⑲", "19"),
    ("⑱", "18"),
    ("⑰", "17"),
    ("⑯", "16"),
    ("⑮", "15"),
    ("⑭", "14"),
    ("⑬", "13"),
    ("⑫", "12"),
    ("⑪", "11"),
    ("⑩", "10"),
    ("⒑", "10"),
)
# 脚注上标/下标 ¹ ² ³（OCR 常与带圈 ①②③ 互换）
_SUPERSCRIPT_MARK_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉]")
_SUPERSCRIPT_TO_DIGIT = str.maketrans(
    {
        "⁰": "0",
        "¹": "1",
        "²": "2",
        "³": "3",
        "⁴": "4",
        "⁵": "5",
        "⁶": "6",
        "⁷": "7",
        "⁸": "8",
        "⁹": "9",
        "₀": "0",
        "₁": "1",
        "₂": "2",
        "₃": "3",
        "₄": "4",
        "₅": "5",
        "₆": "6",
        "₇": "7",
        "₈": "8",
        "₉": "9",
    }
)
# 课标题装饰：选学星号、角标星等（OCR 常一侧有一侧无）
_LESSON_ORNAMENT_RE = re.compile(r"[\*＊★☆※]")
# 课文脚注角标：带圈 / 上标 / 下标（同一序号的多种 OCR 形态）
_FOOTNOTE_MARK_CHARS = (
    "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    "⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉"
)


def _normalize_lesson_markers(s: str) -> str:
    """课序号/脚注/选学星等软归一：比对用，不改展示原文。"""
    t = s or ""
    for src, dst in _CIRCLED_NUM_MULTI:
        t = t.replace(src, dst)
    t = t.translate(_CIRCLED_NUM_MAP)
    # ³ → 3（与 ③→3 对齐）；禁止直接删上标，否则 ③↔³ 会误成「3」↔空
    t = t.translate(_SUPERSCRIPT_TO_DIGIT)
    # 3* / ③* / 标题旁装饰星 → 去掉
    t = _LESSON_ORNAMENT_RE.sub("", t)
    # 「3 *」去星后残留双空格，压成单空再比
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _has_lesson_marker_chars(s: str) -> bool:
    t = s or ""
    if _SUPERSCRIPT_MARK_RE.search(t) or _LESSON_ORNAMENT_RE.search(t):
        return True
    if any(ch in t for ch in "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳⒈⒉⒊⒋⒌⒍⒎⒏⒐⒑１２３４５６７８９０"):
        return True
    return False


def _is_lesson_ornament_only(s: str) -> bool:
    """仅选学星/角标装饰（可夹空白），无实质文字。"""
    t = re.sub(r"[\s\u200b\u200c\u200d\ufeff\u00a0]+", "", s or "")
    if not t:
        return False
    t = _LESSON_ORNAMENT_RE.sub("", t)
    t = _SUPERSCRIPT_MARK_RE.sub("", t)
    return not t


def _soft_lesson_marker_equivalent(a: str, b: str) -> bool:
    """①↔1、3*↔3、脚注上标有无等：归一后相同且至少一侧带课标记/装饰。"""
    if (a or "") == (b or ""):
        return True
    if _normalize_lesson_markers(a) != _normalize_lesson_markers(b):
        return False
    return _has_lesson_marker_chars(a) or _has_lesson_marker_chars(b)


_DIGIT_TO_CIRCLED = {
    "1": "①",
    "2": "②",
    "3": "③",
    "4": "④",
    "5": "⑤",
    "6": "⑥",
    "7": "⑦",
    "8": "⑧",
    "9": "⑨",
    "１": "①",
    "２": "②",
    "３": "③",
    "４": "④",
    "５": "⑤",
    "６": "⑥",
    "７": "⑦",
    "８": "⑧",
    "９": "⑨",
}
_CIRCLED_SET = frozenset("①②③④⑤⑥⑦⑧⑨⑩")


def _looks_like_short_lesson_title(text: str) -> bool:
    """短课标题：③* 现代诗二首 / 3 观潮 等。"""
    t = (text or "").strip()
    if not t or len(t) > 40 or "\n" in t:
        return False
    return bool(
        re.match(
            r"^[①②③④⑤⑥⑦⑧⑨⑩0-9０-９]\s*[\*＊]?\s*[\u4e00-\u9fff《「].{0,28}$",
            t,
        )
    )


def _canonicalize_lesson_title_display(text: str, *, force_star: bool = False) -> str:
    """课标题展示：普通数字升为带圈号；可选补选学星。"""
    t = (text or "").strip()
    if not _looks_like_short_lesson_title(t):
        return text or ""
    m = re.match(
        r"^([①②③④⑤⑥⑦⑧⑨⑩0-9０-９])\s*([\*＊]?)\s*(.+)$",
        t,
    )
    if not m:
        return text or ""
    num, star, rest = m.group(1), m.group(2), m.group(3).strip()
    circled = num if num in _CIRCLED_SET else _DIGIT_TO_CIRCLED.get(num, num)
    star_out = "*" if (star or force_star) else ""
    return f"{circled}{star_out} {rest}".strip()


def _unify_lesson_title_pair(ot: str, nt: str) -> tuple[str, str]:
    """
    课标题展示对齐：数字→圈号；若任一侧有选学星且实质相同，两侧都补 *。
    """
    o0 = (ot or "").strip()
    n0 = (nt or "").strip()
    soft = _soft_lesson_marker_equivalent(o0, n0)
    force_star = soft and (
        bool(_LESSON_ORNAMENT_RE.search(o0)) or bool(_LESSON_ORNAMENT_RE.search(n0))
    )
    return (
        _canonicalize_lesson_title_display(o0, force_star=force_star),
        _canonicalize_lesson_title_display(n0, force_star=force_star),
    )


def _atom_plain(atom: dict) -> str:
    from ..llm.page_text_extract import normalize_inline_pinyin_clusters

    plain = normalize_inline_pinyin_clusters(
        (atom.get("content") or atom.get("ocr_text") or "").strip()
    )
    return _peel_glued_exercise_from_word_bank(plain)


def _ymid(atom: dict) -> float:
    return (float(atom.get("y_start", 0)) + float(atom.get("y_end", 0))) / 2.0


def _serialize_atom(atom: dict, *, side: str) -> dict:
    return {
        "atom_id": atom.get("atom_id") or "",
        "atom_type": atom.get("atom_type") or "text",
        "side": side,
        "bbox": {
            "x_start": float(atom.get("x_start", 0)),
            "y_start": float(atom.get("y_start", 0)),
            "x_end": float(atom.get("x_end", 1)),
            "y_end": float(atom.get("y_end", 1)),
        },
        "ocr_text": atom.get("ocr_text") or "",
        "content": atom.get("content") or "",
        "display_text": _atom_plain(atom),
    }


def _volume_label(volume: Volume) -> str:
    return str(getattr(volume, "volume_code", None) or getattr(volume, "id", "") or "vol")


def _doubao_text_atoms_for_page(
    *,
    volume: Volume,
    page_1: int,
    png_path: Path,
    source: str,
    force_refresh: bool = False,
    skip_post_audit: bool = False,
    audit_pending: dict | None = None,
) -> list[dict] | None:
    """与小学科学双轨一致：优先豆包视觉文字 OCR；失败返回 None 走 RapidOCR。

    附录识字表/写字表/词语表页启用附录分行模式；其它页走普通识别。
    """
    from ..llm.page_text_extract import (
        extract_page_text_regions_from_image,
        page_text_llm_enabled,
    )
    from .yuwen_appendix import volume_page_is_appendix_table

    if not page_text_llm_enabled():
        return None
    appendix = volume_page_is_appendix_table(volume, page_1)
    label = _volume_label(volume)
    ocr_png = _prepare_page_png_for_text_ocr(png_path)
    try:
        atoms, meta = extract_page_text_regions_from_image(
            cache_key=f"diff_{label}_{source}",
            page_index=page_1,
            image_path=ocr_png,
            force_refresh=force_refresh,
            appendix=appendix,
            subject=getattr(volume, "subject", None),
            skip_post_audit=skip_post_audit,
            audit_pending=audit_pending,
        )
    except Exception as exc:
        _log.warning("diff doubao text OCR exception %s p%d: %s", label, page_1, exc)
        return None
    atoms = _drop_watermark_atoms(atoms or [], skip_form_pair_merge=appendix)
    if atoms:
        _log.info(
            "diff doubao text OCR %s p%d: %d atoms (%s)%s",
            label,
            page_1,
            len(atoms),
            meta.get("source"),
            " [appendix]" if appendix else "",
        )
        return atoms
    _log.warning(
        "diff doubao text OCR fallback %s p%d: %s",
        label,
        page_1,
        meta.get("warning") or meta.get("source"),
    )
    return None


def _prepare_page_png_for_text_ocr(png_path: Path) -> Path:
    """文字 OCR 前去红章/手写红笔（与 RapidOCR 路径一致），写入旁路 PNG。"""
    try:
        import cv2
        from ...parsers.pdf_stamp_remove import prepare_lesson_page_bgr_from_path

        bgr, _mask = prepare_lesson_page_bgr_from_path(png_path)
        out = png_path.with_name(f"{png_path.stem}_ocrprep.png")
        if not cv2.imwrite(str(out), bgr):
            return png_path
        return out
    except Exception as exc:
        _log.debug("text OCR stamp prep skipped: %s", exc)
        return png_path


def _drop_watermark_atoms(
    atoms: list[dict],
    *,
    skip_form_pair_merge: bool = False,
) -> list[dict]:
    from ..llm.page_text_extract import (
        drop_orphan_pinyin_atoms,
        expand_atom_bbox_for_inline_pinyin,
        _looks_like_page_edge_scrap,
        _looks_like_watermark_text,
    )

    out: list[dict] = []
    for a in atoms or []:
        plain = _atom_plain(a)
        if _looks_like_watermark_text(plain):
            continue
        if _looks_like_page_edge_scrap(
            plain,
            x_start=float(a.get("x_start") or 0),
            y_start=float(a.get("y_start") or 0),
            x_end=float(a.get("x_end") or 1),
            y_end=float(a.get("y_end") or 1),
        ):
            continue
        out.append(expand_atom_bbox_for_inline_pinyin(dict(a)))
    cleaned = drop_orphan_pinyin_atoms(out)
    # 附录页跳过识字加油站合框，以免把模型逐行结果又并回去
    if skip_form_pair_merge:
        return cleaned
    return _merge_adjacent_form_pair_list_atoms(cleaned)


def _extract_page_atoms(
    volume: Volume,
    page_1: int,
    *,
    source: str,
    pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
    ocr_phase: str = "all",
    existing_text_atoms: list[dict] | None = None,
    force_llm_refresh: bool = False,
    skip_post_audit: bool = False,
    audit_pending: dict | None = None,
) -> list[dict]:
    from ..volume_pdf import resolve_volume_pdf_path

    if pdf_source == "draft":
        pdf_path = resolve_volume_pdf_path(
            volume,
            source="draft",
            preview_blob_id=preview_blob_id,
        )
    else:
        pdf_path = _pdf_path_for_volume(volume)
    with tempfile.TemporaryDirectory(prefix="diff_atom_") as td:
        png_path = Path(td) / "page.png"
        from ...parsers.pdf_spread import layout_for_volume, render_view_page_png

        layout = layout_for_volume(volume, persist=False)
        png_path.write_bytes(
            render_view_page_png(pdf_path, page_1, dpi=150, layout=layout)
        )
        sheet_1 = page_1
        if layout == "spread":
            from ...parsers.pdf_spread import view_to_sheet_side

            sheet_1, _ = view_to_sheet_side(page_1)

        # ① 文字：优先豆包（语文正文质量关键），否则 RapidOCR 两阶段
        if (ocr_phase or "").strip().lower() == "text":
            from .yuwen_appendix import volume_page_is_appendix_table

            appendix = volume_page_is_appendix_table(volume, page_1)
            doubao = _doubao_text_atoms_for_page(
                volume=volume,
                page_1=page_1,
                png_path=png_path,
                source=source,
                force_refresh=force_llm_refresh,
                skip_post_audit=skip_post_audit,
                audit_pending=audit_pending,
            )
            if doubao:
                return _drop_watermark_atoms(
                    doubao, skip_form_pair_merge=appendix
                )

        raw = extract_atoms_for_textbook_page(
            page_num=page_1,
            image_path=png_path,
            pdf_path=pdf_path,
            pdf_page_index=sheet_1 - 1,
            use_ocr=True,
            fill_gaps=(ocr_phase or "").strip().lower() != "images",
            id_prefix="A",
            source=source,
            ocr_phase=ocr_phase,
            existing_text_atoms=existing_text_atoms,
        )
        if (ocr_phase or "").strip().lower() == "text":
            return _drop_watermark_atoms(raw)
        return raw


def _text_only(atoms: list[dict]) -> list[dict]:
    return [
        a
        for a in atoms
        if a.get("atom_type") in _TEXT_TYPES and not _is_gap_placeholder_atom(a)
    ]


def _is_gap_placeholder_atom(atom: dict) -> bool:
    t = (atom.get("content") or atom.get("ocr_text") or atom.get("display_text") or "").strip()
    return t.startswith("[未拆分") or t.startswith("[整页未识别")


def _count_types(atoms: list[dict], types: frozenset[str]) -> int:
    return sum(1 for a in atoms if a.get("atom_type") in types)


def _default_cache_entry(*, old_page: int, new_page: int) -> dict:
    return {
        "old_page": old_page,
        "new_page": new_page,
        "old_raw": [],
        "new_raw": [],
        "old_text_ocr_done": False,
        "new_text_ocr_done": False,
        "old_image_ocr_done": False,
        "new_image_ocr_done": False,
        "text_compare": None,
        "image_compare": None,
        "text_compare_version": None,
        "image_compare_version": None,
    }


def _clear_text_compare(entry: dict) -> None:
    entry["text_compare"] = None
    entry["text_compare_version"] = None


def _clear_image_compare(entry: dict) -> None:
    entry["image_compare"] = None
    entry["image_compare_version"] = None


def _sanitize_text_compare_pinyin(compare: dict | None) -> dict | None:
    """比对缓存/结果出口再纠一次感叹啊声调，避免旧缓存仍显示 á/a。"""
    from ..llm.page_text_extract import repair_common_pinyin_tone_ocr

    if not isinstance(compare, dict):
        return compare
    out = dict(compare)
    for key in ("old_text", "new_text"):
        if key in out and isinstance(out[key], str):
            out[key] = repair_common_pinyin_tone_ocr(out[key])
    rows = out.get("block_rows")
    if isinstance(rows, list):
        fixed_rows: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                fixed_rows.append(row)
                continue
            r = dict(row)
            for key in ("old_text", "new_text", "change_summary"):
                if isinstance(r.get(key), str):
                    r[key] = repair_common_pinyin_tone_ocr(r[key])
            lines = r.get("change_summary_lines")
            if isinstance(lines, list):
                r["change_summary_lines"] = [
                    repair_common_pinyin_tone_ocr(x) if isinstance(x, str) else x
                    for x in lines
                ]
            ops = r.get("ops")
            if isinstance(ops, list):
                new_ops = []
                for op in ops:
                    if not isinstance(op, dict):
                        new_ops.append(op)
                        continue
                    o = dict(op)
                    for key in ("old", "new"):
                        if isinstance(o.get(key), str):
                            o[key] = repair_common_pinyin_tone_ocr(o[key])
                    new_ops.append(o)
                r["ops"] = new_ops
            fixed_rows.append(r)
        out["block_rows"] = fixed_rows
    for key in ("ops", "text_ops", "punct_ops"):
        ops = out.get(key)
        if not isinstance(ops, list):
            continue
        new_ops = []
        for op in ops:
            if not isinstance(op, dict):
                new_ops.append(op)
                continue
            o = dict(op)
            for k in ("old", "new"):
                if isinstance(o.get(k), str):
                    o[k] = repair_common_pinyin_tone_ocr(o[k])
            new_ops.append(o)
        out[key] = new_ops
    anchors = out.get("anchors")
    if isinstance(anchors, list):
        fixed_anc = []
        for anc in anchors:
            if not isinstance(anc, dict):
                fixed_anc.append(anc)
                continue
            a = dict(anc)
            for key in ("old_text", "new_text"):
                if isinstance(a.get(key), str):
                    a[key] = repair_common_pinyin_tone_ocr(a[key])
            fixed_anc.append(a)
        out["anchors"] = fixed_anc
    return out


def _valid_text_compare(entry: dict | None) -> dict | None:
    if not entry:
        return None
    if entry.get("text_compare_version") != _TEXT_COMPARE_CACHE_VERSION:
        return None
    cmp = entry.get("text_compare")
    if isinstance(cmp, dict) and cmp.get("verdict") is not None:
        return _sanitize_text_compare_pinyin(cmp)
    return None


def _valid_image_compare(entry: dict | None) -> dict | None:
    if not entry:
        return None
    if entry.get("image_compare_version") != _IMAGE_COMPARE_CACHE_VERSION:
        return None
    cmp = entry.get("image_compare")
    return cmp if isinstance(cmp, dict) and cmp.get("verdict") is not None else None


def _save_text_compare(cache_file: Path | None, entry: dict, compare: dict) -> None:
    entry["text_compare"] = compare
    entry["text_compare_version"] = _TEXT_COMPARE_CACHE_VERSION
    _write_cache_entry(cache_file, entry)


def _save_image_compare(cache_file: Path | None, entry: dict, compare: dict) -> None:
    entry["image_compare"] = compare
    entry["image_compare_version"] = _IMAGE_COMPARE_CACHE_VERSION
    _write_cache_entry(cache_file, entry)


def _load_pair_entry(
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    *,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
    auto_recompare: bool = True,
) -> tuple[Path | None, dict]:
    """读磁盘缓存并用 MySQL 补全（原子快照 + 比对结果）。

    比对缓存版本升级后：若两侧文字 OCR 仍在，自动重算 ②（不重跑 OCR），避免整单元结果「消失」。
    """
    from .page_compare_db import hydrate_pair_entry_from_db
    from .test_persist import should_hydrate_compare_from_db

    cache_file, entry = _read_cache_entry(
        old_vol,
        new_vol,
        old_page,
        new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    if entry is None:
        entry = _default_cache_entry(old_page=old_page, new_page=new_page)
    if should_hydrate_compare_from_db(old_vol, new_vol):
        entry = hydrate_pair_entry_from_db(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            entry=entry,
        )
    if (
        auto_recompare
        and entry.get("old_text_ocr_done")
        and entry.get("new_text_ocr_done")
        and _valid_text_compare(entry) is None
        and (entry.get("old_raw") or entry.get("new_raw"))
    ):
        try:
            entry = _recompare_text_inplace(
                cache_file,
                entry,
                old_vol=old_vol,
                new_vol=new_vol,
                old_page=old_page,
                new_page=new_page,
                new_pdf_source=new_pdf_source,
                preview_blob_id=preview_blob_id,
            )
        except Exception as exc:
            _log.warning(
                "auto recompare o%d/n%d failed: %s",
                old_page,
                new_page,
                exc,
            )
    return cache_file, entry


def _recompare_text_inplace(
    cache_file: Path | None,
    entry: dict,
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> dict:
    """用已有 OCR 原子按当前逻辑重跑文字比对并落盘/入库。"""
    old_raw = entry.get("old_raw") or []
    new_raw = entry.get("new_raw") or []
    old_hints, new_hints = _neighbor_body_pinyin(
        old_vol,
        new_vol,
        old_page,
        new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    prev_old_raw: list[dict] = []
    prev_new_raw: list[dict] = []
    next_old_raw: list[dict] = []
    next_new_raw: list[dict] = []
    # 邻页只取原子，禁止连锁 auto_recompare
    if old_page > 1 and new_page > 1:
        _, prev_entry = _load_pair_entry(
            old_vol,
            new_vol,
            old_page - 1,
            new_page - 1,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            auto_recompare=False,
        )
        if prev_entry:
            prev_old_raw = list(prev_entry.get("old_raw") or [])
            prev_new_raw = list(prev_entry.get("new_raw") or [])
    _, next_entry = _load_pair_entry(
        old_vol,
        new_vol,
        old_page + 1,
        new_page + 1,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        auto_recompare=False,
    )
    if next_entry:
        next_old_raw = list(next_entry.get("old_raw") or [])
        next_new_raw = list(next_entry.get("new_raw") or [])
    compare = compare_page_text(
        old_raw,
        new_raw,
        old_body_pinyin=old_hints,
        new_body_pinyin=new_hints,
        prev_old_atoms=prev_old_raw,
        prev_new_atoms=prev_new_raw,
        next_old_atoms=next_old_raw,
        next_new_atoms=next_new_raw,
    )
    _save_text_compare(cache_file, entry, compare)
    _persist_text_compare_to_db(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        compare=compare,
    )
    return entry


def _persist_side_atoms_to_db(
    *,
    volume: Volume,
    page_1: int,
    pdf_source: str,
    preview_blob_id: str | None,
    atoms: list[dict],
    text_ocr_done: bool,
    image_ocr_done: bool,
) -> None:
    from .test_persist import should_persist_compare_to_db

    if not should_persist_compare_to_db(volume):
        return
    from .page_compare_db import upsert_page_atom_snapshot

    upsert_page_atom_snapshot(
        volume=volume,
        page_1=page_1,
        pdf_source=pdf_source,
        preview_blob_id=preview_blob_id,
        atoms=atoms,
        text_ocr_done=text_ocr_done,
        image_ocr_done=image_ocr_done,
    )


def _persist_text_compare_to_db(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: PdfSource,
    preview_blob_id: str | None,
    compare: dict,
) -> None:
    from .test_persist import should_persist_compare_to_db

    if not should_persist_compare_to_db(old_vol, new_vol):
        return
    from .page_compare_db import upsert_page_compare

    upsert_page_compare(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        text_compare=compare,
        text_compare_version=_TEXT_COMPARE_CACHE_VERSION,
    )


def _persist_image_compare_to_db(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: PdfSource,
    preview_blob_id: str | None,
    compare: dict,
) -> None:
    from .test_persist import should_persist_compare_to_db

    if not should_persist_compare_to_db(old_vol, new_vol):
        return
    from .page_compare_db import upsert_page_compare

    upsert_page_compare(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        image_compare=compare,
        image_compare_version=_IMAGE_COMPARE_CACHE_VERSION,
    )


def _clear_compares_in_db(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: PdfSource,
    preview_blob_id: str | None,
    clear_text: bool = False,
    clear_image: bool = False,
) -> None:
    from .test_persist import should_persist_compare_to_db

    if not should_persist_compare_to_db(old_vol, new_vol):
        return
    from .page_compare_db import upsert_page_compare

    if not clear_text and not clear_image:
        return
    upsert_page_compare(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        clear_text=clear_text,
        clear_image=clear_image,
    )


def _empty_atoms_payload(*, old_page: int, new_page: int) -> dict:
    return _build_atoms_payload(_default_cache_entry(old_page=old_page, new_page=new_page))


def _appendix_flags_for_pair(
    old_vol: Volume | None,
    new_vol: Volume | None,
    *,
    old_page: int,
    new_page: int,
) -> tuple[bool, bool]:
    from .yuwen_appendix import volume_page_is_appendix_table

    appendix_old = bool(old_vol and volume_page_is_appendix_table(old_vol, old_page))
    appendix_new = bool(new_vol and volume_page_is_appendix_table(new_vol, new_page))
    return appendix_old, appendix_new


def _build_pair_atoms_payload(
    entry: dict,
    *,
    old_vol: Volume | None,
    new_vol: Volume | None,
    cache_file: Path | None = None,
) -> dict:
    del cache_file  # 保留形参以兼容调用方
    old_page = int(entry.get("old_page") or 0)
    new_page = int(entry.get("new_page") or 0)
    appendix_old, appendix_new = _appendix_flags_for_pair(
        old_vol, new_vol, old_page=old_page, new_page=new_page
    )
    return _build_atoms_payload(
        entry, appendix_old=appendix_old, appendix_new=appendix_new
    )


def _atoms_for_api(raw: list[dict]) -> list[dict]:
    """序列化给前端：去掉填缝占位与噪声插图框。"""
    out: list[dict] = []
    for a in raw:
        if _is_gap_placeholder_atom(a):
            continue
        if is_spurious_image_atom(a):
            continue
        out.append(a)
    return out


def _build_atoms_payload(
    entry: dict,
    *,
    appendix_old: bool = False,
    appendix_new: bool = False,
) -> dict:
    # 展示前再跑一遍：旧缓存框未盖注音 / 残留孤立拼音碎框时也能立刻修正
    old_raw = _drop_watermark_atoms(
        _atoms_for_api(entry.get("old_raw") or []),
        skip_form_pair_merge=appendix_old,
    )
    new_raw = _drop_watermark_atoms(
        _atoms_for_api(entry.get("new_raw") or []),
        skip_form_pair_merge=appendix_new,
    )
    old_text_done = bool(entry.get("old_text_ocr_done"))
    new_text_done = bool(entry.get("new_text_ocr_done"))
    old_image_done = bool(entry.get("old_image_ocr_done"))
    new_image_done = bool(entry.get("new_image_ocr_done"))
    return {
        "old_page": entry.get("old_page"),
        "new_page": entry.get("new_page"),
        "old_atoms": [_serialize_atom(a, side="old") for a in old_raw],
        "new_atoms": [_serialize_atom(a, side="new") for a in new_raw],
        "old_text_ocr_done": old_text_done,
        "new_text_ocr_done": new_text_done,
        "old_image_ocr_done": old_image_done,
        "new_image_ocr_done": new_image_done,
        "text_ocr_done": old_text_done and new_text_done,
        "image_ocr_done": old_image_done and new_image_done,
        "summary": {
            "old_atom_count": len(old_raw),
            "new_atom_count": len(new_raw),
            "old_text_atoms": _count_types(old_raw, _TEXT_TYPES),
            "new_text_atoms": _count_types(new_raw, _TEXT_TYPES),
            "old_image_atoms": _count_types(old_raw, _IMAGE_TYPES),
            "new_image_atoms": _count_types(new_raw, _IMAGE_TYPES),
        },
        "text_compare": _valid_text_compare(entry),
        "image_compare": _valid_image_compare(entry),
        "text_compare_cached": _valid_text_compare(entry) is not None,
        "image_compare_cached": _valid_image_compare(entry) is not None,
    }


def _read_cache_entry(
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    *,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> tuple[Path | None, dict | None]:
    cache_file = _cache_path(
        old_vol,
        new_vol,
        old_page,
        new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    if not cache_file or not cache_file.is_file():
        return cache_file, None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cache_file, None
    if data.get("cache_version") != _CACHE_VERSION:
        # 旧版磁盘缓存仍可用于展示 OCR 原子；比对版本在 _load_pair_entry 再校验/重算
        data["_cache_version_stale"] = True
    return cache_file, data


def _write_cache_entry(cache_file: Path | None, entry: dict) -> None:
    if not cache_file:
        return
    entry["cache_version"] = _CACHE_VERSION
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        _log.warning("原子对比缓存写入失败：%s", exc)


def _fuzz_ratio(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    try:
        from rapidfuzz import fuzz

        return fuzz.ratio(na, nb) / 100.0
    except ImportError:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, na, nb).ratio()


_QUOTE_CHARS = frozenset("\"'「」『』“”‘’")


def _strip_quotes(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch not in _QUOTE_CHARS)


def _texts_equal_ignoring_quotes(a: str, b: str) -> bool:
    """汉字与非引号标点均相同，仅引号有无/位置不同。"""
    if _strip_for_content(a) != _strip_for_content(b):
        return False
    return _soft_normalize_punct(_strip_quotes(a)) == _soft_normalize_punct(_strip_quotes(b))


def _quotes_soft_equal(a: str, b: str) -> bool:
    """含引号在内的软归一文本是否相同。"""
    return _soft_normalize_punct(a) == _soft_normalize_punct(b)


_CN_QUOTE_INNER_RE = re.compile(r'[“"]([^”"]*)[”"]')


def _quoted_inners(text: str) -> list[str]:
    return [m.group(1).strip() for m in _CN_QUOTE_INNER_RE.finditer(text or "")]


def _quote_move_summary_lines(old_text: str, new_text: str) -> list[str] | None:
    """引号挪位/增减且汉字未改 → 教研可读说明。"""
    if not _texts_equal_ignoring_quotes(old_text, new_text):
        return None
    if _quotes_soft_equal(old_text, new_text):
        return None
    old_inners = _quoted_inners(old_text)
    new_inners = _quoted_inners(new_text)
    lines = ["引号位置改动"]
    if old_inners or new_inners:
        old_s = "／".join(old_inners) if old_inners else "（无）"
        new_s = "／".join(new_inners) if new_inners else "（无）"
        lines.append(f"旧引号内：「{old_s}」→ 新引号内：「{new_s}」")
    else:
        lines.append("引号挪位或增减，汉字未改")
    return lines


def _soft_normalize_punct(s: str) -> str:
    """标点 OCR 软归一：全半角括号/逗号等视为等价，去空白。"""
    t = (s or "").translate(_SOFT_PUNCT_MAP)
    # 列表方块符形态差（◇/◊/◆/□…）视为同一项目符号
    for ch in _LIST_BULLET_CHARS:
        if ch != "◇":
            t = t.replace(ch, "◇")
    return re.sub(r"[\s\u200b\u200c\u200d\ufeff\u00a0]+", "", t)


def _punct_soft_equivalent(a: str, b: str) -> bool:
    """两侧去掉汉字后，软归一标点是否相同（含一侧为空的空白噪声）。"""
    sa, sb = _soft_normalize_punct(a), _soft_normalize_punct(b)
    if sa == sb:
        return True
    # 列表方块符一侧漏识：◇/◊ vs 空 → 等价
    if (_is_list_bullet_chunk(a) and not sb) or (_is_list_bullet_chunk(b) and not sa):
        return True
    # 内容剥离后相同且软归一也相同 → 仅形近标点
    if _strip_for_content(a) == _strip_for_content(b) and sa == sb:
        return True
    return False


def _layout_role(atom: dict) -> str:
    """
    阅读角色：body / figure_caption / figure_label。
    用于阻止饼图旁注、图题与正文互锚。
    """
    if (atom.get("atom_type") or "") == "caption":
        return "figure_caption"
    text = _atom_plain(atom)
    if not text:
        return "body"
    if re.match(r"^(图|表)\s*\d", text):
        return "figure_caption"
    if re.match(r"^图[一二三四五六七八九十\d]", text):
        return "figure_caption"
    x0 = float(atom.get("x_start") or 0)
    # 饼图/示意图旁短标注：含 % 的短串，或偏右且极短非句
    if len(text) <= 14 and "%" in text and not re.search(r"[。；！？]", text):
        return "figure_label"
    if (
        len(text) <= 10
        and x0 >= 0.45
        and not re.search(r"[。；！？]", text)
        and not re.search(r"通过|实验|测定|我们|可以|因为|所以", text)
    ):
        return "figure_label"
    return "body"


def _is_margin_side_note(atom: dict) -> bool:
    """左/右窄栏旁批（非主栏宽正文）。"""
    if (atom.get("block_role") or "") == "side_note":
        return True
    x0 = float(atom.get("x_start") or 0)
    x1 = float(atom.get("x_end") or 1)
    w = max(x1 - x0, 1e-6)
    text = _atom_plain(atom)
    # 左栏窄旁批 / 右栏旁批
    left = x1 <= 0.34 and w <= 0.30
    right = x0 >= 0.48 and w <= 0.52
    if not (left or right):
        return False
    # 排除主栏误检：过长叙述段
    if len(re.sub(r"\s+", "", text)) > 120:
        return False
    return True


def _margin_note_shared_signal(a: str, b: str) -> float:
    """旁批改写时的共现信号（引号片段 / 问句骨架），0~1。"""
    import re as _re

    def _quotes(s: str) -> set[str]:
        return {m.group(1) for m in _re.finditer(r"[“\"「『]([^”\"」』]{2,20})[”\"」』]", s or "")}

    def _hans(s: str) -> set[str]:
        return set(_re.findall(r"[\u4e00-\u9fff]", s or ""))

    qa, qb = _quotes(a), _quotes(b)
    if qa and qb and (qa & qb):
        return 0.35
    ha, hb = _hans(a), _hans(b)
    if not ha or not hb:
        return 0.0
    inter = len(ha & hb)
    union = len(ha | hb)
    jacc = inter / max(union, 1)
    # 双方都是问句且汉字重合较高
    if ("？" in (a or "") or "?" in (a or "")) and ("？" in (b or "") or "?" in (b or "")):
        if jacc >= 0.28 and inter >= 6:
            return 0.28
    if jacc >= 0.4 and inter >= 8:
        return 0.22
    return 0.0


def _char_overlap_ratio(a: str, b: str) -> float:
    """汉字/字母集合重合度，改写句仍可高于纯编辑距离。"""
    ha = {ch for ch in _norm(a or "") if "\u4e00" <= ch <= "\u9fff" or ch.isalnum()}
    hb = {ch for ch in _norm(b or "") if "\u4e00" <= ch <= "\u9fff" or ch.isalnum()}
    if not ha or not hb:
        return 0.0
    return len(ha & hb) / max(len(ha | hb), 1)


def _anchor_score(old_a: dict, new_a: dict) -> float:
    oy, ny = _ymid(old_a), _ymid(new_a)
    # 软窗口略放宽：同文气泡换位时仍可拿到非零位置分，但不压过强文本相似
    pos = max(0.0, 1.0 - abs(oy - ny) / 0.28)
    ot, nt = _atom_plain(old_a), _atom_plain(new_a)
    text_sim = _fuzz_ratio(ot, nt)
    overlap = _char_overlap_ratio(ot, nt)
    # 编辑距离 + 字集重合：改写段（爸爸气泡）常 fuzz~0.5 但 overlap 更高
    content = max(text_sim, 0.55 * text_sim + 0.45 * overlap)
    # 文意近 → 以内容为主（版面挪位/对调）；文意弱才靠位置
    if content >= 0.78:
        w_pos, w_text = 0.08, 0.92
    elif content >= 0.55:
        w_pos, w_text = 0.15, 0.85
    elif content >= 0.40:
        w_pos, w_text = 0.28, 0.72
    else:
        w_pos, w_text = 0.45, 0.55
    score = w_pos * pos + w_text * content
    # 中等以上内容相似的长句：给「无视位置」的下限，避免换位后掉到阈值下被同 Y 错句抢走
    lo, ln = len(ot), len(nt)
    if content >= 0.48 and min(lo, ln) >= 12:
        score = max(score, 0.32 + 0.58 * content)

    # 左/右栏旁批优先互配，避免与主栏正文错锚
    o_note = _is_margin_side_note(old_a)
    n_note = _is_margin_side_note(new_a)
    if o_note and n_note:
        score += 0.14
        score += _margin_note_shared_signal(ot, nt)
    elif o_note != n_note and abs(float(old_a.get("x_start") or 0) - float(new_a.get("x_start") or 0)) > 0.25:
        score -= 0.16

    o_role, n_role = _layout_role(old_a), _layout_role(new_a)
    if o_role != n_role:
        # 正文 ↔ 图注/旁注：禁止互配
        if (o_role in _FIGURE_ROLES) != (n_role in _FIGURE_ROLES):
            score -= 0.5
        elif o_role in _FIGURE_ROLES and n_role in _FIGURE_ROLES and o_role != n_role:
            # 饼图旁注 ↔ 图题也禁配
            score -= 0.45
    else:
        if o_role in _FIGURE_ROLES:
            score += 0.08

    if lo > 0 and ln > 0:
        ratio = min(lo, ln) / max(lo, ln)
        if ratio < 0.35 and (o_role == "body" or n_role == "body"):
            # 两侧都是旁批时放宽长度比惩罚（旧旁批常更长/更短）
            # 短边是长边前/后缀：多为跨页截断，不应因长度比掉到阈值下
            short, long = (ot, nt) if lo <= ln else (nt, ot)
            sn, lnrm = _norm(short), _norm(long)
            prefix_cut = bool(sn) and (lnrm.startswith(sn) or lnrm.endswith(sn))
            if not (o_note and n_note) and not prefix_cut:
                score -= 0.28
    return score


def _pair_atoms_by_score(
    old_text: list[dict],
    new_text: list[dict],
    *,
    min_score: float = 0.38,
) -> list[dict]:
    """全局高分优先 1:1 锚定（非按旧侧阅读序贪心），减少版面换位错配。"""
    edges: list[tuple[float, int, int]] = []
    for i, o in enumerate(old_text):
        for j, n in enumerate(new_text):
            score = _anchor_score(o, n)
            if score >= min_score:
                edges.append((score, i, j))
    # 同分时偏向阅读序接近，稳定输出
    edges.sort(key=lambda t: (-t[0], abs(t[1] - t[2]), t[1], t[2]))
    used_old: set[int] = set()
    used_new: set[int] = set()
    anchors: list[dict] = []
    for score, i, j in edges:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        anchors.append(_make_anchor(old_text[i], new_text[j], score=score))
    anchors.sort(
        key=lambda a: (
            float((a.get("old_bbox") or {}).get("y_start") or 0),
            float((a.get("old_bbox") or {}).get("x_start") or 0),
        )
    )
    return anchors


def _repair_misaligned_by_text(
    old_text: list[dict],
    new_text: list[dict],
    anchors: list[dict],
) -> list[dict]:
    """对低文本相似的错配做交换修复：若互换后双方文意都更近，则交换新侧。"""
    if len(anchors) < 2:
        return anchors
    old_by_id = {a.get("atom_id"): a for a in old_text if a.get("atom_id")}
    new_by_id = {a.get("atom_id"): a for a in new_text if a.get("atom_id")}
    items = list(anchors)
    # 多轮小幅 2-opt，优先修「改写」错配
    for _ in range(min(6, len(items))):
        improved = False
        for i in range(len(items)):
            ai = items[i]
            if float(ai.get("text_similarity") or 0) >= 0.62:
                continue
            oi = old_by_id.get(ai.get("old_atom_id"))
            ni = new_by_id.get(ai.get("new_atom_id"))
            if not oi or not ni:
                continue
            best_j = -1
            best_gain = 0.08
            best_pair: tuple[dict, dict] | None = None
            for j in range(len(items)):
                if i == j:
                    continue
                aj = items[j]
                oj = old_by_id.get(aj.get("old_atom_id"))
                nj = new_by_id.get(aj.get("new_atom_id"))
                if not oj or not nj:
                    continue
                # 只在双方当前都偏弱、或互换后双方都明显更好时动手
                cur_i = max(
                    _fuzz_ratio(_atom_plain(oi), _atom_plain(ni)),
                    _char_overlap_ratio(_atom_plain(oi), _atom_plain(ni)),
                )
                cur_j = max(
                    _fuzz_ratio(_atom_plain(oj), _atom_plain(nj)),
                    _char_overlap_ratio(_atom_plain(oj), _atom_plain(nj)),
                )
                sw_i = max(
                    _fuzz_ratio(_atom_plain(oi), _atom_plain(nj)),
                    _char_overlap_ratio(_atom_plain(oi), _atom_plain(nj)),
                )
                sw_j = max(
                    _fuzz_ratio(_atom_plain(oj), _atom_plain(ni)),
                    _char_overlap_ratio(_atom_plain(oj), _atom_plain(ni)),
                )
                if sw_i < 0.50 and sw_j < 0.50:
                    continue
                gain = (sw_i + sw_j) - (cur_i + cur_j)
                if gain > best_gain and sw_i >= cur_i - 0.02 and sw_j >= cur_j - 0.02:
                    best_gain = gain
                    best_j = j
                    best_pair = (
                        _make_anchor(oi, nj, score=_anchor_score(oi, nj)),
                        _make_anchor(oj, ni, score=_anchor_score(oj, ni)),
                    )
            if best_j >= 0 and best_pair is not None:
                items[i], items[best_j] = best_pair
                improved = True
        if not improved:
            break
    items.sort(
        key=lambda a: (
            float((a.get("old_bbox") or {}).get("y_start") or 0),
            float((a.get("old_bbox") or {}).get("x_start") or 0),
        )
    )
    return items


def _percent_values_differ(old_text: str, new_text: str) -> bool:
    if "%" not in old_text and "%" not in new_text:
        return False
    ot = re.findall(r"(\d+\.?\d*)%", old_text)
    nt = re.findall(r"(\d+\.?\d*)%", new_text)
    if not ot or not nt:
        return False
    return ot != nt


def _classify_anchor_change(old_a: dict, new_a: dict) -> tuple[str, float]:
    ot, nt = _atom_plain(old_a), _atom_plain(new_a)
    sim = _fuzz_ratio(ot, nt)
    if _percent_values_differ(ot, nt):
        return "数据变化", sim
    if _ocr_equivalent(ot, nt):
        return "一致", sim
    if sim >= 0.72:
        return "轻微差异", sim
    return "改写", sim


_LIST_BULLET_CHARS = "◇◆◊♦●•○□■▪▫※"
_LIST_BULLET_RE = re.compile(rf"[{re.escape(_LIST_BULLET_CHARS)}]")
_LIST_LEAD_RE = re.compile(rf"^([{re.escape(_LIST_BULLET_CHARS)}])\s*")


def _split_list_item_texts(text: str) -> list[str]:
    """同一 OCR 框内多条 ◇/◆/◊ 等列表项 → 拆成多段（比对前虚拟原子）。"""
    t = (text or "").strip()
    if not t:
        return []
    if len(_LIST_BULLET_RE.findall(t)) < 2:
        return [t]
    parts = [p.strip() for p in re.split(rf"(?=[{re.escape(_LIST_BULLET_CHARS)}])", t) if p.strip()]
    return parts if len(parts) >= 2 else [t]


def _is_list_bullet_chunk(s: str) -> bool:
    t = re.sub(r"\s+", "", s or "")
    return bool(t) and all(ch in _LIST_BULLET_CHARS for ch in t)


def _unify_list_bullet_pair(ot: str, nt: str) -> tuple[str, str]:
    """配对块：一侧识别到列表方块符、另一侧漏识时，把符号补到缺失侧。

    新旧扫描页常都有 ◇/◊，但 OCR 可能只写出一侧；补齐后避免误报「标点删除」，
    且展示与页图一致。正文若另有改写（如 株→棵），仍保留真实文字差。
    """
    ot_s, nt_s = (ot or "").strip(), (nt or "").strip()
    if not ot_s and not nt_s:
        return ot, nt
    mo, mn = _LIST_LEAD_RE.match(ot_s), _LIST_LEAD_RE.match(nt_s)
    o_rest = ot_s[mo.end() :] if mo else ot_s
    n_rest = nt_s[mn.end() :] if mn else nt_s
    if mo and not mn:
        bullet = mo.group(1)
        filled = f"{bullet} {n_rest.lstrip()}" if n_rest.strip() else bullet
        return ot_s, filled
    if mn and not mo:
        bullet = mn.group(1)
        filled = f"{bullet} {o_rest.lstrip()}" if o_rest.strip() else bullet
        return filled, nt_s
    return ot, nt


def _explode_merged_list_atoms(atoms: list[dict]) -> list[dict]:
    """
    OCR 偶发把多条列表题干挂进一个原子；比对前按项目符号拆开，
    避免 1:1 锚定只配上其中一条、另一条被漏配或误报删除。
    """
    out: list[dict] = []
    for a in atoms:
        plain = _atom_plain(a)
        parts = _split_list_item_texts(plain)
        if len(parts) <= 1:
            out.append(a)
            continue
        aid = str(a.get("atom_id") or "atom")
        y0 = float(a.get("y_start") or 0)
        y1 = float(a.get("y_end") or (y0 + 0.05))
        if y1 <= y0:
            y1 = y0 + 0.05 * len(parts)
        n = len(parts)
        for i, part in enumerate(parts):
            yi0 = y0 + (y1 - y0) * i / n
            yi1 = y0 + (y1 - y0) * (i + 1) / n
            child = dict(a)
            child["atom_id"] = f"{aid}#p{i + 1}"
            child["ocr_text"] = part
            child["content"] = part
            child["y_start"] = yi0
            child["y_end"] = yi1
            child["split_from"] = aid
            out.append(child)
    return out


_PREREAD_PROMPT_HINTS = (
    "默读课文",
    "朗读课文",
    "提出自己的问题",
    "把问题分类",
    "值得思考的问题",
    "尝试解决",
    "边读边想",
    "预习提示",
    "学习提示",
    "读前思考",
)


def _looks_like_preread_prompt(text: str) -> bool:
    """课题下短学习提示句（默读/选出问题…），非叙述正文。"""
    t = re.sub(r"\s+", "", text or "")
    if not t or len(t) > 60:
        return False
    # 叙述正文常见专名/过长情节 → 不是导读
    if any(x in t for x in ("孙膑", "田忌", "齐威王", "门客", "将军")):
        return False
    if any(h in t for h in _PREREAD_PROMPT_HINTS):
        return True
    if t.startswith(("选出你", "选出", "试着", "再试着", "并尝试", "说说", "想一想", "读一读")):
        return True
    return False


def _looks_like_form_pair_fragment(text: str) -> bool:
    """对照表行碎片：至少两条「字—字」条目（用于合框）。"""
    return len(_FORM_PAIR_ENTRY_RE.findall(text or "")) >= 2 or (
        len(re.findall(r"[\u4e00-\u9fff]\s*[—\-－]\s*[\u4e00-\u9fff]", text or "")) >= 2
    )


def _merge_adjacent_form_pair_list_atoms(atoms: list[dict]) -> list[dict]:
    """比对/展示前：把按行拆开的识字加油站对照表合成一块。"""
    if len(atoms or []) < 2:
        return list(atoms or [])
    ordered = sorted(
        atoms,
        key=lambda a: (_ymid(a), float(a.get("x_start") or 0)),
    )
    out: list[dict] = []
    i = 0
    while i < len(ordered):
        cur = ordered[i]
        if not _looks_like_form_pair_fragment(_atom_plain(cur)):
            out.append(cur)
            i += 1
            continue
        group = [cur]
        j = i + 1
        while j < len(ordered):
            nxt = ordered[j]
            if not _looks_like_form_pair_fragment(_atom_plain(nxt)):
                break
            prev = group[-1]
            gap = float(nxt.get("y_start") or 0) - float(prev.get("y_end") or 0)
            if gap > 0.04:
                break
            if abs(float(nxt.get("x_start") or 0) - float(group[0].get("x_start") or 0)) > 0.2:
                break
            if float(nxt.get("x_end") or 1) - float(nxt.get("x_start") or 0) < 0.25:
                break
            group.append(nxt)
            j += 1
        if len(group) == 1:
            out.append(cur)
            i += 1
            continue
        merged = dict(group[0])
        joined = "\n".join(_atom_plain(g) for g in group if _atom_plain(g))
        merged["ocr_text"] = joined
        merged["content"] = joined
        merged["x_start"] = min(float(g.get("x_start") or 0) for g in group)
        merged["x_end"] = max(float(g.get("x_end") or 1) for g in group)
        merged["y_start"] = min(float(g.get("y_start") or 0) for g in group)
        merged["y_end"] = max(float(g.get("y_end") or 0) for g in group)
        base_id = str(group[0].get("atom_id") or "formpair")
        merged["atom_id"] = f"{base_id}#formpair"
        merged["merged_form_pair_from"] = [g.get("atom_id") for g in group]
        out.append(merged)
        i = j
    return out


def _merge_adjacent_preread_prompt_atoms(atoms: list[dict]) -> list[dict]:
    """比对前：把课题下连续拆开的学习提示句合成一块，对齐「新侧合框、旧侧拆框」。"""
    if len(atoms or []) < 2:
        return list(atoms or [])
    ordered = sorted(
        atoms,
        key=lambda a: (_ymid(a), float(a.get("x_start") or 0)),
    )
    out: list[dict] = []
    i = 0
    while i < len(ordered):
        cur = ordered[i]
        if not _looks_like_preread_prompt(_atom_plain(cur)):
            out.append(cur)
            i += 1
            continue
        group = [cur]
        j = i + 1
        while j < len(ordered):
            nxt = ordered[j]
            if not _looks_like_preread_prompt(_atom_plain(nxt)):
                break
            prev = group[-1]
            gap = float(nxt.get("y_start") or 0) - float(prev.get("y_end") or 0)
            if gap > 0.055:
                break
            if abs(float(nxt.get("x_start") or 0) - float(group[0].get("x_start") or 0)) > 0.18:
                break
            group.append(nxt)
            j += 1
        if len(group) == 1:
            out.append(cur)
            i += 1
            continue
        merged = dict(group[0])
        joined = "\n".join(_atom_plain(g) for g in group if _atom_plain(g))
        merged["ocr_text"] = joined
        merged["content"] = joined
        merged["x_start"] = min(float(g.get("x_start") or 0) for g in group)
        merged["x_end"] = max(float(g.get("x_end") or 1) for g in group)
        merged["y_start"] = min(float(g.get("y_start") or 0) for g in group)
        merged["y_end"] = max(float(g.get("y_end") or 0) for g in group)
        base_id = str(group[0].get("atom_id") or "preread")
        merged["atom_id"] = f"{base_id}#preread"
        merged["merged_preread_from"] = [g.get("atom_id") for g in group]
        out.append(merged)
        i = j
    return out


def _partial_fuzz_ratio(long: str, short: str) -> float:
    nl, ns = _norm(long), _norm(short)
    if not nl or not ns:
        return 0.0
    try:
        from rapidfuzz import fuzz

        return fuzz.partial_ratio(nl, ns) / 100.0
    except ImportError:
        from difflib import SequenceMatcher

        if ns in nl:
            return 1.0
        best = 0.0
        step = max(1, len(ns) // 4)
        for i in range(0, max(1, len(nl) - len(ns) + 1), step):
            best = max(best, SequenceMatcher(None, nl[i : i + len(ns)], ns).ratio())
        return best


def _segment_covered_by(haystack: str, needle: str, *, edge: bool = False) -> bool:
    """短段是否实质包含在长段中（合并框 vs 拆分框）。

    edge=True：页首/页尾残段可更短（跨页续行常只有几个字）。
    """
    h, n = _norm(haystack), _norm(needle)
    min_len = 4 if edge else 8
    if not h or not n or len(n) < min_len:
        return False
    if len(n) / max(len(h), 1) > 0.92:
        return False
    if n in h:
        return True
    if edge and (h.endswith(n) or h.startswith(n)):
        return True
    return _partial_fuzz_ratio(haystack, needle) >= (0.94 if edge else 0.92)


def _is_page_edge_fragment(atom: dict) -> bool:
    """页顶/页底的短残段：多半是跨页段尾/段首，不是独立改写。"""
    y0 = float(atom.get("y_start") or 0)
    y1 = float(atom.get("y_end") or 1)
    plain = _strip_for_content(_atom_plain(atom))
    if len(plain) < 2 or len(plain) > 28:
        return False
    return y0 < 0.16 or y1 > 0.84


def _prev_page_edge_hosts(atoms: list[dict], *, bottom: bool) -> list[dict]:
    """邻页用于承接残段的候选：页底（接本页页顶）或页顶（接本页页底）。"""
    out: list[dict] = []
    for a in _filter_text_atoms(atoms):
        y0 = float(a.get("y_start") or 0)
        y1 = float(a.get("y_end") or 0)
        if bottom and y1 >= 0.70:
            out.append(a)
        elif (not bottom) and y0 <= 0.30:
            out.append(a)
    out.sort(key=lambda a: -len(_strip_for_content(_atom_plain(a))))
    return out


def _absorb_pagination_edge_fragments(
    old_text: list[dict],
    new_text: list[dict],
    anchors: list[dict],
    *,
    prev_old_atoms: list[dict] | None = None,
    prev_new_atoms: list[dict] | None = None,
) -> tuple[list[dict], list[dict], list[dict], set[str]]:
    """
    吸收跨页残段：本页页顶/页尾短框若被对侧本页长段或邻页段尾/段首包含，
    则视为「分页错位」而非内容增删。
    返回 (anchors, old_text, new_text, absorbed_atom_ids)。
    """
    matched_old = {a["old_atom_id"] for a in anchors}
    matched_new = {a["new_atom_id"] for a in anchors}
    id_to = {a.get("atom_id"): a for a in old_text + new_text if a.get("atom_id")}
    for mid in list(matched_old):
        parent = (id_to.get(mid) or {}).get("split_from")
        if parent:
            matched_old.add(parent)
    for mid in list(matched_new):
        parent = (id_to.get(mid) or {}).get("split_from")
        if parent:
            matched_new.add(parent)

    prev_old = list(prev_old_atoms or [])
    prev_new = list(prev_new_atoms or [])
    prev_old_bottom = _prev_page_edge_hosts(prev_old, bottom=True)
    prev_new_bottom = _prev_page_edge_hosts(prev_new, bottom=True)
    prev_old_top = _prev_page_edge_hosts(prev_old, bottom=False)
    prev_new_top = _prev_page_edge_hosts(prev_new, bottom=False)

    new_anchors = list(anchors)
    extra_old: list[dict] = []
    extra_new: list[dict] = []
    absorbed: set[str] = set()

    def _host_lists_for_frag(frag: dict, *, frag_side: str) -> list[tuple[str, dict]]:
        """仅邻页可作分页承接；同页长短框是 OCR 合/拆，交给 1:N 锚定，禁止标「续到下页」。"""
        y0 = float(frag.get("y_start") or 0)
        at_top = y0 < 0.16
        hosts: list[tuple[str, dict]] = []
        if frag_side == "old":
            neigh = prev_new_bottom if at_top else prev_new_top
        else:
            neigh = prev_old_bottom if at_top else prev_old_top
        for h in neigh:
            hosts.append(("prev", h))
        return hosts

    def _try_absorb(frag: dict, *, frag_side: str) -> bool:
        fid = frag.get("atom_id")
        if not fid or fid in absorbed:
            return False
        if frag_side == "old" and fid in matched_old:
            return False
        if frag_side == "new" and fid in matched_new:
            return False
        if not _is_page_edge_fragment(frag):
            return False
        ft = _atom_plain(frag)
        for source, host in _host_lists_for_frag(frag, frag_side=frag_side):
            ht = _atom_plain(host)
            if not _segment_covered_by(ht, ft, edge=True):
                continue
            seg = _slice_matching_segment(ht, ft)
            if frag_side == "old":
                syn = dict(host) if source == "same" else {
                    "atom_id": f"prev#{host.get('atom_id')}",
                    "atom_type": "text",
                    "x_start": frag.get("x_start"),
                    "y_start": frag.get("y_start"),
                    "x_end": frag.get("x_end"),
                    "y_end": frag.get("y_end"),
                }
                syn = dict(syn)
                syn["atom_id"] = f"{syn.get('atom_id')}#pg"
                syn["ocr_text"] = seg
                syn["content"] = seg
                syn["split_from"] = host.get("atom_id")
                syn["pagination"] = True
                extra_new.append(syn)
                anc = _make_anchor(frag, syn, score=0.72)
            else:
                syn = dict(host) if source == "same" else {
                    "atom_id": f"prev#{host.get('atom_id')}",
                    "atom_type": "text",
                    "x_start": frag.get("x_start"),
                    "y_start": frag.get("y_start"),
                    "x_end": frag.get("x_end"),
                    "y_end": frag.get("y_end"),
                }
                syn = dict(syn)
                syn["atom_id"] = f"{syn.get('atom_id')}#pg"
                syn["ocr_text"] = seg
                syn["content"] = seg
                syn["split_from"] = host.get("atom_id")
                syn["pagination"] = True
                extra_old.append(syn)
                anc = _make_anchor(syn, frag, score=0.72)
            anc["change"] = "分页错位"
            anc["pagination"] = True
            # 仅邻页承接：本页残段来自上页
            anc["pagination_note"] = {"received": ft}
            new_anchors.append(anc)
            absorbed.add(fid)
            if frag_side == "old":
                matched_old.add(fid)
            else:
                matched_new.add(fid)
            return True
        return False

    for frag in list(old_text):
        _try_absorb(frag, frag_side="old")
    for frag in list(new_text):
        _try_absorb(frag, frag_side="new")

    # 第二轮：整段已在对侧上页出现 → 分页错位（非真删），仍保留本页行
    prev_old_blob = "".join(_neighbor_plain_texts(prev_old))
    prev_new_blob = "".join(_neighbor_plain_texts(prev_new))

    def _try_absorb_prev_duplicate(frag: dict, *, frag_side: str) -> bool:
        fid = frag.get("atom_id")
        if not fid or fid in absorbed:
            return False
        if frag_side == "old" and fid in matched_old:
            return False
        if frag_side == "new" and fid in matched_new:
            return False
        ft = _atom_plain(frag)
        ft_c = _strip_for_content(ft)
        if len(ft_c) < 8:
            return False
        # 词表/练习不按「上页重复」吞掉
        if _looks_like_reading_list(ft) or _looks_like_writing_grid(ft):
            return False
        blob = prev_new_blob if frag_side == "old" else prev_old_blob
        if not blob:
            return False
        # 整段已在上页：允许 needle≈haystack（_segment_covered_by 会拒绝对等长）
        bn, fn = _norm(blob), _norm(ft)
        covered = bool(fn and (fn in bn or _fuzz_ratio(ft, blob) >= 0.92))
        if not covered and not _segment_covered_by(blob, ft, edge=False):
            return False
        syn = {
            "atom_id": f"prev#dup#{fid}#pg",
            "atom_type": "text",
            "x_start": frag.get("x_start"),
            "y_start": frag.get("y_start"),
            "x_end": frag.get("x_end"),
            "y_end": frag.get("y_end"),
            "ocr_text": ft,
            "content": ft,
            "pagination": True,
        }
        if frag_side == "old":
            extra_new.append(syn)
            anc = _make_anchor(frag, syn, score=0.7)
        else:
            extra_old.append(syn)
            anc = _make_anchor(syn, frag, score=0.7)
        anc["change"] = "分页错位"
        anc["pagination"] = True
        anc["pagination_note"] = {"received": ft}
        new_anchors.append(anc)
        absorbed.add(fid)
        if frag_side == "old":
            matched_old.add(fid)
        else:
            matched_new.add(fid)
        return True

    for frag in list(old_text):
        _try_absorb_prev_duplicate(frag, frag_side="old")
    for frag in list(new_text):
        _try_absorb_prev_duplicate(frag, frag_side="new")

    if extra_old:
        old_text = old_text + extra_old
    if extra_new:
        new_text = new_text + extra_new
    return new_anchors, old_text, new_text, absorbed


def _norm_char_raw_indices(raw: str) -> list[int]:
    """_norm(raw) 中每个字符对应的原文下标。"""
    return [i for i, ch in enumerate(raw or "") if _norm(ch)]


def _slice_matching_segment(haystack: str, needle: str) -> str:
    """从合并文本中取出与 needle 最对齐的一段，作虚拟旧/新侧文本（不向两侧扩字）。"""
    parts = _partition_merged_by_needles(haystack, [needle])
    return (parts[0] if parts else "") or (needle or "").strip()


def _partition_merged_by_needles(haystack: str, needles: list[str]) -> list[str]:
    """
    将合并长文按多段 needle 切成互不重叠的原文片段。
    用于一对多虚拟切片，避免旧实现「±2 字符」把上段句号/下段首字串进邻段。
    """
    h_raw = (haystack or "").strip()
    cleaned = [(n or "").strip() for n in needles]
    if not cleaned:
        return []
    if not h_raw:
        return list(cleaned)

    hn = _norm(h_raw)
    mapping = _norm_char_raw_indices(h_raw)
    spans: list[tuple[int, int] | None] = []
    cursor = 0
    for n_raw in cleaned:
        nn = _norm(n_raw)
        if not nn:
            spans.append(None)
            continue
        idx = hn.find(nn, cursor)
        if idx < 0:
            idx = hn.find(nn)
        if idx < 0:
            spans.append(None)
            continue
        spans.append((idx, idx + len(nn)))
        cursor = idx + len(nn)

    # 若相邻 span 重叠，收束到下一段起点，保证无重叠
    for i in range(len(spans) - 1):
        cur, nxt = spans[i], spans[i + 1]
        if cur and nxt and cur[1] > nxt[0]:
            spans[i] = (cur[0], nxt[0])

    out: list[str] = []
    for i, span in enumerate(spans):
        if span is None or not mapping:
            out.append(cleaned[i])
            continue
        a, b = span
        if a >= len(mapping) or b <= a:
            out.append(cleaned[i])
            continue
        raw_start = mapping[a]
        raw_end = mapping[min(b, len(mapping)) - 1] + 1
        frag = h_raw[raw_start:raw_end].strip()
        out.append(frag if frag else cleaned[i])
    return out


def _band_slice_ys(
    parent: dict,
    index: int,
    total: int,
    *,
    weights: list[float] | None = None,
) -> tuple[float, float]:
    """
    将父原子竖直带按份切开，供一对多虚拟切片使用。
    必须切「本侧」父框，不能抄对侧 y（否则页图框会错位/画不出来）。
    """
    n = max(1, int(total))
    i = max(0, min(int(index), n - 1))
    y0 = float(parent.get("y_start") or 0)
    y1 = float(parent.get("y_end") or (y0 + 0.05))
    if y1 <= y0:
        y1 = y0 + 0.05 * n
    span = y1 - y0
    if weights and len(weights) == n and sum(weights) > 0:
        s = float(sum(weights))
        acc = 0.0
        for j, w in enumerate(weights):
            nxt = acc + float(w) / s
            if j == i:
                return y0 + span * acc, y0 + span * nxt
            acc = nxt
    return y0 + span * i / n, y0 + span * (i + 1) / n


def _filter_text_atoms(atoms: list[dict]) -> list[dict]:
    return [
        a
        for a in atoms
        if a.get("atom_type") in _TEXT_TYPES
        and _atom_plain(a)
        and not _is_gap_placeholder_atom(a)
    ]


def _make_anchor(o: dict, n: dict, *, score: float) -> dict:
    change, sim = _classify_anchor_change(o, n)
    return {
        "anchor_id": f"{o.get('atom_id')}|{n.get('atom_id')}",
        "old_atom_id": o.get("atom_id"),
        "new_atom_id": n.get("atom_id"),
        "old_text": _atom_plain(o),
        "new_text": _atom_plain(n),
        "old_bbox": {
            "x_start": o.get("x_start"),
            "y_start": o.get("y_start"),
            "x_end": o.get("x_end"),
            "y_end": o.get("y_end"),
        },
        "new_bbox": {
            "x_start": n.get("x_start"),
            "y_start": n.get("y_start"),
            "x_end": n.get("x_end"),
            "y_end": n.get("y_end"),
        },
        "anchor_score": round(score, 3),
        "text_similarity": round(sim, 3),
        "change": change,
    }


def _rebind_merged_containment(
    old_text: list[dict],
    new_text: list[dict],
    anchors: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    一侧仍是合并长框、另一侧已拆成多段时：把 1:1 错配扩成 1:N（或 N:1）。
    返回 (anchors, old_text, new_text)；可能向列表追加虚拟切片原子。
    """
    old_by_id = {a.get("atom_id"): a for a in old_text if a.get("atom_id")}
    new_by_id = {a.get("atom_id"): a for a in new_text if a.get("atom_id")}
    matched_new = {a["new_atom_id"] for a in anchors}
    matched_old = {a["old_atom_id"] for a in anchors}
    unmatched_new = [a for a in new_text if a.get("atom_id") not in matched_new]
    unmatched_old = [a for a in old_text if a.get("atom_id") not in matched_old]

    new_anchors: list[dict] = []
    extra_old: list[dict] = []
    extra_new: list[dict] = []
    consumed_old_ids: set[str] = set()

    for anc in anchors:
        oid, nid = anc.get("old_atom_id"), anc.get("new_atom_id")
        if oid and str(oid) in consumed_old_ids:
            continue
        o = old_by_id.get(oid)
        n = new_by_id.get(nid)
        if not o or not n:
            new_anchors.append(anc)
            continue
        ot, nt = _atom_plain(o), _atom_plain(n)

        # 旧合并 → 新多段
        covered_new = [n] + [
            u for u in unmatched_new if _segment_covered_by(ot, _atom_plain(u))
        ]
        if (
            len(covered_new) >= 2
            and _segment_covered_by(ot, nt)
            and len(_norm(ot)) > len(_norm(nt)) * 1.25
        ):
            # 去重保持阅读序
            seen: set[str] = set()
            ordered: list[dict] = []
            for cand in sorted(covered_new, key=lambda a: (_ymid(a), float(a.get("x_start") or 0))):
                cid = cand.get("atom_id")
                if cid in seen:
                    continue
                seen.add(cid)
                ordered.append(cand)
            if len(ordered) >= 2:
                weights = [max(1.0, float(len(_norm(_atom_plain(c))))) for c in ordered]
                segs = _partition_merged_by_needles(ot, [_atom_plain(c) for c in ordered])
                for i, cand in enumerate(ordered):
                    seg = segs[i] if i < len(segs) else _atom_plain(cand)
                    syn = dict(o)
                    syn["atom_id"] = f"{oid}#m{i + 1}"
                    syn["ocr_text"] = seg
                    syn["content"] = seg
                    syn["split_from"] = oid
                    # 切旧侧父框自身竖直带（勿用新侧 y，否则旧页叠框错位）
                    yi0, yi1 = _band_slice_ys(o, i, len(ordered), weights=weights)
                    syn["y_start"] = yi0
                    syn["y_end"] = yi1
                    extra_old.append(syn)
                    score = _anchor_score(syn, cand)
                    new_anchors.append(_make_anchor(syn, cand, score=max(score, 0.55)))
                    unmatched_new = [u for u in unmatched_new if u.get("atom_id") != cand.get("atom_id")]
                if oid:
                    consumed_old_ids.add(str(oid))
                continue

        # 新合并 → 旧多段（含：第二句已被错配到正文时，从错配锚里赎回）
        covered_old = [o] + [
            u for u in unmatched_old if _segment_covered_by(nt, _atom_plain(u))
        ]
        steal_ids: set[str] = set()
        for other in anchors:
            if other is anc:
                continue
            oid2, nid2 = other.get("old_atom_id"), other.get("new_atom_id")
            if not oid2 or oid2 == oid or str(oid2) in consumed_old_ids:
                continue
            oo = old_by_id.get(oid2)
            on = new_by_id.get(nid2) if nid2 else None
            if not oo:
                continue
            oot = _atom_plain(oo)
            if not _segment_covered_by(nt, oot):
                continue
            # 与错配新框仍高度相似则不赎回；导读句错配到正文时相似度低
            if on and _partial_fuzz_ratio(_atom_plain(on), oot) >= 0.82:
                continue
            if any(_atom_plain(c) == oot for c in covered_old):
                continue
            covered_old.append(oo)
            steal_ids.add(str(oid2))
        if (
            len(covered_old) >= 2
            and _segment_covered_by(nt, ot)
            and len(_norm(nt)) > len(_norm(ot)) * 1.25
        ):
            seen_o: set[str] = set()
            ordered_o: list[dict] = []
            for cand in sorted(covered_old, key=lambda a: (_ymid(a), float(a.get("x_start") or 0))):
                cid = cand.get("atom_id")
                if cid in seen_o:
                    continue
                seen_o.add(cid)
                ordered_o.append(cand)
            if len(ordered_o) >= 2:
                weights = [max(1.0, float(len(_norm(_atom_plain(c))))) for c in ordered_o]
                segs = _partition_merged_by_needles(nt, [_atom_plain(c) for c in ordered_o])
                for i, cand in enumerate(ordered_o):
                    seg = segs[i] if i < len(segs) else _atom_plain(cand)
                    syn = dict(n)
                    syn["atom_id"] = f"{nid}#m{i + 1}"
                    syn["ocr_text"] = seg
                    syn["content"] = seg
                    syn["split_from"] = nid
                    # 切新侧父框自身竖直带（勿抄旧侧 y，否则新页叠框错位/漏画）
                    yi0, yi1 = _band_slice_ys(n, i, len(ordered_o), weights=weights)
                    syn["y_start"] = yi0
                    syn["y_end"] = yi1
                    extra_new.append(syn)
                    score = _anchor_score(cand, syn)
                    new_anchors.append(_make_anchor(cand, syn, score=max(score, 0.55)))
                    unmatched_old = [u for u in unmatched_old if u.get("atom_id") != cand.get("atom_id")]
                    if cand.get("atom_id"):
                        consumed_old_ids.add(str(cand.get("atom_id")))
                consumed_old_ids.update(steal_ids)
                continue

        new_anchors.append(anc)

    if extra_old:
        old_text = old_text + extra_old
    if extra_new:
        new_text = new_text + extra_new
    return new_anchors, old_text, new_text


def _bind_unanchored_merges(
    old_text: list[dict],
    new_text: list[dict],
    anchors: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    1:1 分过低未锚定、但一侧长框覆盖对侧多短框时（古诗合框 vs 逐行）：做 1:N 切片锚定。
    避免落入「页底短行→分页错位/新有旧无」误报。
    """
    matched_old = {a["old_atom_id"] for a in anchors}
    matched_new = {a["new_atom_id"] for a in anchors}
    id_to = {a.get("atom_id"): a for a in old_text + new_text if a.get("atom_id")}
    for mid in list(matched_old):
        parent = (id_to.get(mid) or {}).get("split_from")
        if parent:
            matched_old.add(parent)
    for mid in list(matched_new):
        parent = (id_to.get(mid) or {}).get("split_from")
        if parent:
            matched_new.add(parent)

    unmatched_old = [a for a in old_text if a.get("atom_id") not in matched_old]
    unmatched_new = [a for a in new_text if a.get("atom_id") not in matched_new]
    new_anchors = list(anchors)
    extra_old: list[dict] = []
    extra_new: list[dict] = []
    used_new: set[str] = set()
    used_old: set[str] = set()

    def _coverage_ok(host_plain: str, parts: list[dict]) -> bool:
        hn = len(_norm(host_plain))
        if hn < 12:
            return False
        pn = sum(len(_norm(_atom_plain(p))) for p in parts)
        return pn >= max(8, int(hn * 0.45))

    # 旧长 → 新多短
    for o in sorted(
        unmatched_old, key=lambda a: -len(_strip_for_content(_atom_plain(a)))
    ):
        oid = o.get("atom_id")
        if not oid or oid in used_old:
            continue
        ot = _atom_plain(o)
        covered = [
            u
            for u in unmatched_new
            if u.get("atom_id") not in used_new
            and _segment_covered_by(ot, _atom_plain(u))
        ]
        covered = sorted(
            covered, key=lambda a: (_ymid(a), float(a.get("x_start") or 0))
        )
        if len(covered) < 2 or not _coverage_ok(ot, covered):
            continue
        weights = [max(1.0, float(len(_norm(_atom_plain(c))))) for c in covered]
        segs = _partition_merged_by_needles(ot, [_atom_plain(c) for c in covered])
        for i, cand in enumerate(covered):
            seg = segs[i] if i < len(segs) else _atom_plain(cand)
            syn = dict(o)
            syn["atom_id"] = f"{oid}#m{i + 1}"
            syn["ocr_text"] = seg
            syn["content"] = seg
            syn["split_from"] = oid
            yi0, yi1 = _band_slice_ys(o, i, len(covered), weights=weights)
            syn["y_start"] = yi0
            syn["y_end"] = yi1
            extra_old.append(syn)
            score = _anchor_score(syn, cand)
            new_anchors.append(_make_anchor(syn, cand, score=max(score, 0.55)))
            used_new.add(str(cand.get("atom_id")))
        used_old.add(str(oid))

    unmatched_old = [a for a in unmatched_old if a.get("atom_id") not in used_old]
    unmatched_new = [a for a in unmatched_new if a.get("atom_id") not in used_new]

    # 新长 → 旧多短
    for n in sorted(
        unmatched_new, key=lambda a: -len(_strip_for_content(_atom_plain(a)))
    ):
        nid = n.get("atom_id")
        if not nid or nid in used_new:
            continue
        nt = _atom_plain(n)
        covered = [
            u
            for u in unmatched_old
            if u.get("atom_id") not in used_old
            and _segment_covered_by(nt, _atom_plain(u))
        ]
        covered = sorted(
            covered, key=lambda a: (_ymid(a), float(a.get("x_start") or 0))
        )
        if len(covered) < 2 or not _coverage_ok(nt, covered):
            continue
        weights = [max(1.0, float(len(_norm(_atom_plain(c))))) for c in covered]
        segs = _partition_merged_by_needles(nt, [_atom_plain(c) for c in covered])
        for i, cand in enumerate(covered):
            seg = segs[i] if i < len(segs) else _atom_plain(cand)
            syn = dict(n)
            syn["atom_id"] = f"{nid}#m{i + 1}"
            syn["ocr_text"] = seg
            syn["content"] = seg
            syn["split_from"] = nid
            yi0, yi1 = _band_slice_ys(n, i, len(covered), weights=weights)
            syn["y_start"] = yi0
            syn["y_end"] = yi1
            extra_new.append(syn)
            score = _anchor_score(cand, syn)
            new_anchors.append(_make_anchor(cand, syn, score=max(score, 0.55)))
            used_old.add(str(cand.get("atom_id")))
        used_new.add(str(nid))

    if extra_old:
        old_text = old_text + extra_old
    if extra_new:
        new_text = new_text + extra_new
    return new_anchors, old_text, new_text


def align_atoms(
    old_atoms: list[dict],
    new_atoms: list[dict],
    *,
    prev_old_atoms: list[dict] | None = None,
    prev_new_atoms: list[dict] | None = None,
) -> dict:
    """按阅读顺序 + 位置/文本相似度锚定新旧 text/title 原子。"""
    old_text = _merge_adjacent_form_pair_list_atoms(
        _merge_adjacent_preread_prompt_atoms(
            _explode_merged_list_atoms(_filter_text_atoms(old_atoms))
        )
    )
    new_text = _merge_adjacent_form_pair_list_atoms(
        _merge_adjacent_preread_prompt_atoms(
            _explode_merged_list_atoms(_filter_text_atoms(new_atoms))
        )
    )
    old_text.sort(key=lambda a: (_ymid(a), float(a.get("x_start", 0))))
    new_text.sort(key=lambda a: (_ymid(a), float(a.get("x_start", 0))))

    anchors = _pair_atoms_by_score(old_text, new_text, min_score=0.38)
    anchors = _repair_misaligned_by_text(old_text, new_text, anchors)

    anchors, old_text, new_text = _rebind_merged_containment(old_text, new_text, anchors)
    anchors, old_text, new_text = _bind_unanchored_merges(old_text, new_text, anchors)
    anchors, old_text, new_text, pagination_absorbed = _absorb_pagination_edge_fragments(
        old_text,
        new_text,
        anchors,
        prev_old_atoms=prev_old_atoms,
        prev_new_atoms=prev_new_atoms,
    )

    matched_old = {a["old_atom_id"] for a in anchors}
    matched_new = {a["new_atom_id"] for a in anchors}
    # 虚拟切片的父原子视为已参与比对，避免整段再报「旧有新无/新有旧无」
    id_to_atom = {a.get("atom_id"): a for a in old_text + new_text if a.get("atom_id")}
    for mid in list(matched_old):
        parent = (id_to_atom.get(mid) or {}).get("split_from")
        if parent:
            matched_old.add(parent)
    for mid in list(matched_new):
        parent = (id_to_atom.get(mid) or {}).get("split_from")
        if parent:
            matched_new.add(parent)
    matched_old |= pagination_absorbed
    matched_new |= pagination_absorbed
    unmatched_old = [
        _serialize_atom(a, side="old")
        for a in old_text
        if a.get("atom_id") not in matched_old and not a.get("pagination")
    ]
    unmatched_new = [
        _serialize_atom(a, side="new")
        for a in new_text
        if a.get("atom_id") not in matched_new and not a.get("pagination")
    ]

    soft_ok = ("一致", "分页错位")
    changed = [a for a in anchors if a["change"] not in soft_ok]
    return {
        "anchors": anchors,
        "unmatched_old": unmatched_old,
        "unmatched_new": unmatched_new,
        "changed_anchors": changed,
        "pagination_absorbed_ids": sorted(pagination_absorbed),
        "summary": {
            "old_text_atoms": len(old_text),
            "new_text_atoms": len(new_text),
            "anchored": len(anchors),
            "changed": len(changed),
            "unmatched_old": len(unmatched_old),
            "unmatched_new": len(unmatched_new),
            "pagination_absorbed": len(pagination_absorbed),
        },
        "_work_old": old_text,
        "_work_new": new_text,
    }


def _cache_path(
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    *,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> Path | None:
    ob = old_vol.blob_id and FileBlob.query.get(old_vol.blob_id)
    if not ob:
        return None
    if new_pdf_source == "draft":
        from ..volume_draft_pdfs import resolve_draft_blob_id

        try:
            nb_id = resolve_draft_blob_id(new_vol, preview_blob_id)
        except ValueError:
            return None
    else:
        nb_id = new_vol.blob_id
    nb = nb_id and FileBlob.query.get(nb_id)
    if not nb:
        return None
    src_tag = "d" if new_pdf_source == "draft" else "f"
    key = (
        f"v{_CACHE_VERSION}_{src_tag}_{ob.content_hash[:12]}_{nb.content_hash[:12]}"
        f"_o{old_page}_n{new_page}"
    )
    return base_data_dir() / "cache" / "diff_page_atoms" / f"{key}.json"


def load_page_atom_cache(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> dict | None:
    """仅读取已缓存的原子，不触发 OCR（磁盘 + MySQL）。"""
    if old_page < 1 or new_page < 1:
        raise ValueError("页码无效")
    cache_file, entry = _load_pair_entry(
        old_vol,
        new_vol,
        old_page,
        new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    if not entry.get("old_raw") and not entry.get("new_raw"):
        if not entry.get("text_compare") and not entry.get("image_compare"):
            return None
    return _build_pair_atoms_payload(
        entry,
        old_vol=old_vol,
        new_vol=new_vol,
        cache_file=cache_file,
    )


def _atom_plain_for_join(atom: dict) -> str:
    """整页拼接用：读音表压成「字(音)」单行，避免上方纯拼音行干扰整页 verdict。"""
    plain = _atom_plain(atom)
    pairs = _parse_reading_pairs(plain)
    if pairs:
        return _format_reading_pairs(pairs)
    return plain


def _join_reading_text(atoms: list[dict]) -> str:
    """按阅读顺序拼接正文 text/title（排除图注/饼图旁注，避免整页 blob 被图解污染）。"""
    text_atoms = [
        a
        for a in atoms
        if a.get("atom_type") in _TEXT_TYPES
        and _atom_plain(a)
        and not _is_gap_placeholder_atom(a)
        and _layout_role(a) == "body"
    ]
    text_atoms.sort(key=lambda a: (_ymid(a), float(a.get("x_start", 0))))
    return "\n".join(_atom_plain_for_join(a) for a in text_atoms)


def _is_blank_chunk(s: str) -> bool:
    """空白、换行、零宽字符等视为无内容。"""
    t = re.sub(r"[\s\u200b\u200c\u200d\ufeff\u00a0]+", "", s or "")
    return not t


# 课文旁脚注角标（带圈/上下标）；OCR 常漏识或形态互换，比对时作软差异
_FOOTNOTE_MARK_RE = re.compile(f"[{re.escape(_FOOTNOTE_MARK_CHARS)}]")


def _strip_footnote_marks(s: str) -> str:
    return _FOOTNOTE_MARK_RE.sub("", s or "")


def _is_footnote_mark_only(s: str) -> bool:
    t = re.sub(r"[\s\u200b\u200c\u200d\ufeff\u00a0]+", "", s or "")
    if not t:
        return False
    return not _FOOTNOTE_MARK_RE.sub("", t)


def _is_punct_only(s: str) -> bool:
    if _is_blank_chunk(s):
        return True
    s = (s or "").strip()
    if not s:
        return True
    if _is_footnote_mark_only(s):
        return True
    # 角标在 Python \w 里可能算“词字符”，先剥掉再判
    s = _strip_footnote_marks(s)
    if not s:
        return True
    return not re.search(r"[\w\u4e00-\u9fff]", s)


def _strip_for_content(s: str) -> str:
    """实质改文键：脚注角标剥掉，课序号①≈1≈¹，选学星*忽略。"""
    t = s or ""
    # 花牛歌① / 真珠³ / 缘(yuán)③ → 去掉脚注角标（汉字后或拼音括号后）
    # 保留开头课序号供下一行归一
    t = re.sub(
        rf"(?<=[\u4e00-\u9fff\)）])[{re.escape(_FOOTNOTE_MARK_CHARS)}]+",
        "",
        t,
    )
    t = _normalize_lesson_markers(t)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t)


def _is_noisy_latin_fragment(s: str) -> bool:
    """拼音被拆碎后的单字母/无元音碎片，不写入变化说明。

    注意：单个带调元音（ā/ǎ/à/ō/ǒ…）可能是声调 OCR 差，不得当碎片丢掉。
    """
    t = (s or "").strip()
    if not t or re.search(r"[\u4e00-\u9fff]", t):
        return False
    if not re.fullmatch(r"[a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ\s]+", t):
        return False
    compact = re.sub(r"\s+", "", t)
    # 带调元音：声调差异（tōng↔tǒng、liū↔liù）必须保留
    if re.fullmatch(r"[āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜĀÁǍÀĒÉĚÈĪÍǏÌŌÓǑÒŪÚǓÙǕǗǙǛ]+", compact):
        return False
    if len(compact) <= 1:
        return True
    if not re.search(r"[aeiouüAEIOUāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]", t):
        return True
    return False


def _char_ops(old_text: str, new_text: str, *, max_items: int = 40) -> list[dict]:
    """字符级差异：区分文字改写与标点差异；忽略纯空白。"""
    from difflib import SequenceMatcher

    # 去掉 LaTeX 公式分隔符 $，避免 OCR 侧有 $ 而文字层侧无 $ 时的伪差异
    old_text = (old_text or "").replace("$", "")
    new_text = (new_text or "").replace("$", "")

    sm = SequenceMatcher(None, old_text or "", new_text or "", autojunk=False)
    ops: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_chunk = (old_text or "")[i1:i2]
        new_chunk = (new_text or "")[j1:j2]
        if _is_blank_chunk(old_chunk) and _is_blank_chunk(new_chunk):
            continue
        if tag == "delete" and _is_blank_chunk(old_chunk):
            continue
        if tag == "insert" and _is_blank_chunk(new_chunk):
            continue
        if tag == "replace" and _is_blank_chunk(old_chunk) and _is_blank_chunk(new_chunk):
            continue
        # ①↔1、脚注上标 ¹ 等课标记形态差：忽略
        if _soft_lesson_marker_equivalent(old_chunk, new_chunk):
            continue
        if tag == "replace":
            if _is_footnote_mark_only(old_chunk) and _is_footnote_mark_only(new_chunk):
                # 角标序号替换（①↔②）作软标点；完全相同不会进入 replace
                if old_chunk == new_chunk:
                    continue
                kind = "标点差异"
            elif _punct_soft_equivalent(old_chunk, new_chunk) and (
                _is_punct_only(old_chunk) or _is_punct_only(new_chunk)
                or _strip_for_content(old_chunk) == _strip_for_content(new_chunk)
            ):
                # 全半角括号等 OCR 形近标点：不报差异
                continue
            elif _strip_for_content(old_chunk) == _strip_for_content(new_chunk):
                # 两侧无汉字/字母：标点或空白差异；纯空白已在上面跳过
                kind = "标点差异"
            elif _is_punct_only(old_chunk) and _is_punct_only(new_chunk):
                if _punct_soft_equivalent(old_chunk, new_chunk):
                    continue
                kind = "标点差异"
            else:
                kind = "文字改写"
        elif tag == "delete":
            # 单侧漏识脚注角标/选学星：不报「删除①/*」假改动
            if _is_footnote_mark_only(old_chunk) or _is_lesson_ornament_only(old_chunk):
                continue
            kind = "标点删除" if _is_punct_only(old_chunk) else "文字删除"
        else:
            if _is_footnote_mark_only(new_chunk) or _is_lesson_ornament_only(new_chunk):
                continue
            kind = "标点新增" if _is_punct_only(new_chunk) else "文字新增"
        if kind.startswith("标点") and _punct_soft_equivalent(old_chunk, new_chunk):
            continue
        if kind.startswith("文字") and (
            _is_noisy_latin_fragment(old_chunk) or _is_noisy_latin_fragment(new_chunk)
        ):
            # 两侧都是拉丁碎片则丢弃；一侧碎片一侧汉字则保留汉字侧语义
            if _is_noisy_latin_fragment(old_chunk) and _is_noisy_latin_fragment(new_chunk):
                continue
            if _is_noisy_latin_fragment(old_chunk) and not _strip_for_content(new_chunk):
                continue
            if _is_noisy_latin_fragment(new_chunk) and not _strip_for_content(old_chunk):
                continue
        ops.append(
            {
                "kind": kind,
                "old": old_chunk[:120],
                "new": new_chunk[:120],
                "old_span": [i1, i2],
                "new_span": [j1, j2],
            }
        )
        if len(ops) >= max_items:
            break
    return ops


def _strip_pinyin_annotations(text: str) -> str:
    """夹注 字(pīn) → 字，便于判断是否仅音标差异。"""
    return _INLINE_PINYIN_RE.sub(r"\1", text or "")


def _hanzi_content_key(text: str) -> str:
    """实质内容键：忽略夹注拼音与标点/脚注形态。"""
    return _strip_for_content(_strip_pinyin_annotations(text))


def _is_pinyin_annotation_diff(old_text: str, new_text: str) -> bool:
    """汉字实质相同，但夹注拼音/音标有增减或改调。"""
    ot, nt = old_text or "", new_text or ""
    if ot == nt:
        return False
    if _hanzi_content_key(ot) != _hanzi_content_key(nt):
        return False
    if _INLINE_PINYIN_RE.findall(ot) != _INLINE_PINYIN_RE.findall(nt):
        return True
    old_latin = "".join(_PINYIN_SYL_RE.findall(ot))
    new_latin = "".join(_PINYIN_SYL_RE.findall(nt))
    return bool(old_latin or new_latin) and old_latin != new_latin


def _block_verdict_from_ops(ops: list[dict], *, old_text: str, new_text: str) -> str:
    if not old_text and not new_text:
        return "无文字"
    if not old_text:
        return "新有旧无"
    if not new_text:
        return "旧有新无"
    # 仅引号挪位（如 “…推荐会” vs “…好地方”推荐会）→ 仅标点差异
    if _texts_equal_ignoring_quotes(old_text, new_text):
        return "一致" if _quotes_soft_equal(old_text, new_text) else "仅标点差异"
    if _strip_for_content(old_text) == _strip_for_content(new_text) and not ops:
        return "一致"
    if not ops:
        return "一致"
    punct_ops = [o for o in ops if "标点" in o["kind"]]
    text_ops = [o for o in ops if "标点" not in o["kind"]]
    if _strip_for_content(old_text) == _strip_for_content(new_text):
        if not text_ops:
            return "一致" if not punct_ops else "仅标点差异"
        # 实质内容相同却仍有「文字」ops（多为空白残留）→ 一致
        return "一致"
    if not text_ops and punct_ops:
        return "仅标点差异"
    # 汉字相同、仅注音/声调音标不同 → 单独状态（便于汇总与人工复核）
    if _is_pinyin_annotation_diff(old_text, new_text):
        return "音标变动注意"
    if text_ops:
        return "文字有差异"
    return "有差异"


_PINYIN_SYL_RE = re.compile(
    r"[a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+"
)
_INLINE_PINYIN_RE = re.compile(
    r"([\u4e00-\u9fff])\(([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+)\)"
)
_TONE_FOLD = str.maketrans(
    {
        "ā": "a",
        "á": "a",
        "ǎ": "a",
        "à": "a",
        "ē": "e",
        "é": "e",
        "ě": "e",
        "è": "e",
        "ī": "i",
        "í": "i",
        "ǐ": "i",
        "ì": "i",
        "ō": "o",
        "ó": "o",
        "ǒ": "o",
        "ò": "o",
        "ū": "u",
        "ú": "u",
        "ǔ": "u",
        "ù": "u",
        "ǖ": "ü",
        "ǘ": "ü",
        "ǚ": "ü",
        "ǜ": "ü",
        "Ā": "a",
        "Á": "a",
        "Ǎ": "a",
        "À": "a",
        "Ē": "e",
        "É": "e",
        "Ě": "e",
        "È": "e",
        "Ī": "i",
        "Í": "i",
        "Ǐ": "i",
        "Ì": "i",
        "Ō": "o",
        "Ó": "o",
        "Ǒ": "o",
        "Ò": "o",
        "Ū": "u",
        "Ú": "u",
        "Ǔ": "u",
        "Ù": "u",
        "Ǖ": "ü",
        "Ǘ": "ü",
        "Ǚ": "ü",
        "Ǜ": "ü",
        "Ü": "ü",
    }
)


def _extract_hanzi_list(text: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]", text or "")


def _pinyin_base(syl: str) -> str:
    return (syl or "").strip().lower().translate(_TONE_FOLD)


def _collect_body_pinyin(atoms: list[dict] | None) -> dict[str, str]:
    """从正文夹注收集 字→拼音；跳过读音表/写字表，避免把词表 OCR 错读写回。"""
    out: dict[str, str] = {}
    for a in atoms or []:
        if a.get("atom_type") not in _TEXT_TYPES:
            continue
        text = _atom_plain(a)
        if not text:
            continue
        if _looks_like_reading_list(text) or _looks_like_writing_grid(text):
            continue
        for han, syl in _INLINE_PINYIN_RE.findall(text):
            if han not in out:
                out[han] = syl
    return out


def _merge_body_pinyin(*maps: dict[str, str] | None) -> dict[str, str]:
    """靠后的映射覆盖靠前的（本页正文优先于邻页提示）。"""
    out: dict[str, str] = {}
    for m in maps:
        if not m:
            continue
        out.update(m)
    return out


def _neighbor_body_pinyin(
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    *,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
    lookback: int = 5,
) -> tuple[dict[str, str], dict[str, str]]:
    """从前序已 OCR 页收集正文夹注，供读音表声调校准。"""
    old_acc: dict[str, str] = {}
    new_acc: dict[str, str] = {}
    for d in range(lookback, 0, -1):
        op = old_page - d
        np_ = new_page - d
        if op < 1 or np_ < 1:
            continue
        _, entry = _read_cache_entry(
            old_vol,
            new_vol,
            op,
            np_,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
        )
        if not entry:
            continue
        if entry.get("old_text_ocr_done"):
            old_acc.update(_collect_body_pinyin(entry.get("old_raw") or []))
        if entry.get("new_text_ocr_done"):
            new_acc.update(_collect_body_pinyin(entry.get("new_raw") or []))
    return old_acc, new_acc


def _correct_reading_pairs(
    pairs: list[tuple[str, str]],
    body_pinyin: dict[str, str] | None,
) -> list[tuple[str, str]]:
    """读音表音节与正文夹注仅声调不同时，以正文为准。"""
    if not pairs or not body_pinyin:
        return pairs
    fixed: list[tuple[str, str]] = []
    for han, syl in pairs:
        body = body_pinyin.get(han)
        if body and _pinyin_base(body) == _pinyin_base(syl) and body != syl:
            fixed.append((han, body))
        else:
            fixed.append((han, syl))
    return fixed


def _looks_like_writing_grid(text: str) -> bool:
    """田字格/写字表：多为空格分隔的单字，几乎无句读。"""
    hans = _extract_hanzi_list(text)
    if len(hans) < 8:
        return False
    plain = re.sub(r"\s+", "", text or "")
    if not plain or len(hans) / len(plain) < 0.9:
        return False
    # 有句读 → 正文，不是写字表
    if any(ch in plain for ch in "。，、；：？！“”\"'（）()"):
        return False
    if "(" in (text or "") or "（" in (text or ""):
        return False
    if _parse_reading_pairs(text):
        return False
    # 至少 6 个「单字 token」（空格拆开的田字格）
    parts = re.findall(r"[\u4e00-\u9fff]+", text or "")
    singles = sum(1 for p in parts if len(p) == 1)
    if singles < 6:
        return False
    return True


_WORD_BANK_TOKEN_RE = re.compile(r"^[\u4e00-\u9fff]{2,4}$")
_PINYIN_PAREN_STRIP_RE = re.compile(
    r"\([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+\)"
)


def _looks_like_word_bank(text: str) -> bool:
    """词句段词语/成语表：多枚 2~4 字词语空格或换行排列，非散文续句。"""
    t = (text or "").strip()
    if not t or any(ch in t for ch in "。！？"):
        return False
    plain = _PINYIN_PAREN_STRIP_RE.sub("", t)
    tokens = [x for x in re.split(r"[\s　]+", plain) if x]
    if len(tokens) < 4:
        return False
    good = sum(1 for x in tokens if _WORD_BANK_TOKEN_RE.match(x))
    return good >= 4 and good >= len(tokens) * 0.7


_GLUED_EXERCISE_START_RE = re.compile(
    r"(选一个|选做|选一选|读下面|用上两三个加点|和同学交流|体会每组)"
)


def _peel_glued_exercise_from_word_bank(text: str) -> str:
    """
    词语表尾若粘上下页练习指令（误拼或 OCR 并框），剥掉练习句只留词语表。
    例：…悄(qiǎo)无声息选一个事物，用上两三个加点的词语描绘它，再写下来。
    """
    t = (text or "").strip()
    if not t:
        return text or ""
    m = _GLUED_EXERCISE_START_RE.search(t)
    if not m or m.start() < 6:
        return t
    head = t[: m.start()].rstrip()
    if not head:
        return t
    if _looks_like_word_bank(head):
        return head
    return t


def _partition_seq_moves(
    deleted: list[str],
    added: list[str],
    *,
    stem_fn=None,
) -> tuple[list[str], list[tuple[str, str]], list[str], list[str]]:
    """从 SequenceMatcher 的删/增列表中拆出「移动」与「同条改写」。

    返回 (moved, rewritten, deleted_rest, added_rest)。
    rewritten 为 (旧条, 新条)，仅当 stem_fn(旧)==stem_fn(新) 且全文不同。
    """
    from collections import Counter

    dc = Counter(deleted)
    ac = Counter(added)
    moved: list[str] = []
    for item in list(dc.keys()):
        n = min(dc[item], ac[item])
        if n <= 0:
            continue
        moved.extend([item] * n)
        dc[item] -= n
        ac[item] -= n
        if dc[item] <= 0:
            del dc[item]
        if ac[item] <= 0:
            del ac[item]

    rewritten: list[tuple[str, str]] = []
    if stem_fn is not None and dc and ac:
        d_items = list(dc.elements())
        a_items = list(ac.elements())
        used_a: set[int] = set()
        keep_d: list[str] = []
        for d in d_items:
            hit = -1
            for j, a in enumerate(a_items):
                if j in used_a:
                    continue
                if stem_fn(d) == stem_fn(a) and d != a:
                    hit = j
                    break
            if hit >= 0:
                rewritten.append((d, a_items[hit]))
                used_a.add(hit)
            else:
                keep_d.append(d)
        keep_a = [a for j, a in enumerate(a_items) if j not in used_a]
        return moved, rewritten, keep_d, keep_a

    return moved, rewritten, list(dc.elements()), list(ac.elements())


def _writing_grid_summary(old_text: str, new_text: str) -> tuple[str, str, list[str]]:
    """返回 (change, change_summary, change_summary_lines)。"""
    from difflib import SequenceMatcher

    o = _extract_hanzi_list(old_text)
    n = _extract_hanzi_list(new_text)
    if o == n:
        return "一致", "写字表：没变化", ["写字表", "没变化"]
    if sorted(o) == sorted(n):
        return "一致", "写字表：用字相同，顺序有调整", ["写字表", "用字相同，顺序有调整"]
    sm = SequenceMatcher(None, o, n, autojunk=False)
    deleted: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            deleted.extend(o[i1:i2])
        elif tag == "insert":
            added.extend(n[j1:j2])
        elif tag == "replace":
            deleted.extend(o[i1:i2])
            added.extend(n[j1:j2])
    moved, _rew, deleted, added = _partition_seq_moves(deleted, added)
    lines: list[str] = ["写字表"]
    if deleted:
        lines.append("删" + "、".join(f"「{c}」" for c in deleted))
    if added:
        lines.append("增" + "、".join(f"「{c}」" for c in added))
    if moved:
        lines.append("移动" + "、".join(f"「{c}」" for c in moved))
    if len(lines) == 1:
        lines.append("有差异")
    summary = "写字表：" + "；".join(lines[1:])
    return "文字有差异", summary, lines


_FORM_PAIR_ENTRY_RE = re.compile(
    r"[\u4e00-\u9fff]\s*[—\-－]\s*[\u4e00-\u9fff]"
    r"(?:\([A-Za-züÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+\))?"
    r"(?:（[^）]{1,16}）|\([^\)]{1,16}\))?"
)


def _parse_form_pair_entries(text: str) -> list[str] | None:
    """识字加油站类「冈—纲(gāng)（提纲）」词条列表。"""
    entries = [m.group(0).strip() for m in _FORM_PAIR_ENTRY_RE.finditer(text or "")]
    if len(entries) < 3:
        return None
    return entries


def _form_pair_stem(entry: str) -> str:
    m = re.match(r"([\u4e00-\u9fff])\s*[—\-－]\s*([\u4e00-\u9fff])", entry or "")
    if not m:
        return (entry or "").strip()
    return f"{m.group(1)}—{m.group(2)}"


def _looks_like_form_pair_list(text: str) -> bool:
    return _parse_form_pair_entries(text) is not None


def _form_pair_list_summary(old_text: str, new_text: str) -> tuple[str, str, list[str], list[dict]] | None:
    """形近字对照表：按词条比对，区分增/删/移动/改写。"""
    from difflib import SequenceMatcher

    o = _parse_form_pair_entries(old_text)
    n = _parse_form_pair_entries(new_text)
    if not o or not n:
        return None
    if o == n:
        return "一致", "对照表：没变化", ["对照表", "没变化"], []
    sm = SequenceMatcher(None, o, n, autojunk=False)
    deleted: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            deleted.extend(o[i1:i2])
        elif tag == "insert":
            added.extend(n[j1:j2])
        elif tag == "replace":
            deleted.extend(o[i1:i2])
            added.extend(n[j1:j2])
    moved, rewritten, deleted, added = _partition_seq_moves(
        deleted, added, stem_fn=_form_pair_stem
    )
    semantic: list[dict] = []
    for item in deleted:
        semantic.append({"kind": "文字删除", "old": item, "new": "", "old_span": [0, 0], "new_span": [0, 0]})
    for item in added:
        semantic.append({"kind": "文字新增", "old": "", "new": item, "old_span": [0, 0], "new_span": [0, 0]})
    for item in moved:
        semantic.append({"kind": "文字移动", "old": item, "new": item, "old_span": [0, 0], "new_span": [0, 0]})
    for old_e, new_e in rewritten:
        semantic.append({"kind": "文字改写", "old": old_e, "new": new_e, "old_span": [0, 0], "new_span": [0, 0]})
    if not semantic:
        return "一致", "对照表：没变化", ["对照表", "没变化"], []
    lines: list[str] = ["对照表"]
    if deleted:
        lines.append("删 " + "、".join(deleted))
    if added:
        lines.append("增 " + "、".join(added))
    if moved:
        lines.append("移动 " + "、".join(moved))
    if rewritten:
        lines.append("改 " + "、".join(f"{a}→{b}" for a, b in rewritten))
    summary = "对照表：" + "；".join(lines[1:])
    return "文字有差异", summary, lines, semantic


def _is_narrative_prose(text: str) -> bool:
    """正文叙述句（含句读），不是读音表词表。"""
    plain = re.sub(r"\s+", "", text or "")
    if not plain:
        return False
    if any(ch in plain for ch in "。！？；"):
        return True
    # 较长且含逗号的叙述
    if len(plain) >= 36 and "，" in plain:
        return True
    return False


def _strip_reading_list_noise(text: str) -> str:
    """去掉粘连进读音表的课标题/页码星号，避免上下拼音+字(音)+标题叠在一起。"""
    lines_out: list[str] = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        compact = re.sub(r"\s+", "", ln)
        # 「现代诗二首」：无空格的短标题；「为 盐 薄…」词表行保留
        if (
            " " not in ln
            and "\u3000" not in ln
            and not _PINYIN_SYL_RE.search(ln)
            and "(" not in ln
            and re.fullmatch(r"[\u4e00-\u9fff]{2,10}", compact)
        ):
            continue
        # …萤(yíng)3* → 去掉注音后的页码星号
        ln = re.sub(
            r"(\([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+\))\s*\d+\*?",
            r"\1",
            ln,
        )
        # 同一行最后一个字(音)后的粘连标题：…萤(yíng) 现代诗
        ln = re.sub(r"(\))\s*[\u4e00-\u9fff]{2,12}\s*$", r"\1", ln)
        ln = re.sub(r"\s*\d+\*?\s*$", "", ln).strip()
        if ln:
            lines_out.append(ln)
    return "\n".join(lines_out)


def _parse_reading_pairs(text: str) -> list[tuple[str, str]] | None:
    """解析读音表为 [(字, 音节), ...]。

    支持：
    - 纯「字(音) 字(音) …」
    - 「上方纯拼音行 + 下方汉字行」（新教材 OCR 常见）
    - 「上方纯拼音行 + 下方字(音)行」（旧教材 OCR 常见；展示时丢掉纯拼音行）
    正文里夹注拼音（如 闷(mèn)雷）绝不算读音表。
    OCR 偶发同时给出上下拼音行 + 字(音) 行时，以字(音) 为准（与第一课时展示一致）。
    """
    if _is_narrative_prose(text):
        return None

    raw = _strip_reading_list_noise(text or "")
    if not raw:
        return None
    # 1) 优先吃「字(音)」——即使上方还有纯拼音行也认（残留拼音行不算叙述噪声）
    paren = re.findall(
        r"([\u4e00-\u9fff])\(([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+)\)",
        raw,
    )
    if len(paren) >= 3:
        residual = re.sub(
            r"[\u4e00-\u9fff]\([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+\)",
            "",
            raw,
        )
        residual = _PINYIN_SYL_RE.sub("", residual)
        residual = re.sub(r"[\s·•、，,；;：:\-_/|]+", "", residual)
        residual_hans = re.findall(r"[\u4e00-\u9fff]", residual)
        if len(residual_hans) <= 2 and len(residual) <= 2:
            return [(h, s) for h, s in paren]
        # 噪声标题已剥；残留极短则仍按字(音)词表
        if re.fullmatch(r"[\u4e00-\u9fff\d\*]{0,12}", residual or ""):
            return [(h, s) for h, s in paren]

    # 2) 拼音行 + 裸汉字行 → 按序配对
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    syls: list[str] = []
    hans: list[str] = []
    for ln in lines:
        found_syls = _PINYIN_SYL_RE.findall(ln)
        found_hans = re.findall(r"[\u4e00-\u9fff]", ln)
        # 纯拼音行（可多行折行）累加音节；含「字(音)」的行不走此支，避免音节重复计数
        has_paren_pair = bool(
            re.search(
                r"[\u4e00-\u9fff]\([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+\)",
                ln,
            )
        )
        if found_syls and len(found_hans) <= 2 and not has_paren_pair:
            syls.extend(found_syls)
        if len(found_hans) >= 4 and len(found_syls) <= 1:
            hans.extend(found_hans)
    if len(syls) >= 4 and len(hans) >= 4 and abs(len(syls) - len(hans)) <= 2:
        n = min(len(syls), len(hans))
        return list(zip(hans[:n], syls[:n]))
    return None


def _drop_redundant_top_pinyin_line(text: str) -> str:
    """已有字(音) 时删掉单独的上下拼音行（OCR 双写）。"""
    if not text or "(" not in text:
        return text
    if not re.search(
        r"[\u4e00-\u9fff]\([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+\)",
        text,
    ):
        return text
    kept: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        syls = _PINYIN_SYL_RE.findall(s)
        hans = re.findall(r"[\u4e00-\u9fff]", s)
        # 纯拼音行且几乎无汉字、无字(音)
        if (
            syls
            and len(hans) <= 1
            and "(" not in s
            and len(syls) >= 2
        ):
            continue
        kept.append(ln.rstrip())
    return "\n".join(kept).strip() or text


def _looks_like_reading_list(text: str) -> bool:
    return _parse_reading_pairs(text) is not None


def _format_reading_pairs(pairs: list[tuple[str, str]]) -> str:
    """成组展示：巢(cháo) 苇(wěi) …，注音一律在字右侧，不单独占一行。"""
    return " ".join(f"{h}({s})" for h, s in pairs)


def _span_of_pair_token(formatted: str, token: str, *, occurrence: int = 0) -> list[int]:
    """在成组串中定位 token 的 [start, end)。"""
    start = 0
    found = 0
    while True:
        i = formatted.find(token, start)
        if i < 0:
            return [0, 0]
        if found == occurrence:
            return [i, i + len(token)]
        found += 1
        start = i + len(token)


def _reading_list_enrich(
    old_text: str,
    new_text: str,
    *,
    old_body_pinyin: dict[str, str] | None = None,
    new_body_pinyin: dict[str, str] | None = None,
) -> tuple[str, str, list[dict], str, str, list[str]] | None:
    """返回 (change, summary, semantic_ops, old_display, new_display, summary_lines)。"""
    op = _parse_reading_pairs(old_text)
    np_ = _parse_reading_pairs(new_text)
    if not op and not np_:
        return None
    op = _correct_reading_pairs(op or [], old_body_pinyin)
    np_ = _correct_reading_pairs(np_ or [], new_body_pinyin)
    if not op and not np_:
        return None
    from difflib import SequenceMatcher

    old_disp = _format_reading_pairs(op) if op else ""
    new_disp = _format_reading_pairs(np_) if np_ else ""
    old_hans = [h for h, _ in op]
    new_hans = [h for h, _ in np_]
    old_syl = {h: s for h, s in op}
    new_syl = {h: s for h, s in np_}
    sm = SequenceMatcher(None, old_hans, new_hans, autojunk=False)
    deleted: list[str] = []
    added: list[str] = []
    semantic: list[dict] = []

    def _add_del(h: str) -> None:
        pair = f"{h}({old_syl[h]})"
        deleted.append(pair)
        semantic.append(
            {
                "kind": "文字删除",
                "old": pair,
                "new": "",
                "old_span": _span_of_pair_token(old_disp, pair),
                "new_span": [0, 0],
            }
        )

    def _add_ins(h: str) -> None:
        pair = f"{h}({new_syl[h]})"
        added.append(pair)
        semantic.append(
            {
                "kind": "文字新增",
                "old": "",
                "new": pair,
                "old_span": [0, 0],
                "new_span": _span_of_pair_token(new_disp, pair),
            }
        )

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for h in old_hans[i1:i2]:
                osyl, nsyl = old_syl.get(h, ""), new_syl.get(h, "")
                if osyl != nsyl:
                    opair = f"{h}({osyl})"
                    npair = f"{h}({nsyl})"
                    semantic.append(
                        {
                            "kind": "文字替换",
                            "old": opair,
                            "new": npair,
                            "old_span": _span_of_pair_token(old_disp, opair),
                            "new_span": _span_of_pair_token(new_disp, npair),
                        }
                    )
        elif tag == "delete":
            for h in old_hans[i1:i2]:
                _add_del(h)
        elif tag == "insert":
            for h in new_hans[j1:j2]:
                _add_ins(h)
        elif tag == "replace":
            for h in old_hans[i1:i2]:
                _add_del(h)
            for h in new_hans[j1:j2]:
                _add_ins(h)

    if not semantic:
        return "一致", "读音表：没变化", [], old_disp, new_disp, ["读音表", "没变化"]

    from collections import Counter

    moved, _rew, deleted, added = _partition_seq_moves(deleted, added)
    if moved:
        del_left = Counter(moved)
        ins_left = Counter(moved)
        kept_ops: list[dict] = []
        for sop in semantic:
            kind = sop.get("kind")
            if kind == "文字删除":
                tok = sop.get("old") or ""
                if del_left.get(tok, 0) > 0:
                    del_left[tok] -= 1
                    continue
            elif kind == "文字新增":
                tok = sop.get("new") or ""
                if ins_left.get(tok, 0) > 0:
                    ins_left[tok] -= 1
                    continue
            kept_ops.append(sop)
        for tok in moved:
            kept_ops.append(
                {
                    "kind": "文字移动",
                    "old": tok,
                    "new": tok,
                    "old_span": _span_of_pair_token(old_disp, tok),
                    "new_span": _span_of_pair_token(new_disp, tok),
                }
            )
        semantic = kept_ops

    lines: list[str] = ["读音表"]
    if deleted:
        lines.append("删 " + "、".join(deleted))
    if added:
        lines.append("增 " + "、".join(added))
    if moved:
        lines.append("移动 " + "、".join(moved))
    tone = [o for o in semantic if o.get("kind") == "文字替换"]
    if tone:
        lines.append(
            "改 "
            + "、".join(f"{o['old']}→{o['new']}" for o in tone)
        )
    summary = "读音表：" + "；".join(lines[1:])
    # 仅音节/声调改动、生字集合不变 → 音标变动注意
    if tone and not deleted and not added and not moved:
        if all((o.get("kind") or "") == "文字替换" for o in semantic):
            return "音标变动注意", summary, semantic, old_disp, new_disp, lines
    return "文字有差异", summary, semantic, old_disp, new_disp, lines


def _detect_block_role(text: str) -> str:
    if _looks_like_reading_list(text):
        return "reading_list"
    if _looks_like_writing_grid(text):
        return "writing_grid"
    if _looks_like_form_pair_list(text):
        return "form_pair_list"
    plain = re.sub(r"\s+", "", text or "")
    if 6 <= len(plain) <= 80 and any(
        h in plain for h in ("读这段话", "我仿佛", "我觉得", "边观察", "记录吧")
    ):
        return "side_note"
    return "other"


def _enrich_block_row(
    row: dict,
    *,
    old_body_pinyin: dict[str, str] | None = None,
    new_body_pinyin: dict[str, str] | None = None,
) -> dict:
    """为写字表/读音表补充 block_role 与 change_summary。"""
    from ..llm.page_text_extract import repair_common_pinyin_tone_ocr

    ot = repair_common_pinyin_tone_ocr(
        _drop_redundant_top_pinyin_line(row.get("old_text") or "")
    )
    nt = repair_common_pinyin_tone_ocr(
        _drop_redundant_top_pinyin_line(row.get("new_text") or "")
    )
    row["old_text"] = ot
    row["new_text"] = nt
    sample = ot if len(_extract_hanzi_list(ot)) >= len(_extract_hanzi_list(nt)) else nt
    role = "other"
    if _looks_like_reading_list(ot) or _looks_like_reading_list(nt):
        role = "reading_list"
    elif _looks_like_writing_grid(ot) or _looks_like_writing_grid(nt):
        role = "writing_grid"
    elif _looks_like_form_pair_list(ot) or _looks_like_form_pair_list(nt):
        role = "form_pair_list"
    elif sample:
        role = _detect_block_role(sample)
    row["block_role"] = role

    if role == "writing_grid":
        change, summary, lines = _writing_grid_summary(ot, nt)
        row["change"] = change
        row["change_summary"] = summary
        row["change_summary_lines"] = lines
        # 保留字符 ops 供高亮；过滤空白
        row["ops"] = _char_ops(ot, nt, max_items=20)
        return row

    if role == "form_pair_list":
        enriched = _form_pair_list_summary(ot, nt)
        if enriched:
            change, summary, lines, semantic = enriched
            row["change"] = change
            row["change_summary"] = summary
            row["change_summary_lines"] = lines
            row["ops"] = semantic
            return row

    if role == "reading_list":
        enriched = _reading_list_enrich(
            ot,
            nt,
            old_body_pinyin=old_body_pinyin,
            new_body_pinyin=new_body_pinyin,
        )
        if enriched:
            change, summary, semantic, old_disp, new_disp, lines = enriched
            row["change"] = change
            row["change_summary"] = summary
            row["change_summary_lines"] = lines
            row["ops"] = semantic
            row["old_text"] = old_disp
            row["new_text"] = new_disp
            return row
        # 单侧可解析时仍规范成字(音)，去掉上下拼音/粘连标题
        op = _parse_reading_pairs(ot)
        np_ = _parse_reading_pairs(nt)
        if op:
            row["old_text"] = _format_reading_pairs(
                _correct_reading_pairs(op, old_body_pinyin)
            )
        if np_:
            row["new_text"] = _format_reading_pairs(
                _correct_reading_pairs(np_, new_body_pinyin)
            )
        if op or np_:
            return row
        # 误判读音表时回退普通正文 diff（保留拼音标红）
        row["block_role"] = "other"

    qlines = _quote_move_summary_lines(ot, nt)
    if qlines:
        row["change"] = "仅标点差异"
        row["change_summary_lines"] = qlines
        row["change_summary"] = "；".join(qlines)

    return row


def _atom_lookup_from_alignment(
    alignment: dict,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """
    工作集 + 未匹配 → 旧/新分表 id 索引（含 bbox）。
    必须分侧：两边常共用 A010-005 这类编号，混进同一 dict 会被对侧坐标覆盖，
    导致比对表阅读序颠倒（如诗行被标成脚注 y，排到脚注后面）。
    """
    old_by: dict[str, dict] = {}
    new_by: dict[str, dict] = {}

    def _put(bucket: dict[str, dict], atom: dict) -> None:
        aid = atom.get("atom_id")
        if aid:
            bucket[str(aid)] = atom

    for a in alignment.get("_work_old") or []:
        _put(old_by, a)
    for a in alignment.get("_work_new") or []:
        _put(new_by, a)

    def _from_unmatched(bucket: dict[str, dict], u: dict) -> None:
        aid = u.get("atom_id")
        if not aid:
            return
        aid_s = str(aid)
        if aid_s in bucket:
            return
        bb = u.get("bbox") or {}
        bucket[aid_s] = {
            "atom_id": aid_s,
            "x_start": bb.get("x_start", 0),
            "y_start": bb.get("y_start", 0),
            "x_end": bb.get("x_end", 1),
            "y_end": bb.get("y_end", 1),
        }

    for u in alignment.get("unmatched_old") or []:
        _from_unmatched(old_by, u)
    for u in alignment.get("unmatched_new") or []:
        _from_unmatched(new_by, u)

    for a in alignment.get("anchors") or []:
        oid, nid = a.get("old_atom_id"), a.get("new_atom_id")
        obb, nbb = a.get("old_bbox") or {}, a.get("new_bbox") or {}
        if oid and str(oid) not in old_by and obb:
            old_by[str(oid)] = {
                "atom_id": str(oid),
                "x_start": obb.get("x_start", 0),
                "y_start": obb.get("y_start", 0),
                "x_end": obb.get("x_end", 1),
                "y_end": obb.get("y_end", 1),
            }
        if nid and str(nid) not in new_by and nbb:
            new_by[str(nid)] = {
                "atom_id": str(nid),
                "x_start": nbb.get("x_start", 0),
                "y_start": nbb.get("y_start", 0),
                "x_end": nbb.get("x_end", 1),
                "y_end": nbb.get("y_end", 1),
            }
    return old_by, new_by


def _resolve_side_atom(by_id: dict[str, dict], aid: str | None) -> dict | None:
    if not aid:
        return None
    aid_s = str(aid)
    atom = by_id.get(aid_s)
    if atom:
        return atom
    # A010-004#m2 → 回退父框（切片未入表时）
    if "#" in aid_s:
        return by_id.get(aid_s.split("#", 1)[0])
    return None


def _row_reading_key(
    row: dict,
    old_by_id: dict[str, dict],
    new_by_id: dict[str, dict],
) -> tuple[float, float, str]:
    """
    页内阅读序（上→下、左→右）：
    - 有旧框则只用旧侧坐标（与旧教材版式一致，避免新侧页顶读音表拽乱）；
    - 旧无则用新侧；
    - 两侧分表查询，禁止跨侧撞号。
    """
    oid, nid = row.get("old_atom_id"), row.get("new_atom_id")
    atom = _resolve_side_atom(old_by_id, oid) or _resolve_side_atom(new_by_id, nid)
    if atom:
        return (
            _ymid(atom),
            float(atom.get("x_start") or 0),
            str(oid or nid or ""),
        )
    return (999.0, 999.0, str(oid or nid or ""))


def _build_block_rows(
    alignment: dict,
    *,
    old_body_pinyin: dict[str, str] | None = None,
    new_body_pinyin: dict[str, str] | None = None,
    work_old: list[dict] | None = None,
    work_new: list[dict] | None = None,
) -> list[dict]:
    """同块横排：锚定对 + 未匹配，每块自带字符级 ops；最终按教材阅读序排序。"""
    rows: list[dict] = []
    # compare_page_text 会 pop _work_*，调用方显式传入工作集
    if work_old is not None:
        alignment = {**alignment, "_work_old": work_old}
    if work_new is not None:
        alignment = {**alignment, "_work_new": work_new}
    atom_old_by, atom_new_by = _atom_lookup_from_alignment(alignment)
    for a in alignment.get("anchors") or []:
        ot = (a.get("old_text") or "").strip()
        nt = (a.get("new_text") or "").strip()
        # 课标题展示：3→③；任一侧有选学星则两侧补 *
        ot, nt = _unify_lesson_title_pair(ot, nt)
        # 列表方块符一侧漏识时补齐，避免误报「◇ 删除」
        ot, nt = _unify_list_bullet_pair(ot, nt)
        ops = _char_ops(ot, nt, max_items=20)
        change = a.get("change") or _block_verdict_from_ops(ops, old_text=ot, new_text=nt)
        # 分页错位仍保留 ops，供「跨页对齐后仍有差异」说明与高亮
        if change == "一致" and ops:
            change = _block_verdict_from_ops(ops, old_text=ot, new_text=nt)
        # 内容实质相同（含仅角标/课序号形态差）→ 去掉「文字*」ops；分页行除外
        if change != "分页错位" and _strip_for_content(ot) == _strip_for_content(nt):
            ops = [o for o in ops if "标点" in (o.get("kind") or "")]
            change = _block_verdict_from_ops(ops, old_text=ot, new_text=nt)
            if change not in ("一致", "仅标点差异"):
                change = "一致" if not ops else "仅标点差异"
        row = {
            "kind": "pair",
            "old_atom_id": a.get("old_atom_id"),
            "new_atom_id": a.get("new_atom_id"),
            "old_text": ot,
            "new_text": nt,
            "change": change,
            "ops": ops,
            "similarity": a.get("text_similarity")
            if a.get("text_similarity") is not None
            else round(_fuzz_ratio(ot, nt), 3),
            "pagination": bool(a.get("pagination") or change == "分页错位"),
            "pagination_note": a.get("pagination_note") or {},
        }
        row = _enrich_block_row(
            row,
            old_body_pinyin=old_body_pinyin,
            new_body_pinyin=new_body_pinyin,
        )
        if row.get("change") == "分页错位" or row.get("pagination"):
            row = _apply_pagination_notes(row, row.get("pagination_note"))
        rows.append(row)
    for u in alignment.get("unmatched_old") or []:
        ot = (u.get("display_text") or u.get("ocr_text") or u.get("content") or "").strip()
        ot = _canonicalize_lesson_title_display(ot)
        change = "分页错位" if u.get("pagination") or u.get("change") == "分页错位" else "旧有新无"
        row = {
            "kind": "old_only",
            "old_atom_id": u.get("atom_id"),
            "new_atom_id": None,
            "old_text": ot,
            "new_text": "",
            "change": change,
            "ops": [] if change == "分页错位" else _char_ops(ot, "", max_items=8),
            "similarity": 0.0,
            "pagination": change == "分页错位",
            "pagination_note": u.get("pagination_note")
            or ({"received": ot} if change == "分页错位" else {}),
        }
        row = _enrich_block_row(
            row,
            old_body_pinyin=old_body_pinyin,
            new_body_pinyin=new_body_pinyin,
        )
        if row.get("change") == "分页错位":
            row = _apply_pagination_notes(row, row.get("pagination_note"))
        rows.append(row)
    for u in alignment.get("unmatched_new") or []:
        nt = (u.get("display_text") or u.get("ocr_text") or u.get("content") or "").strip()
        nt = _canonicalize_lesson_title_display(nt)
        change = "分页错位" if u.get("pagination") or u.get("change") == "分页错位" else "新有旧无"
        row = {
            "kind": "new_only",
            "old_atom_id": None,
            "new_atom_id": u.get("atom_id"),
            "old_text": "",
            "new_text": nt,
            "change": change,
            "ops": [] if change == "分页错位" else _char_ops("", nt, max_items=8),
            "similarity": 0.0,
            "pagination": change == "分页错位",
            "pagination_note": u.get("pagination_note")
            or ({"received": nt} if change == "分页错位" else {}),
        }
        row = _enrich_block_row(
            row,
            old_body_pinyin=old_body_pinyin,
            new_body_pinyin=new_body_pinyin,
        )
        if row.get("change") == "分页错位":
            row = _apply_pagination_notes(row, row.get("pagination_note"))
        rows.append(row)
    rows.sort(key=lambda r: _row_reading_key(r, atom_old_by, atom_new_by))
    return rows


def _clip_note_snippet(text: str, *, limit: int = 36) -> str:
    t = re.sub(r"\s+", "", text or "").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


def _ops_content_diff_bits(ops: list[dict] | None, *, limit: int = 4) -> list[str]:
    """从字符 ops 抽出删/增/改短句（忽略纯标点）。"""
    bits: list[str] = []
    for op in ops or []:
        kind = str(op.get("kind") or "")
        if "标点" in kind:
            continue
        old_c = _clip_note_snippet(str(op.get("old") or ""), limit=18)
        new_c = _clip_note_snippet(str(op.get("new") or ""), limit=18)
        if kind == "文字删除" and old_c:
            bits.append(f"删「{old_c}」")
        elif kind == "文字新增" and new_c:
            bits.append(f"增「{new_c}」")
        elif kind == "文字改写" and (old_c or new_c):
            bits.append(f"改「{old_c}」→「{new_c}」")
        if len(bits) >= limit:
            break
    return bits


def _pagination_note_lines(
    *,
    continued: str = "",
    received: str = "",
    neighbor_missing: bool = False,
    content_bits: list[str] | None = None,
) -> list[str]:
    """教研可读的分页错位说明（含跨页对齐后删增改结论）。"""
    lines = ["分页错位"]
    if continued:
        lines.append(f"续到下页：「{_clip_note_snippet(continued)}」")
    if received:
        lines.append(f"承接上页：「{_clip_note_snippet(received)}」")
    if neighbor_missing:
        lines.append("邻页未 OCR 或未加载，疑似跨页续写（请先跑下页①）")
    bits = [b for b in (content_bits or []) if b]
    if bits:
        lines.append("跨页对齐后仍有差异：" + "；".join(bits))
    else:
        lines.append("跨页对齐后：无删增改")
    lines.append("版式分页不同，非正文改写")
    return lines


def _apply_pagination_notes(row: dict, note: dict | None = None) -> dict:
    note = note or row.get("pagination_note") or {}
    if row.get("change") != "分页错位" and not row.get("pagination"):
        return row
    row["change"] = "分页错位"
    row["pagination"] = True
    ot = (row.get("old_text") or "").strip()
    nt = (row.get("new_text") or "").strip()
    # 对齐后的真文字差（页顶残段 vs 合成侧常实质相同 → 无删增改）
    content_ops = [
        o
        for o in _char_ops(ot, nt, max_items=20)
        if "标点" not in str(o.get("kind") or "")
    ]
    if _strip_for_content(ot) == _strip_for_content(nt):
        content_ops = []
    content_bits = _ops_content_diff_bits(content_ops)
    continued = str(note.get("continued") or "")
    received = str(note.get("received") or "")
    neighbor_missing = bool(note.get("neighbor_missing"))
    # 页顶残段默认承接上页
    if not continued and not received and not neighbor_missing:
        frag = (ot or nt).strip()
        if frag and len(_strip_for_content(frag)) <= 28:
            received = frag
    lines = _pagination_note_lines(
        continued=continued,
        received=received,
        neighbor_missing=neighbor_missing,
        content_bits=content_bits,
    )
    row["change_summary_lines"] = lines
    row["change_summary"] = "；".join(lines)
    # 有真差异时保留 ops 高亮；无差异则清空避免误红
    row["ops"] = content_ops if content_bits else []
    return row


def _content_prefix_tail_diff(ot: str, nt: str) -> tuple[str, str]:
    """若一侧是另一侧前缀，返回 (短侧, 长侧多出的尾部)。否则 ("", "")。"""
    oc = _strip_for_content(ot)
    nc = _strip_for_content(nt)
    if not oc or not nc or oc == nc:
        return "", ""
    if oc.startswith(nc) and len(oc) > len(nc) + 3:
        return nt, ot[len(nt) :] if ot.startswith(nt) else oc[len(nc) :]
    if nc.startswith(oc) and len(nc) > len(oc) + 3:
        return ot, nt[len(ot) :] if nt.startswith(ot) else nc[len(oc) :]
    # 模糊：用 strip 对齐
    if nc and oc.startswith(nc):
        return nt, oc[len(nc) :]
    if oc and nc.startswith(oc):
        return ot, nc[len(oc) :]
    return "", ""


def _looks_like_cross_page_tail_diff(ot: str, nt: str) -> bool:
    """未收句且一侧是另一侧前缀 → 很像跨页断点不同。"""
    if not (_is_incomplete_paragraph_end(ot) or _is_incomplete_paragraph_end(nt)):
        return False
    _short, tail = _content_prefix_tail_diff(ot, nt)
    if not tail or len(_strip_for_content(tail)) < 4:
        return False
    # 尾部也不应是完整独立段（以句号收束的长段更像真删）
    tail_s = (tail or "").strip()
    if len(_strip_for_content(tail_s)) >= 40 and tail_s.endswith(("。", "！", "？")):
        return False
    return True


def _neighbor_plain_texts(atoms: list[dict] | None) -> list[str]:
    return [_atom_plain(a) for a in _filter_text_atoms(list(atoms or [])) if _atom_plain(a)]


def _looks_like_heading_line(text: str) -> bool:
    """课标题/单元题等：无句读短行，绝非页末未完句。"""
    plain = re.sub(r"\s+", "", text or "")
    if not plain or len(plain) > 48:
        return False
    if any(ch in plain for ch in "。！？；"):
        return False
    # 3*现代诗二首 / 第2课 / 第一单元…
    if re.match(r"^[\d０-９\*＊·•]+", plain):
        return True
    if re.search(r"(现代诗|古诗|课文|单元|活动)", plain) and "，" not in plain:
        return True
    return False


def _is_page_bottom_atom(atom: dict | None) -> bool:
    """页最尾段附近（用于「续到下页」）。"""
    if not atom:
        return False
    y0 = float(atom.get("y_start") or 0)
    y1 = float(atom.get("y_end") or 0)
    return y0 >= 0.55 or y1 >= 0.70


def _is_incomplete_paragraph_end(text: str) -> bool:
    """本页段末未收句（无句末标点），通常续到下一页。"""
    if _looks_like_heading_line(text):
        return False
    # 词语表/读音表/写字表以汉字收尾，不是未完句
    if (
        _looks_like_word_bank(text)
        or _looks_like_reading_list(text)
        or _looks_like_writing_grid(text)
    ):
        return False
    t = (text or "").rstrip()
    if len(_strip_for_content(t)) < 4:
        return False
    # 去掉收尾引号后再判句末
    while t and t[-1] in "\"'」』”’）)":
        t = t[:-1].rstrip()
    if not t:
        return False
    if t[-1] in "。！？…":
        return False
    # 逗号/顿号/冒号或汉字收尾 → 未完
    if t[-1] in "，、；：" or re.search(r"[\u4e00-\u9fff0-9a-zA-Z]$", t):
        return True
    return False


def _looks_like_paragraph_continuation(prev: str, lead: str) -> bool:
    """规则：lead 是否像 prev 未完句的续写（排除词表/练习/资料袋）。"""
    lead_s = (lead or "").strip()
    if not lead_s:
        return False
    if (
        _looks_like_reading_list(lead_s)
        or _looks_like_writing_grid(lead_s)
        or _looks_like_word_bank(lead_s)
        or _looks_like_word_bank(prev)
    ):
        return False
    if _looks_like_heading_line(lead_s) or _looks_like_heading_line(prev):
        return False
    head = re.sub(r"\s+", "", lead_s)[:16]
    ban_prefixes = (
        "有感情地",
        "小练笔",
        "资料袋",
        "选做",
        "选一个",
        "选一选",
        "读一读",
        "读下面",
        "写一写",
        "写下来",
        "和同学",
        "体会",
        "泡泡",
        "单元",
        "第一",
        "第二",
        "第三",
        "思考",
        "练习",
        "现代诗",
        "用上",
    )
    if any(head.startswith(p) for p in ban_prefixes):
        return False
    if re.match(r"^[①②③④⑤⑥⑦⑧⑨⑩１２３４５６７８９０\d]+[.、．]", lead_s):
        return False
    joined = f"{(prev or '').rstrip()}{lead_s}"
    # 拼上后能收句 → 强续段
    if _is_incomplete_paragraph_end(prev) and not _is_incomplete_paragraph_end(joined):
        return True
    # 短残尾（如「母亲的怀里了。」）——须像收束碎片，禁止把整条练习指令当续段
    lead_c = _strip_for_content(lead_s)
    if len(lead_c) <= 14:
        return True
    if (
        len(lead_c) <= 24
        and lead_s.rstrip().endswith(("。", "！", "？", "…"))
        and "，" not in lead_s
        and "," not in lead_s
    ):
        return True
    return False


def _next_page_lead_plain(next_atoms: list[dict] | None, *, max_atoms: int = 1) -> str:
    """下一页页顶续段（默认仅 1 个原子，避免把读音表/下一段正文误拼进来）。"""
    atoms = _filter_text_atoms(list(next_atoms or []))
    atoms.sort(key=lambda a: (_ymid(a), float(a.get("x_start") or 0)))
    leads: list[dict] = []
    for a in atoms:
        if float(a.get("y_start") or 0) > 0.22:
            continue
        plain = _atom_plain(a)
        if not plain:
            continue
        # 读音表/写字表/词语表/练习指令不是正文续句
        if (
            _looks_like_reading_list(plain)
            or _looks_like_writing_grid(plain)
            or _looks_like_word_bank(plain)
        ):
            continue
        head = re.sub(r"\s+", "", plain)[:12]
        if any(
            head.startswith(p)
            for p in ("选一个", "选做", "读一读", "读下面", "写一写", "和同学", "体会")
        ):
            continue
        leads.append(a)
        if len(leads) >= max_atoms:
            break
    if not leads and atoms:
        # 回退：页顶第一个非词表原子
        for a in atoms:
            plain = _atom_plain(a)
            if (
                plain
                and not _looks_like_reading_list(plain)
                and not _looks_like_writing_grid(plain)
                and not _looks_like_word_bank(plain)
            ):
                leads = [a]
                break
    return "".join(_atom_plain(a) for a in leads)


def _stitch_next_page_continuation(
    text: str,
    next_atoms: list[dict] | None,
    *,
    atom: dict | None = None,
) -> tuple[str, bool]:
    """仅页末未收句正文才拼下页页顶；课标题/词语表/页中短行不拼。"""
    if atom is not None and not _is_page_bottom_atom(atom):
        return text, False
    if (
        _looks_like_word_bank(text)
        or _looks_like_reading_list(text)
        or _looks_like_writing_grid(text)
    ):
        return text, False
    if not _is_incomplete_paragraph_end(text):
        return text, False
    lead = _next_page_lead_plain(next_atoms)
    if not lead or not _looks_like_paragraph_continuation(text, lead):
        return text, False
    # 已含续文则不重复拼
    lead_n = _norm(lead)
    text_n = _norm(text)
    if lead_n and lead_n[: min(8, len(lead_n))] in text_n[-max(24, len(lead_n)) :]:
        return text, False
    return f"{text.rstrip()}{lead}", True


def _raw_cut_for_content_prefix(raw: str, content_prefix_len: int) -> int:
    """把 strip 后的前缀长度近似映射回原文切点。"""
    if content_prefix_len <= 0:
        return 0
    stripped = _strip_for_content(raw)
    if not stripped:
        return 0
    if content_prefix_len >= len(stripped):
        return len(raw)
    ratio = content_prefix_len / max(len(stripped), 1)
    return max(0, min(len(raw), int(round(len(raw) * ratio))))


def _reconcile_stitched_suffix_overlap(ot: str, nt: str) -> tuple[str, str, bool]:
    """
    拼下一页后：较短侧若近似等于较长侧后缀，则裁掉较长侧前缀（跨版分页错位）。
    例：旧段从「我仿佛看见…」起，新段多出页首「的夜是柔和的…」。
    """
    oc = _strip_for_content(ot)
    nc = _strip_for_content(nt)
    if not oc or not nc or oc == nc:
        return ot, nt, False

    if len(oc) <= len(nc):
        short_c, long_c, short_raw, long_raw, short_is_old = oc, nc, ot, nt, True
    else:
        short_c, long_c, short_raw, long_raw, short_is_old = nc, oc, nt, ot, False

    if len(short_c) < 8:
        return ot, nt, False

    exact = long_c.endswith(short_c) or (short_c in long_c and long_c.rfind(short_c) >= len(long_c) - len(short_c) - 2)
    tail = long_c[-len(short_c) :]
    fuzzy_ok = _fuzz_ratio(short_c, tail) >= 0.88
    if not exact and not fuzzy_ok:
        return ot, nt, False

    if exact and long_c.endswith(short_c):
        prefix_len = len(long_c) - len(short_c)
    elif exact and short_c in long_c:
        prefix_len = long_c.rfind(short_c)
    else:
        prefix_len = len(long_c) - len(short_c)

    if prefix_len <= 0:
        return ot, nt, fuzzy_ok  # 仅 OCR 微差（如 者/着）

    cut = _raw_cut_for_content_prefix(long_raw, prefix_len)
    trimmed = long_raw[cut:].lstrip("\n ，,、")
    if not trimmed:
        return ot, nt, False
    if short_is_old:
        return short_raw, trimmed, True
    return trimmed, short_raw, True


def _leftover_covered_by_neighbors(leftover: str, neighbors: list[str]) -> bool:
    """多出的前/后缀是否其实在邻页。"""
    left = _strip_for_content(leftover)
    if len(left) < 4:
        return False
    for neigh in neighbors:
        if not neigh:
            continue
        if _segment_covered_by(neigh, leftover, edge=True):
            return True
        nn = _strip_for_content(neigh)
        if left and (left in nn or nn.endswith(left) or nn.startswith(left)):
            return True
    return False


def _reconcile_pair_pagination(
    ot: str,
    nt: str,
    *,
    prev_old_atoms: list[dict] | None = None,
    prev_new_atoms: list[dict] | None = None,
    next_old_atoms: list[dict] | None = None,
    next_new_atoms: list[dict] | None = None,
) -> tuple[str, str, bool]:
    """
    锚定对中一侧多出跨页续行时，裁掉邻页已有片段再比。
    例：旧框含「我微笑着…现在睡在」+「如今在海上…」，新框只有后者。
    """
    oc = _strip_for_content(ot)
    nc = _strip_for_content(nt)
    if not oc or not nc or oc == nc:
        return ot, nt, False

    neighbors = (
        _neighbor_plain_texts(prev_old_atoms)
        + _neighbor_plain_texts(prev_new_atoms)
        + _neighbor_plain_texts(next_old_atoms)
        + _neighbor_plain_texts(next_new_atoms)
    )
    if not neighbors:
        return ot, nt, False

    # 短边是长边的后缀 → 长边多出前缀（页顶续行并进本段）
    if len(oc) > len(nc) and (oc.endswith(nc) or nc in oc):
        if oc.endswith(nc):
            # 用原文比例切前缀
            ratio = (len(oc) - len(nc)) / max(len(oc), 1)
            cut = max(0, min(len(ot), int(round(len(ot) * ratio)) + 2))
            # 更稳：从原文找 short 对齐
            nt_n, ot_n = _norm(nt), _norm(ot)
            idx = ot_n.find(nt_n) if nt_n else -1
            if idx >= 0:
                cut = int(round(idx / max(len(ot_n), 1) * len(ot)))
            prefix = ot[:cut]
            if _leftover_covered_by_neighbors(prefix, neighbors):
                return ot[cut:].lstrip("\n ，,、"), nt, True
        elif nc in oc and oc.find(nc) > 0:
            idx = oc.find(nc)
            # approximate raw cut
            cut = int(round(idx / max(len(oc), 1) * len(ot)))
            prefix = ot[:cut]
            if _leftover_covered_by_neighbors(prefix, neighbors):
                return ot[cut:].lstrip("\n ，,、"), nt, True

    if len(nc) > len(oc) and (nc.endswith(oc) or oc in nc):
        if nc.endswith(oc):
            ot_n, nt_n = _norm(ot), _norm(nt)
            idx = nt_n.find(ot_n) if ot_n else -1
            cut = int(round(idx / max(len(nt_n), 1) * len(nt))) if idx >= 0 else 0
            prefix = nt[:cut]
            if _leftover_covered_by_neighbors(prefix, neighbors):
                return ot, nt[cut:].lstrip("\n ，,、"), True
        elif oc in nc and nc.find(oc) > 0:
            idx = nc.find(oc)
            cut = int(round(idx / max(len(nc), 1) * len(nt)))
            prefix = nt[:cut]
            if _leftover_covered_by_neighbors(prefix, neighbors):
                return ot, nt[cut:].lstrip("\n ，,、"), True

    # 短边是长边的前缀 → 长边多出后缀（本段尾跨到下页，邻页页顶有续）
    if len(oc) > len(nc) and oc.startswith(nc):
        suffix = ot[int(round(len(nc) / max(len(oc), 1) * len(ot))) :]
        # refine
        ot_n, nt_n = _norm(ot), _norm(nt)
        if ot_n.startswith(nt_n):
            cut = int(round(len(nt_n) / max(len(ot_n), 1) * len(ot)))
            suffix = ot[cut:]
            if _leftover_covered_by_neighbors(suffix, neighbors):
                return ot[:cut].rstrip("\n ，,、"), nt, True

    if len(nc) > len(oc) and nc.startswith(oc):
        ot_n, nt_n = _norm(ot), _norm(nt)
        if nt_n.startswith(ot_n):
            cut = int(round(len(ot_n) / max(len(nt_n), 1) * len(nt)))
            suffix = nt[cut:]
            if _leftover_covered_by_neighbors(suffix, neighbors):
                return ot, nt[:cut].rstrip("\n ，,、"), True

    return ot, nt, False


def compare_page_text(
    old_atoms: list[dict],
    new_atoms: list[dict],
    *,
    old_body_pinyin: dict[str, str] | None = None,
    new_body_pinyin: dict[str, str] | None = None,
    prev_old_atoms: list[dict] | None = None,
    prev_new_atoms: list[dict] | None = None,
    next_old_atoms: list[dict] | None = None,
    next_new_atoms: list[dict] | None = None,
) -> dict:
    """
    语文优先：基于 OCR 文字原子做正文/标点比对。
    含原子锚定结果 + 整页拼接文本的字符级差异 + 同块 block_rows。
    页末未收句时会拼下一页页顶原子再比。
    """
    alignment = align_atoms(
        old_atoms,
        new_atoms,
        prev_old_atoms=prev_old_atoms,
        prev_new_atoms=prev_new_atoms,
    )
    # 优先用比对工作集（含列表拆分/一对多虚拟切片），避免用原始合并框盖掉切片文本
    work_old = alignment.pop("_work_old", None) or old_atoms
    work_new = alignment.pop("_work_new", None) or new_atoms
    absorbed = set(alignment.get("pagination_absorbed_ids") or [])
    old_atom_by_id = {
        a.get("atom_id"): a
        for a in work_old
        if a.get("atom_type") in _TEXT_TYPES and a.get("atom_id")
    }
    new_atom_by_id = {
        a.get("atom_id"): a
        for a in work_new
        if a.get("atom_type") in _TEXT_TYPES and a.get("atom_id")
    }
    old_by_id = {aid: _atom_plain(a) for aid, a in old_atom_by_id.items()}
    new_by_id = {aid: _atom_plain(a) for aid, a in new_atom_by_id.items()}
    for a in alignment.get("anchors") or []:
        oid, nid = a.get("old_atom_id"), a.get("new_atom_id")
        if oid in old_by_id:
            a["old_text"] = old_by_id[oid]
        if nid in new_by_id:
            a["new_text"] = new_by_id[nid]
        if a.get("change") == "分页错位" or a.get("pagination"):
            a["change"] = "分页错位"
            a["text_similarity"] = 1.0
            note = dict(a.get("pagination_note") or {})
            ot0, nt0 = a.get("old_text") or "", a.get("new_text") or ""
            if _looks_like_cross_page_tail_diff(ot0, nt0):
                _s, tail = _content_prefix_tail_diff(ot0, nt0)
                if tail:
                    note["continued"] = tail
                    note.pop("received", None)
                if not (next_old_atoms or next_new_atoms):
                    note["neighbor_missing"] = True
            elif not note.get("received") and not note.get("continued"):
                frag = (ot0 or nt0).strip()
                if frag and len(_strip_for_content(frag)) <= 28:
                    note["received"] = frag
            a["pagination_note"] = note
            continue
        ot, nt = a.get("old_text") or "", a.get("new_text") or ""
        # 实质相同（课标题等同文）→ 一致，禁止拼页后误标分页错位
        if _strip_for_content(ot) == _strip_for_content(nt):
            ops0 = _char_ops(ot, nt, max_items=20)
            a["change"] = _block_verdict_from_ops(ops0, old_text=ot, new_text=nt)
            a["text_similarity"] = round(_fuzz_ratio(ot, nt), 3)
            continue
        old_atom = old_atom_by_id.get(oid) if oid else None
        new_atom = new_atom_by_id.get(nid) if nid else None
        at_page_bottom = _is_page_bottom_atom(old_atom) or _is_page_bottom_atom(new_atom)
        # 页末未收句 → 拼下一页页顶原子后再比
        ot_s, stitched_o = _stitch_next_page_continuation(
            ot, next_old_atoms, atom=old_atom
        )
        nt_s, stitched_n = _stitch_next_page_continuation(
            nt, next_new_atoms, atom=new_atom
        )
        has_next = bool(next_old_atoms or next_new_atoms)
        ot_raw, nt_raw = ot, nt

        def _mark_pagination(*, continued: str = "", neighbor_missing: bool = False) -> None:
            a["change"] = "分页错位"
            a["pagination"] = True
            a["text_similarity"] = 1.0
            note = dict(a.get("pagination_note") or {})
            if continued:
                note["continued"] = continued
            if neighbor_missing:
                note["neighbor_missing"] = True
            a["pagination_note"] = note
            if oid:
                absorbed.add(oid)
            if nid:
                absorbed.add(nid)

        if stitched_o or stitched_n:
            ot, nt = ot_s, nt_s
            a["old_text"] = ot
            a["new_text"] = nt
            a["pagination_stitched"] = True
            # 拼页后按模糊后缀对齐，避免「新教材删除跨页续句」误报
            ot_f, nt_f, overlapped = _reconcile_stitched_suffix_overlap(ot, nt)
            if overlapped and at_page_bottom:
                a["old_text"] = ot_f
                a["new_text"] = nt_f
                cont = ""
                if stitched_n:
                    cont = _next_page_lead_plain(next_new_atoms) or cont
                if stitched_o and not cont:
                    cont = _next_page_lead_plain(next_old_atoms)
                _short, tail = _content_prefix_tail_diff(ot, nt)
                if tail and len(_strip_for_content(tail)) >= 4:
                    cont = tail or cont
                _mark_pagination(continued=cont)
                continue
        ot2, nt2, trimmed = _reconcile_pair_pagination(
            ot,
            nt,
            prev_old_atoms=prev_old_atoms,
            prev_new_atoms=prev_new_atoms,
            next_old_atoms=next_old_atoms,
            next_new_atoms=next_new_atoms,
        )
        if trimmed:
            a["old_text"] = ot2
            a["new_text"] = nt2
            _short, tail = _content_prefix_tail_diff(ot_raw, nt_raw)
            cont = tail
            if not cont and (stitched_o or stitched_n):
                cont = _next_page_lead_plain(next_new_atoms) or _next_page_lead_plain(next_old_atoms)
            _mark_pagination(continued=cont)
            continue
        # 拼页后一致：仅页末才标分页错位；拼前已同文则回退为一致
        if (stitched_o or stitched_n) and _strip_for_content(ot) == _strip_for_content(nt):
            if _strip_for_content(ot_raw) == _strip_for_content(nt_raw) or not at_page_bottom:
                a["old_text"] = ot_raw
                a["new_text"] = nt_raw
                a["change"] = "一致"
                a.pop("pagination_stitched", None)
                a["text_similarity"] = 1.0
                continue
            cont = _next_page_lead_plain(next_new_atoms) or _next_page_lead_plain(next_old_atoms)
            _mark_pagination(continued=cont)
            continue
        a["text_similarity"] = round(_fuzz_ratio(ot, nt), 3)
        ops = _char_ops(ot, nt, max_items=20)
        change = _block_verdict_from_ops(ops, old_text=ot, new_text=nt)
        # 拼页后仍仅剩跨页错位痕迹 → 降为分页错位（仅页末）
        if at_page_bottom and (stitched_o or stitched_n) and change not in ("一致", "无文字"):
            ot3, nt3, trimmed2 = _reconcile_pair_pagination(
                ot,
                nt,
                prev_old_atoms=prev_old_atoms,
                prev_new_atoms=prev_new_atoms,
                next_old_atoms=next_old_atoms,
                next_new_atoms=next_new_atoms,
            )
            if trimmed2 or _fuzz_ratio(ot, nt) >= 0.92:
                a["old_text"] = ot3 if trimmed2 else ot
                a["new_text"] = nt3 if trimmed2 else nt
                cont = _next_page_lead_plain(next_new_atoms) or _next_page_lead_plain(next_old_atoms)
                _mark_pagination(continued=cont)
                continue
        # 邻页缺失：未收句前缀差 → 降为分页提示（仅页末），禁止「删除」误导
        if (
            at_page_bottom
            and change == "文字有差异"
            and not has_next
            and _looks_like_cross_page_tail_diff(ot, nt)
        ):
            _short, tail = _content_prefix_tail_diff(ot, nt)
            _mark_pagination(continued=tail, neighbor_missing=True)
            continue
        # 有邻页但仍像跨页尾差（拼页未命中，仅页末）
        if (
            at_page_bottom
            and change == "文字有差异"
            and has_next
            and _looks_like_cross_page_tail_diff(ot, nt)
        ):
            _short, tail = _content_prefix_tail_diff(ot, nt)
            next_blob = "".join(
                _neighbor_plain_texts(next_old_atoms) + _neighbor_plain_texts(next_new_atoms)
            )
            if tail and _leftover_covered_by_neighbors(tail, [next_blob]):
                _mark_pagination(continued=tail)
                continue
        a["change"] = change

    # 邻页提示在前，本页正文夹注覆盖其后
    old_map = _merge_body_pinyin(old_body_pinyin, _collect_body_pinyin(old_atoms))
    new_map = _merge_body_pinyin(new_body_pinyin, _collect_body_pinyin(new_atoms))
    block_rows = _build_block_rows(
        alignment,
        old_body_pinyin=old_map,
        new_body_pinyin=new_map,
        work_old=work_old,
        work_new=work_new,
    )
    # 整页拼接时去掉已吸收的跨页残段，避免误报「文字有差异」
    old_blob = _join_reading_text(
        [a for a in old_atoms if a.get("atom_id") not in absorbed]
    )
    new_blob = _join_reading_text(
        [a for a in new_atoms if a.get("atom_id") not in absorbed]
    )
    content_same = _strip_for_content(old_blob) == _strip_for_content(new_blob)
    ops = _char_ops(old_blob, new_blob)
    # 引号挪位：去掉「文字」伪差异，保留引号标点 ops
    if _texts_equal_ignoring_quotes(old_blob, new_blob) and not _quotes_soft_equal(
        old_blob, new_blob
    ):
        ops = [o for o in ops if "标点" in o["kind"]]
    punct_ops = [o for o in ops if "标点" in o["kind"]]
    text_ops = [o for o in ops if "标点" not in o["kind"]]
    sim = _fuzz_ratio(old_blob, new_blob)
    if not old_blob and not new_blob:
        verdict = "无文字"
    elif not ops:
        verdict = "一致"
    elif _texts_equal_ignoring_quotes(old_blob, new_blob):
        verdict = (
            "一致" if _quotes_soft_equal(old_blob, new_blob) else "仅标点差异"
        )
    elif not text_ops and punct_ops:
        verdict = "仅标点差异"
    elif content_same and punct_ops:
        verdict = "仅标点差异"
    elif text_ops and _is_pinyin_annotation_diff(old_blob, new_blob):
        verdict = "音标变动注意"
    elif text_ops:
        verdict = "文字有差异"
    else:
        verdict = "有差异"
    soft_ok = ("一致", "无文字", "分页错位")
    soft_or_mild = soft_ok + ("仅标点差异", "音标变动注意")
    changed_blocks = [r for r in block_rows if r.get("change") not in soft_ok]
    # 块级已全部收口时，以块结论为准（避免读音表原始两行 OCR 把整页误判为文字有差异）
    if block_rows and not changed_blocks:
        if any(r.get("change") == "仅标点差异" for r in block_rows):
            verdict = "仅标点差异"
        else:
            verdict = "一致"
            ops, text_ops, punct_ops = [], [], []
    elif changed_blocks and all(
        r.get("change") in soft_or_mild or "标点" in (r.get("change") or "")
        for r in block_rows
    ):
        if any(r.get("change") == "音标变动注意" for r in changed_blocks):
            verdict = "音标变动注意"
        elif any(r.get("change") == "仅标点差异" for r in changed_blocks):
            verdict = "仅标点差异"
    return _sanitize_text_compare_pinyin(
        {
            "verdict": verdict,
            "similarity": round(sim, 3),
            "old_text": old_blob,
            "new_text": new_blob,
            "ops": ops,
            "text_ops": text_ops,
            "punct_ops": punct_ops,
            "block_rows": block_rows,
            "anchors": alignment.get("anchors") or [],
            "changed_anchors": [
                a
                for a in (alignment.get("anchors") or [])
                if a.get("change") not in ("一致", "分页错位")
            ],
            "unmatched_old": alignment.get("unmatched_old") or [],
            "unmatched_new": alignment.get("unmatched_new") or [],
            "summary": {
                **(alignment.get("summary") or {}),
                "op_count": len(ops),
                "text_op_count": len(text_ops),
                "punct_op_count": len(punct_ops),
                "block_count": len(block_rows),
                "changed_blocks": len(changed_blocks),
                "verdict": verdict,
                "similarity": round(sim, 3),
            },
        }
    )


def compare_cached_page_text(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
    force: bool = False,
) -> dict:
    """读取两侧文字 OCR 缓存并比对；结果写入磁盘 + MySQL。"""
    cache_file, entry = _load_pair_entry(
        old_vol,
        new_vol,
        old_page,
        new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    if not entry.get("old_text_ocr_done") or not entry.get("new_text_ocr_done"):
        raise ValueError("请先完成 ① 文字 OCR（两侧）")

    appendix_old, appendix_new = _appendix_flags_for_pair(
        old_vol, new_vol, old_page=old_page, new_page=new_page
    )

    if not force:
        cached_cmp = _valid_text_compare(entry)
        if cached_cmp is not None:
            return {
                "atoms": _build_atoms_payload(
                    entry, appendix_old=appendix_old, appendix_new=appendix_new
                ),
                "compare": cached_cmp,
                "from_cache": True,
            }

    old_raw = entry.get("old_raw") or []
    new_raw = entry.get("new_raw") or []
    old_hints, new_hints = _neighbor_body_pinyin(
        old_vol,
        new_vol,
        old_page,
        new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    prev_old_raw: list[dict] = []
    prev_new_raw: list[dict] = []
    next_old_raw: list[dict] = []
    next_new_raw: list[dict] = []
    if old_page > 1 and new_page > 1:
        _, prev_entry = _load_pair_entry(
            old_vol,
            new_vol,
            old_page - 1,
            new_page - 1,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            auto_recompare=False,
        )
        if prev_entry:
            prev_old_raw = list(prev_entry.get("old_raw") or [])
            prev_new_raw = list(prev_entry.get("new_raw") or [])
    _, next_entry = _load_pair_entry(
        old_vol,
        new_vol,
        old_page + 1,
        new_page + 1,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        auto_recompare=False,
    )
    if next_entry:
        next_old_raw = list(next_entry.get("old_raw") or [])
        next_new_raw = list(next_entry.get("new_raw") or [])
    compare = compare_page_text(
        old_raw,
        new_raw,
        old_body_pinyin=old_hints,
        new_body_pinyin=new_hints,
        prev_old_atoms=prev_old_raw,
        prev_new_atoms=prev_new_raw,
        next_old_atoms=next_old_raw,
        next_new_atoms=next_new_raw,
    )
    _save_text_compare(cache_file, entry, compare)
    _persist_text_compare_to_db(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        compare=compare,
    )
    return {
        "atoms": _build_atoms_payload(
            entry, appendix_old=appendix_old, appendix_new=appendix_new
        ),
        "compare": compare,
        "from_cache": False,
    }


def run_diff_page_ocr(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    side: PageSide,
    ocr_phase: OcrPhase,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> dict:
    """对指定侧（old/new）单页跑一阶段 OCR，另一侧保持缓存不变。"""
    if old_page < 1 or new_page < 1:
        raise ValueError("页码无效")
    if side not in ("old", "new"):
        raise ValueError("side 须为 old 或 new")
    if ocr_phase not in ("text", "images"):
        raise ValueError("ocr_phase 须为 text 或 images")

    cache_file, entry = _load_pair_entry(
        old_vol,
        new_vol,
        old_page,
        new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    entry["old_page"] = old_page
    entry["new_page"] = new_page

    if side == "old":
        if ocr_phase == "text":
            from .yuwen_appendix import volume_page_is_appendix_table

            _log.info("diff page OCR text old p%d", old_page)
            entry["old_raw"] = _extract_page_atoms(
                old_vol,
                old_page,
                source="diff_old",
                ocr_phase="text",
                # 附录页用专用逐行提示，强制刷新以免沿用整表合框缓存
                force_llm_refresh=volume_page_is_appendix_table(old_vol, old_page),
            )
            entry["old_text_ocr_done"] = True
            entry["old_image_ocr_done"] = False
            _clear_text_compare(entry)
            _clear_image_compare(entry)
            _clear_compares_in_db(
                old_vol=old_vol,
                new_vol=new_vol,
                old_page=old_page,
                new_page=new_page,
                new_pdf_source=new_pdf_source,
                preview_blob_id=preview_blob_id,
                clear_text=True,
                clear_image=True,
            )
        else:
            old_text = _text_only(entry.get("old_raw") or [])
            if not entry.get("old_text_ocr_done") and not old_text:
                raise ValueError("请先在旧教材页完成 ① 文字 OCR")
            _log.info("diff page OCR images old p%d", old_page)
            entry["old_raw"] = _extract_page_atoms(
                old_vol,
                old_page,
                source="diff_old",
                ocr_phase="images",
                existing_text_atoms=old_text,
            )
            entry["old_image_ocr_done"] = True
            _clear_image_compare(entry)
            _clear_compares_in_db(
                old_vol=old_vol,
                new_vol=new_vol,
                old_page=old_page,
                new_page=new_page,
                new_pdf_source=new_pdf_source,
                preview_blob_id=preview_blob_id,
                clear_image=True,
            )
    elif ocr_phase == "text":
        from .yuwen_appendix import volume_page_is_appendix_table

        _log.info("diff page OCR text new p%d", new_page)
        entry["new_raw"] = _extract_page_atoms(
            new_vol,
            new_page,
            source="diff_new",
            pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            ocr_phase="text",
            force_llm_refresh=volume_page_is_appendix_table(new_vol, new_page),
        )
        entry["new_text_ocr_done"] = True
        entry["new_image_ocr_done"] = False
        _clear_text_compare(entry)
        _clear_image_compare(entry)
        _clear_compares_in_db(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            clear_text=True,
            clear_image=True,
        )
    else:
        new_text = _text_only(entry.get("new_raw") or [])
        if not entry.get("new_text_ocr_done") and not new_text:
            raise ValueError("请先在新教材页完成 ① 文字 OCR")
        _log.info("diff page OCR images new p%d", new_page)
        entry["new_raw"] = _extract_page_atoms(
            new_vol,
            new_page,
            source="diff_new",
            pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            ocr_phase="images",
            existing_text_atoms=new_text,
        )
        entry["new_image_ocr_done"] = True
        _clear_image_compare(entry)
        _clear_compares_in_db(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            clear_image=True,
        )

    _write_cache_entry(cache_file, entry)

    if side == "old":
        _persist_side_atoms_to_db(
            volume=old_vol,
            page_1=old_page,
            pdf_source="full",
            preview_blob_id=None,
            atoms=entry.get("old_raw") or [],
            text_ocr_done=bool(entry.get("old_text_ocr_done")),
            image_ocr_done=bool(entry.get("old_image_ocr_done")),
        )
    else:
        _persist_side_atoms_to_db(
            volume=new_vol,
            page_1=new_page,
            pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            atoms=entry.get("new_raw") or [],
            text_ocr_done=bool(entry.get("new_text_ocr_done")),
            image_ocr_done=bool(entry.get("new_image_ocr_done")),
        )

    return _build_pair_atoms_payload(
        entry, old_vol=old_vol, new_vol=new_vol, cache_file=cache_file
    )


def run_diff_page_ocr_both(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    ocr_phase: OcrPhase,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> dict:
    """当前页对：旧侧 / 新侧同一阶段 OCR 并行（提取并行，写缓存串行合并）。"""
    from concurrent.futures import ThreadPoolExecutor

    from flask import current_app, has_app_context

    if old_page < 1 or new_page < 1:
        raise ValueError("页码无效")
    if ocr_phase not in ("text", "images"):
        raise ValueError("ocr_phase 须为 text 或 images")

    cache_file, entry = _load_pair_entry(
        old_vol,
        new_vol,
        old_page,
        new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    entry["old_page"] = old_page
    entry["new_page"] = new_page

    old_text_pre = _text_only(entry.get("old_raw") or [])
    new_text_pre = _text_only(entry.get("new_raw") or [])
    if ocr_phase == "images":
        if not entry.get("old_text_ocr_done") and not old_text_pre:
            raise ValueError("请先在旧教材页完成 ① 文字 OCR")
        if not entry.get("new_text_ocr_done") and not new_text_pre:
            raise ValueError("请先在新教材页完成 ① 文字 OCR")

    app = current_app._get_current_object() if has_app_context() else None
    from .yuwen_appendix import volume_page_is_appendix_table

    force_old = ocr_phase == "text" and volume_page_is_appendix_table(old_vol, old_page)
    force_new = ocr_phase == "text" and volume_page_is_appendix_table(new_vol, new_page)

    def _extract_side(side: PageSide) -> list[dict]:
        def _work() -> list[dict]:
            if side == "old":
                _log.info("diff page OCR %s old p%d (parallel)", ocr_phase, old_page)
                return _extract_page_atoms(
                    old_vol,
                    old_page,
                    source="diff_old",
                    ocr_phase=ocr_phase,
                    force_llm_refresh=force_old,
                    existing_text_atoms=old_text_pre if ocr_phase == "images" else None,
                )
            _log.info("diff page OCR %s new p%d (parallel)", ocr_phase, new_page)
            return _extract_page_atoms(
                new_vol,
                new_page,
                source="diff_new",
                pdf_source=new_pdf_source,
                preview_blob_id=preview_blob_id,
                ocr_phase=ocr_phase,
                force_llm_refresh=force_new,
                existing_text_atoms=new_text_pre if ocr_phase == "images" else None,
            )

        if app is not None:
            with app.app_context():
                return _work()
        return _work()

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="diff-ocr-side") as pool:
        fut_old = pool.submit(_extract_side, "old")
        fut_new = pool.submit(_extract_side, "new")
        old_atoms = fut_old.result()
        new_atoms = fut_new.result()

    if ocr_phase == "text":
        entry["old_raw"] = old_atoms
        entry["old_text_ocr_done"] = True
        entry["old_image_ocr_done"] = False
        entry["new_raw"] = new_atoms
        entry["new_text_ocr_done"] = True
        entry["new_image_ocr_done"] = False
        _clear_text_compare(entry)
        _clear_image_compare(entry)
        _clear_compares_in_db(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            clear_text=True,
            clear_image=True,
        )
    else:
        entry["old_raw"] = old_atoms
        entry["old_image_ocr_done"] = True
        entry["new_raw"] = new_atoms
        entry["new_image_ocr_done"] = True
        _clear_image_compare(entry)
        _clear_compares_in_db(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
            clear_image=True,
        )

    _write_cache_entry(cache_file, entry)
    _persist_side_atoms_to_db(
        volume=old_vol,
        page_1=old_page,
        pdf_source="full",
        preview_blob_id=None,
        atoms=entry.get("old_raw") or [],
        text_ocr_done=bool(entry.get("old_text_ocr_done")),
        image_ocr_done=bool(entry.get("old_image_ocr_done")),
    )
    _persist_side_atoms_to_db(
        volume=new_vol,
        page_1=new_page,
        pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        atoms=entry.get("new_raw") or [],
        text_ocr_done=bool(entry.get("new_text_ocr_done")),
        image_ocr_done=bool(entry.get("new_image_ocr_done")),
    )
    # 追加写 xiaoke side cache，使教材库 build_library_lesson_blocks() 能导入
    try:
        from .xiaoke_ocr import save_side_atom_cache

        save_side_atom_cache(
            old_vol,
            old_page,
            atoms=entry.get("old_raw") or [],
            text_ocr_done=bool(entry.get("old_text_ocr_done")),
            image_ocr_done=bool(entry.get("old_image_ocr_done")),
        )
        save_side_atom_cache(
            new_vol,
            new_page,
            atoms=entry.get("new_raw") or [],
            text_ocr_done=bool(entry.get("new_text_ocr_done")),
            image_ocr_done=bool(entry.get("new_image_ocr_done")),
        )
    except Exception:
        _log.debug("side cache 写入失败（非致命）", exc_info=True)
    return _build_pair_atoms_payload(
        entry, old_vol=old_vol, new_vol=new_vol, cache_file=cache_file
    )


def build_page_atom_compare(
    *,
    old_vol: Volume,
    new_vol: Volume,
    old_page: int,
    new_page: int,
    force_refresh: bool = False,
    new_pdf_source: PdfSource = "full",
    preview_blob_id: str | None = None,
) -> dict:
    """读取缓存；force_refresh 时依次跑文字+插图 OCR（无锚定）。"""
    if old_page < 1 or new_page < 1:
        raise ValueError("页码无效")
    if force_refresh:
        for side in ("old", "new"):
            run_diff_page_ocr(
                old_vol=old_vol,
                new_vol=new_vol,
                old_page=old_page,
                new_page=new_page,
                side=side,
                ocr_phase="text",
                new_pdf_source=new_pdf_source,
                preview_blob_id=preview_blob_id,
            )
        for side in ("old", "new"):
            run_diff_page_ocr(
                old_vol=old_vol,
                new_vol=new_vol,
                old_page=old_page,
                new_page=new_page,
                side=side,
                ocr_phase="images",
                new_pdf_source=new_pdf_source,
                preview_blob_id=preview_blob_id,
            )
        cached = load_page_atom_cache(
            old_vol=old_vol,
            new_vol=new_vol,
            old_page=old_page,
            new_page=new_page,
            new_pdf_source=new_pdf_source,
            preview_blob_id=preview_blob_id,
        )
        return cached or _empty_atoms_payload(old_page=old_page, new_page=new_page)
    cached = load_page_atom_cache(
        old_vol=old_vol,
        new_vol=new_vol,
        old_page=old_page,
        new_page=new_page,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
    )
    if cached:
        return cached
    return _empty_atoms_payload(old_page=old_page, new_page=new_page)
