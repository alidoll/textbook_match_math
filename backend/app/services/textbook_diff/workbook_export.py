"""本册建设：整册对比结果导出 Excel（总表 + 按单元分表）。"""
from __future__ import annotations

import re
import zipfile
from collections import OrderedDict
from io import BytesIO
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ...models import Volume
from .compare_export import _image_change_label, _image_rows, _ops_summary
from .lesson_pipeline import list_lesson_page_steps
from .volumes import get_diff_volume_by_code
from .workbook import (
    _load_compares_by_new_page,
    list_stored_pairs,
)

# 单元表单页文字变动明细列上限
_MAX_DETAIL_COLS = 24
_SOFT_BLOCK_CHANGES = frozenset({"一致", "无文字", "没变化", "分页错位"})
_QUOTE_RE = re.compile(r"「([^」]+)」")
# openpyxl 写入含换行的 <t> 时常缺少 xml:space="preserve"
_T_TAG_RE = re.compile(r"<t([^>]*)>(.*?)</t>", flags=re.DOTALL)

_HEADER_FILL = PatternFill("solid", fgColor="BE185D")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_TITLE_FONT = Font(bold=True, size=14, color="9D174D")
_META_FONT = Font(size=10, color="64748B")
_UNIT_FILL = PatternFill("solid", fgColor="FDF2F8")
_LESSON_FILL = PatternFill("solid", fgColor="FFF7ED")
_SPACER_FILL = PatternFill("solid", fgColor="F8FAFC")
_SOFT_FILL = PatternFill("solid", fgColor="ECFDF5")
_DIFF_FILL = PatternFill("solid", fgColor="FCE7F3")
_PINYIN_FILL = PatternFill("solid", fgColor="FFEDD5")
_CELL_BORDER = Border(
    left=Side(style="medium", color="000000"),
    right=Side(style="medium", color="000000"),
    top=Side(style="medium", color="000000"),
    bottom=Side(style="medium", color="000000"),
)
_WRAP_TOP = Alignment(wrap_text=True, vertical="top")
_CENTER = Alignment(wrap_text=True, vertical="center", horizontal="center")

_PAIR_STATUS_LABEL = {
    "suggested": "待对比",
    "pending_review": "待确认",
    "confirmed": "已确认",
    "rejected": "无对应",
}


def _safe_sheet_name(title: str, used: set[str]) -> str:
    raw = re.sub(r'[\\/*?:\[\]]', "_", (title or "").strip()) or "未分单元"
    name = raw[:31]
    base = name
    i = 2
    while name in used:
        suffix = f"_{i}"
        name = (base[: max(1, 31 - len(suffix))] + suffix)[:31]
        i += 1
    used.add(name)
    return name


def _text_summary_blob(tc: dict[str, Any] | None) -> str:
    """文字对比总结：仅结论 / 相似度 / 差异块数。"""
    if not tc:
        return "（尚未文字比对）"
    verdict = (tc.get("verdict") or "—").strip()
    lines = [f"【{verdict}】"]
    sim = tc.get("similarity")
    if sim is not None:
        try:
            lines.append(f"相似度 {float(sim):.3f}")
        except (TypeError, ValueError):
            pass
    summary = tc.get("summary") if isinstance(tc.get("summary"), dict) else {}
    changed = summary.get("changed_blocks")
    total = summary.get("block_count")
    if changed is None:
        changed = len(_changed_block_rows(tc))
    if total is None:
        total = len(tc.get("block_rows") or [])
    lines.append(f"差异块 {changed} / {total}")
    return "\r\n".join(lines)


def _changed_block_rows(tc: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not tc:
        return []
    out: list[dict[str, Any]] = []
    for row in tc.get("block_rows") or []:
        if not isinstance(row, dict):
            continue
        change = (row.get("change") or "").strip()
        if change in _SOFT_BLOCK_CHANGES:
            continue
        out.append(row)
    return out


def _keywords_from_block(row: dict[str, Any]) -> list[str]:
    """从 ops / 明细文案提取可高亮的变动字词（长词优先）。"""
    found: list[str] = []
    for op in row.get("ops") or []:
        if not isinstance(op, dict):
            continue
        for key in ("old", "new"):
            s = (op.get(key) or "").strip()
            # 过长片段不适合作关键词高亮
            if s and 1 <= len(s) <= 48:
                found.append(s)
    detail = (row.get("change_summary") or "").strip() or _ops_summary(row.get("ops"))
    for m in _QUOTE_RE.finditer(detail or ""):
        s = (m.group(1) or "").strip()
        if s and 1 <= len(s) <= 48:
            found.append(s)
    # 去重，长词优先，便于匹配
    uniq: list[str] = []
    seen: set[str] = set()
    for s in sorted(found, key=len, reverse=True):
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq


def _highlight_spans(text: str, keywords: list[str]) -> list[tuple[int, int]]:
    """返回不重叠的高亮区间 [start, end)。"""
    if not text or not keywords:
        return []
    spans: list[tuple[int, int]] = []
    for kw in keywords:
        if not kw:
            continue
        start = 0
        while True:
            i = text.find(kw, start)
            if i < 0:
                break
            spans.append((i, i + len(kw)))
            start = i + max(1, len(kw))
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _mark_keywords_plain(text: str, keywords: list[str]) -> str:
    """纯文本里标出变动字词（【…】）。不用单元格富文本，避免部分 Excel/WPS 吃掉换行。"""
    spans = _highlight_spans(text, keywords)
    if not spans:
        return text
    parts: list[str] = []
    cur = 0
    for s, e in spans:
        if cur < s:
            parts.append(text[cur:s])
        frag = text[s:e]
        # 已被书名号/方括号包住则不再套一层
        if frag.startswith("【") or frag.startswith("「"):
            parts.append(frag)
        else:
            parts.append(f"【{frag}】")
        cur = e
    if cur < len(text):
        parts.append(text[cur:])
    return "".join(parts)


def _prepare_detail_texts(
    row: dict[str, Any] | None,
) -> tuple[str, str, str, str]:
    """返回 (change, old_text, new_text, detail)；无块时 detail 为占位说明。"""
    if not row:
        return "", "—", "—", "（本页无文字差异块）"
    change = (row.get("change") or "有差异").strip() or "有差异"
    old_t = (row.get("old_text") or "").strip()
    new_t = (row.get("new_text") or "").strip()
    detail = (row.get("change_summary") or "").strip() or _ops_summary(row.get("ops"))
    keywords = _keywords_from_block(row)

    old_t = old_t[:400]
    new_t = new_t[:400]
    detail = detail[:400]
    if keywords:
        old_t = _mark_keywords_plain(old_t, keywords)
        new_t = _mark_keywords_plain(new_t, keywords)
        detail = _mark_keywords_plain(detail, keywords)
    return change, old_t or "—", new_t or "—", detail or "—"


def _detail_cell_value(row: dict[str, Any]) -> str:
    """单列变动明细：结论 / 旧 / 新 / 明细 各占一行（硬换行）。普通单元表用。"""
    change, old_t, new_t, detail = _prepare_detail_texts(row)
    lines = [f"· {change}"] if change else []
    if old_t and old_t != "—":
        lines.append(f"旧：{old_t}")
    if new_t and new_t != "—":
        lines.append(f"新：{new_t}")
    if detail and detail != "—":
        lines.append(f"明细：{detail}")
    if not lines:
        return detail if detail else "—"
    return "\r\n".join(lines)


def _detail_split_columns(row: dict[str, Any] | None) -> tuple[str, str, str]:
    """附录表用：拆成（旧内容, 新内容, 变动明细），纯文本、无「旧：/新：」前缀。"""
    change, old_t, new_t, detail = _prepare_detail_texts(row)
    if not change and detail == "（本页无文字差异块）":
        return "—", "—", detail
    # 变动明细：结论 + 增删改说明（不含旧/新正文）
    bits: list[str] = []
    if change:
        bits.append(f"· {change}")
    if detail and detail not in ("—",):
        bits.append(detail)
    return old_t, new_t, "\r\n".join(bits) if bits else "—"


def _ensure_xlsx_preserve_cell_newlines(data: bytes) -> bytes:
    """给含换行的 <t> 补上 xml:space=\"preserve\"，否则 Excel/WPS 会把换行折叠掉。"""

    def _fix_t(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        inner = match.group(2) or ""
        if "\n" not in inner and "\r" not in inner:
            return match.group(0)
        if "xml:space" in attrs:
            return match.group(0)
        return f'<t{attrs} xml:space="preserve">{inner}</t>'

    src = zipfile.ZipFile(BytesIO(data), "r")
    out_buf = BytesIO()
    with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            raw = src.read(info.filename)
            name = info.filename or ""
            if name.startswith("xl/") and name.endswith(".xml"):
                text = raw.decode("utf-8")
                raw = _T_TAG_RE.sub(_fix_t, text).encode("utf-8")
            dst.writestr(info, raw)
    return out_buf.getvalue()


def _image_column_blob(ic: dict[str, Any] | None) -> str:
    if not ic:
        return "（尚未图片比对）"
    verdict = (ic.get("verdict") or "—").strip()
    lines = [f"【{verdict}】"]
    summary = ic.get("summary") if isinstance(ic.get("summary"), dict) else {}
    if summary.get("changed_count") is not None:
        lines.append(f"变动插图 {summary.get('changed_count')}")
    layout = ic.get("layout") if isinstance(ic.get("layout"), dict) else {}
    if layout.get("verdict"):
        lines.append(f"版面：{layout.get('verdict')}")
    caps = ic.get("captions") if isinstance(ic.get("captions"), dict) else {}
    if caps.get("verdict"):
        lines.append(f"图义：{caps.get('verdict')}")
    visuals = ic.get("visuals") if isinstance(ic.get("visuals"), dict) else {}
    if visuals.get("verdict"):
        lines.append(f"画面：{visuals.get('verdict')}")

    for row in _image_rows(ic)[:12]:
        label = _image_change_label(row)
        if label in ("没变化", "版面对齐"):
            continue
        old_theme = (row.get("old_theme") or "").strip()
        new_theme = (row.get("new_theme") or "").strip()
        bit = f"· {label}"
        if old_theme or new_theme:
            bit += f"：{old_theme or '—'} → {new_theme or '—'}"
        lines.append(bit)
    return "\r\n".join(lines)


def _fill_for_text_verdict(verdict: str | None) -> PatternFill | None:
    v = (verdict or "").strip()
    if not v or v in ("一致", "无文字", "没变化"):
        return _SOFT_FILL
    if v == "仅标点差异":
        return PatternFill("solid", fgColor="FEF3C7")
    if v == "音标变动注意":
        return _PINYIN_FILL
    if "差异" in v or "有" in v:
        return _DIFF_FILL
    return None


def _style_header_row(ws, row: int, cols: int) -> None:
    for c in range(1, cols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
        cell.border = _CELL_BORDER


def _set_widths(ws, widths: list[float]) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _collect_unit_groups(items: list[dict[str, Any]]) -> OrderedDict[str, list[dict[str, Any]]]:
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for it in items:
        n = it.get("new") or {}
        unit = (n.get("unit_title") or "").strip() or "未分单元"
        groups.setdefault(unit, []).append(it)
    return groups


def _is_appendix_export_item(item: dict[str, Any]) -> bool:
    """识字表/写字表/词语表（或单元=附录）单独出 sheet，不进普通单元表。"""
    from .yuwen_appendix import YUWEN_APPENDIX_UNIT, is_yuwen_appendix_table

    n = item.get("new") or {}
    unit = (n.get("unit_title") or "").strip()
    name = (n.get("lesson_name") or "").strip()
    label = _lesson_label_from_brief(n)
    if unit == YUWEN_APPENDIX_UNIT:
        return True
    return bool(
        is_yuwen_appendix_table(name)
        or is_yuwen_appendix_table(label)
        or is_yuwen_appendix_table((n.get("lesson_no") or "").strip())
    )


def _appendix_sheet_title(item: dict[str, Any]) -> str:
    from .yuwen_appendix import normalize_appendix_table_name

    n = item.get("new") or {}
    label = _lesson_label_from_brief(n)
    return (
        normalize_appendix_table_name(label)
        or normalize_appendix_table_name((n.get("lesson_name") or "").strip())
        or label
        or "附录"
    )


def _page_status_text_only(tc: dict[str, Any] | None) -> str:
    """附录无插图：页状态只看文字比对。"""
    if not tc:
        return "尚未比对"
    tv = (tc.get("verdict") or "").strip()
    if tv in ("一致", "无文字"):
        return "无实质变动"
    if tv == "仅标点差异":
        return "仅标点/软差异"
    if tv == "音标变动注意":
        return "音标变动注意"
    if tv:
        return "有变动"
    return "尚未比对"


def _page_pairs_for_item(
    *,
    old_vol: Volume,
    new_vol: Volume,
    item: dict[str, Any],
) -> list[dict[str, Any]]:
    n = item.get("new") or {}
    uid = (n.get("lesson_uid") or "").strip()
    if not uid or not (item.get("old") or {}).get("lesson_uid"):
        return []
    try:
        return list_lesson_page_steps(
            old_vol=old_vol,
            new_vol=new_vol,
            new_lesson_uid=uid,
        )
    except ValueError:
        # 无页码时至少导出课信息占位
        ps = int(n.get("page_start") or 0)
        pe = int(n.get("page_end") or ps or 0)
        o = item.get("old") or {}
        ops = int(o.get("page_start") or 0)
        if not ps:
            return []
        out = []
        for i, np in enumerate(range(ps, pe + 1)):
            out.append(
                {
                    "old_page": (ops + i) if ops else None,
                    "new_page": np,
                    "label": f"{_lesson_label_from_brief(n)} · p{np}",
                }
            )
        return out


def _lesson_label_from_brief(n: dict[str, Any]) -> str:
    return (
        (n.get("lesson_label") or "").strip()
        or f"{n.get('lesson_no') or ''} {n.get('lesson_name') or ''}".strip()
        or n.get("lesson_uid")
        or "未命名课时"
    )


def _write_summary_sheet(
    wb: openpyxl.Workbook,
    *,
    old_code: str,
    new_code: str,
    coarse: dict[str, Any],
    items: list[dict[str, Any]],
) -> None:
    ws = wb.active
    ws.title = "总表"
    overview = coarse.get("chapter_overview") or {}
    ws["A1"] = "整册对比结果总览"
    ws["A1"].font = _TITLE_FONT
    ws.merge_cells("A1:H1")
    ws["A2"] = (
        f"旧册 {old_code}  ↔  新册 {new_code}  ·  "
        f"{overview.get('changed_chapter_count') or 0}/{overview.get('chapter_count') or 0} 章有变动  ·  "
        f"变动 {overview.get('pages_changed') or 0} 页  ·  "
        f"文字差异块 {overview.get('text_changed_blocks') or 0}"
        + (
            f"  ·  音标变动注意 {overview.get('pinyin_attention_pages') or 0} 页"
            if overview.get("pinyin_attention_pages")
            else ""
        )
    )
    ws["A2"].font = _META_FONT
    ws.merge_cells("A2:H2")

    headers = [
        "单元",
        "课时",
        "旧教材对应",
        "状态",
        "比对页数",
        "变动页数",
        "文字差异块",
        "音标变动页",
        "有实质变动",
    ]
    header_row = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=header_row, column=i, value=h)
    _style_header_row(ws, header_row, len(headers))

    r = header_row + 1
    for it in items:
        n = it.get("new") or {}
        o = it.get("old") or {}
        cs = it.get("change_stats") or {}
        st = it.get("pair_status") or "suggested"
        has = bool(cs.get("has_change"))
        row_vals = [
            (n.get("unit_title") or "").strip() or "未分单元",
            _lesson_label_from_brief(n),
            _lesson_label_from_brief(o) if o else "—",
            _PAIR_STATUS_LABEL.get(st, st),
            f"{cs.get('pages_compared') or 0}/{cs.get('pages_total') or 0}",
            cs.get("pages_changed") or 0,
            cs.get("text_changed_blocks") or 0,
            cs.get("pinyin_attention_pages") or 0,
            "是" if has else "否",
        ]
        for c, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.alignment = _WRAP_TOP
            cell.border = _CELL_BORDER
            if has and c == 9:
                cell.fill = _DIFF_FILL
            elif cs.get("has_pinyin_attention") and c == 8:
                cell.fill = _PINYIN_FILL
        r += 1

    _set_widths(ws, [14, 28, 28, 10, 12, 10, 12, 12, 12])
    ws.freeze_panes = "A5"
    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 18


def _page_status(
    tc: dict[str, Any] | None,
    ic: dict[str, Any] | None,
) -> str:
    tv = (tc or {}).get("verdict") if tc else ""
    iv = (ic or {}).get("verdict") if ic else ""
    if not tc and not ic:
        return "尚未比对"
    if tv in ("一致", "无文字", "仅标点差异") and (
        not iv or iv in ("一致", "没变化", "无插图", "插图一致", "版面一致")
    ):
        return "无实质变动" if tv != "仅标点差异" else "仅标点/软差异"
    if tv == "音标变动注意" and (
        not iv or iv in ("一致", "没变化", "无插图", "插图一致", "版面一致")
    ):
        return "音标变动注意"
    return "有变动"


def _write_unit_sheet(
    wb: openpyxl.Workbook,
    *,
    sheet_name: str,
    unit_title: str,
    old_vol: Volume,
    new_vol: Volume,
    items: list[dict[str, Any]],
    compares: dict[int, dict[str, Any]],
) -> None:
    """
    单元表列序：课时 | 旧页 | 新页 | 页状态 | 图片对比 | 文字对比(总结)
    | 变动1…N（每个文字差异块单独一列，变动字词加粗斜体）。
    """
    # 先收集行数据，确定明细列数
    prepared: list[dict[str, Any]] = []
    max_details = 0
    for it in items:
        n = it.get("new") or {}
        lesson_name = _lesson_label_from_brief(n)
        steps = _page_pairs_for_item(old_vol=old_vol, new_vol=new_vol, item=it)
        if not steps:
            prepared.append(
                {
                    "kind": "empty_lesson",
                    "lesson_name": lesson_name,
                }
            )
            continue
        page_rows: list[dict[str, Any]] = []
        for si, step in enumerate(steps):
            new_page = int(step.get("new_page") or 0)
            old_page = step.get("old_page")
            cmp = compares.get(new_page) or {}
            tc = cmp.get("text_compare") if isinstance(cmp.get("text_compare"), dict) else None
            ic = cmp.get("image_compare") if isinstance(cmp.get("image_compare"), dict) else None
            details = _changed_block_rows(tc)[:_MAX_DETAIL_COLS]
            max_details = max(max_details, len(details))
            page_rows.append(
                {
                    "lesson_name": lesson_name if si == 0 else "",
                    "old_page": old_page if old_page is not None else "—",
                    "new_page": new_page or "—",
                    "page_st": _page_status(tc, ic),
                    "image_blob": _image_column_blob(ic),
                    "text_summary": _text_summary_blob(tc),
                    "details": details,
                    "text_verdict": (tc or {}).get("verdict") if tc else "",
                }
            )
        prepared.append({"kind": "lesson", "pages": page_rows})

    fixed_headers = ["课时", "旧页", "新页", "页状态", "图片对比", "文字对比"]
    detail_headers = [f"变动{i}" for i in range(1, max_details + 1)]
    headers = fixed_headers + detail_headers
    ncols = len(headers)

    ws = wb.create_sheet(sheet_name)
    ws["A1"] = f"单元对比 · {unit_title}"
    ws["A1"].font = _TITLE_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(6, ncols))
    ws["A2"] = (
        "每一行为新旧对应页：图片对比 → 文字对比总结 → 各文字变动分列；"
        "变动字词尽量加粗斜体。课与课、页与页之间空一行分隔。"
    )
    ws["A2"].font = _META_FONT
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=max(6, ncols))

    header_row = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=header_row, column=i, value=h)
    _style_header_row(ws, header_row, ncols)

    def _spacer(row: int, *, fill: PatternFill, height: float) -> int:
        for c in range(1, ncols + 1):
            cell = ws.cell(row=row, column=c, value="")
            cell.fill = fill
        ws.row_dimensions[row].height = height
        return row + 1

    r = header_row + 1
    for li, block in enumerate(prepared):
        if block["kind"] == "empty_lesson":
            vals = [block["lesson_name"], "—", "—", "无页码/未配对", "—", "—"]
            vals += [""] * max_details
            for c, val in enumerate(vals, start=1):
                cell = ws.cell(row=r, column=c, value=val)
                cell.fill = _LESSON_FILL
                cell.border = _CELL_BORDER
                cell.alignment = _WRAP_TOP
            r += 1
            if li < len(prepared) - 1:
                r = _spacer(r, fill=_SPACER_FILL, height=10)
            continue

        pages = block["pages"]
        for si, page in enumerate(pages):
            page_st = page["page_st"]
            tv = page["text_verdict"]
            fill = _fill_for_text_verdict(tv) if tv else None
            base_vals = [
                page["lesson_name"],
                page["old_page"],
                page["new_page"],
                page_st,
                page["image_blob"],
                page["text_summary"],
            ]
            for c, val in enumerate(base_vals, start=1):
                cell = ws.cell(row=r, column=c, value=val)
                cell.alignment = _WRAP_TOP
                cell.border = _CELL_BORDER
                if c == 1 and page["lesson_name"]:
                    cell.fill = _LESSON_FILL
                    cell.font = Font(bold=True, size=11)
                elif c == 4 and page_st == "音标变动注意":
                    cell.fill = _PINYIN_FILL
                elif c == 4 and page_st == "有变动":
                    cell.fill = _DIFF_FILL
                elif c in (5, 6) and fill is not None:
                    cell.fill = fill

            detail_vals = [_detail_cell_value(d) for d in page["details"]]
            while len(detail_vals) < max_details:
                detail_vals.append("")
            nl_budget = page["text_summary"].count("\n") + page["image_blob"].count("\n")
            for c, val in enumerate(detail_vals, start=7):
                cell = ws.cell(row=r, column=c, value=val)
                cell.alignment = _WRAP_TOP
                cell.border = _CELL_BORDER
                if fill is not None and val not in ("", None):
                    cell.fill = fill
                if isinstance(val, str):
                    nl_budget = max(nl_budget, val.count("\n"))

            ws.row_dimensions[r].height = max(56, min(180, 22 + 14 * min(nl_budget + 1, 12)))
            r += 1

            if si < len(pages) - 1:
                r = _spacer(r, fill=_SPACER_FILL, height=8)

        if li < len(prepared) - 1:
            r = _spacer(r, fill=_UNIT_FILL, height=14)

    widths = [22, 8, 8, 14, 36, 22] + [36] * max_details
    _set_widths(ws, widths)
    ws.freeze_panes = "A5"
    ws.row_dimensions[1].height = 22


def _write_appendix_sheet(
    wb: openpyxl.Workbook,
    *,
    sheet_name: str,
    lesson_title: str,
    old_vol: Volume,
    new_vol: Volume,
    item: dict[str, Any],
    compares: dict[int, dict[str, Any]],
) -> None:
    """
    附录表（识字表/写字表/词语表）单独 sheet：
    无图片对比列；每个文字变动单独一行。
    列：课时 | 旧页 | 新页 | 页状态 | 文字对比 | 旧 | 新 | 变动明细
    """
    headers = ["课时", "旧页", "新页", "页状态", "文字对比", "旧", "新", "变动明细"]
    ncols = len(headers)
    ws = wb.create_sheet(sheet_name)
    ws["A1"] = f"附录 · {lesson_title}"
    ws["A1"].font = _TITLE_FONT
    ws.merge_cells("A1:H1")
    ws["A2"] = (
        "附录无插图，已省略图片对比；每个文字变动单独一行；"
        "旧 / 新 / 变动明细 各占一列。"
    )
    ws["A2"].font = _META_FONT
    ws.merge_cells("A2:H2")

    header_row = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=header_row, column=i, value=h)
    _style_header_row(ws, header_row, ncols)

    steps = _page_pairs_for_item(old_vol=old_vol, new_vol=new_vol, item=item)
    r = header_row + 1
    if not steps:
        for c, val in enumerate(
            [lesson_title, "—", "—", "无页码/未配对", "—", "—", "—", "—"],
            start=1,
        ):
            cell = ws.cell(row=r, column=c, value=val)
            cell.fill = _LESSON_FILL
            cell.border = _CELL_BORDER
            cell.alignment = _WRAP_TOP
        _set_widths(ws, [12, 8, 8, 12, 18, 36, 36, 28])
        ws.freeze_panes = "A5"
        return

    for pi, step in enumerate(steps):
        new_page = int(step.get("new_page") or 0)
        old_page = step.get("old_page")
        cmp = compares.get(new_page) or {}
        tc = cmp.get("text_compare") if isinstance(cmp.get("text_compare"), dict) else None
        details = _changed_block_rows(tc)
        page_st = _page_status_text_only(tc)
        text_summary = _text_summary_blob(tc)
        tv = (tc or {}).get("verdict") if tc else ""
        fill = _fill_for_text_verdict(tv) if tv else None

        detail_rows: list[dict[str, Any] | None] = list(details) if details else [None]
        for di, detail in enumerate(detail_rows):
            first_of_page = di == 0
            old_col, new_col, change_col = _detail_split_columns(detail)
            vals = [
                lesson_title if (pi == 0 and first_of_page) else "",
                (old_page if old_page is not None else "—") if first_of_page else "",
                (new_page or "—") if first_of_page else "",
                page_st if first_of_page else "",
                text_summary if first_of_page else "",
                old_col,
                new_col,
                change_col,
            ]
            for c, val in enumerate(vals, start=1):
                cell = ws.cell(row=r, column=c, value=val)
                cell.alignment = _WRAP_TOP
                cell.border = _CELL_BORDER
                if c == 1 and vals[0]:
                    cell.fill = _LESSON_FILL
                    cell.font = Font(bold=True, size=11)
                elif c == 4 and page_st == "音标变动注意" and first_of_page:
                    cell.fill = _PINYIN_FILL
                elif c == 4 and page_st == "有变动" and first_of_page:
                    cell.fill = _DIFF_FILL
                elif c in (5, 6, 7, 8) and fill is not None:
                    cell.fill = fill
            nl = 0
            for v in vals[4:]:
                if isinstance(v, str):
                    nl = max(nl, v.count("\n") + v.count("\r"))
            ws.row_dimensions[r].height = max(40, min(140, 20 + 12 * min(nl + 1, 8)))
            r += 1

        if pi < len(steps) - 1:
            for c in range(1, ncols + 1):
                cell = ws.cell(row=r, column=c, value="")
                cell.fill = _SPACER_FILL
            ws.row_dimensions[r].height = 8
            r += 1

    _set_widths(ws, [12, 8, 8, 12, 18, 36, 36, 28])
    ws.freeze_panes = "A5"
    ws.row_dimensions[1].height = 22


def build_workbook_volume_export_xlsx(
    *,
    old_code: str,
    new_code: str,
) -> tuple[bytes, str]:
    """生成整册对比 Excel：总表 + 各单元表 + 附录分表。返回 (bytes, filename)。"""
    old_code = (old_code or "").strip()
    new_code = (new_code or "").strip()
    if not old_code or not new_code:
        raise ValueError("缺少 old_code / new_code")

    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    coarse = list_stored_pairs(
        old_vol_id=old_vol.id,
        new_vol_id=new_vol.id,
        old_code=old_code,
        new_code=new_code,
        enrich_changes=True,
    )
    items = list(coarse.get("items") or [])
    if not items:
        raise ValueError("尚无课时粗分结果，请先完成粗分与对比")

    compares = _load_compares_by_new_page(old_vol=old_vol, new_vol=new_vol)
    if not compares:
        raise ValueError("尚未找到页级对比结果（本地缓存或数据库均为空）")

    wb = openpyxl.Workbook()
    _write_summary_sheet(
        wb,
        old_code=old_code,
        new_code=new_code,
        coarse=coarse,
        items=items,
    )

    normal_items = [it for it in items if not _is_appendix_export_item(it)]
    appendix_items = [it for it in items if _is_appendix_export_item(it)]

    used_names: set[str] = {"总表"}
    for unit_title, unit_items in _collect_unit_groups(normal_items).items():
        sheet_name = _safe_sheet_name(unit_title, used_names)
        _write_unit_sheet(
            wb,
            sheet_name=sheet_name,
            unit_title=unit_title,
            old_vol=old_vol,
            new_vol=new_vol,
            items=unit_items,
            compares=compares,
        )

    for it in appendix_items:
        title = _appendix_sheet_title(it)
        sheet_name = _safe_sheet_name(title, used_names)
        _write_appendix_sheet(
            wb,
            sheet_name=sheet_name,
            lesson_title=title,
            old_vol=old_vol,
            new_vol=new_vol,
            item=it,
            compares=compares,
        )

    buf = BytesIO()
    wb.save(buf)
    raw = _ensure_xlsx_preserve_cell_newlines(buf.getvalue())
    # ASCII 文件名避免部分浏览器/中间层丢掉中文后变成「册码_.xlsx」
    filename = f"{old_code}_{new_code}_workbook_diff.xlsx"
    return raw, filename


def build_lesson_compare_export_xlsx(
    *,
    old_code: str,
    new_code: str,
    new_lesson_uid: str,
) -> tuple[bytes, str]:
    """生成单课对比 Excel：该课全部页的文字+图片比对。返回 (bytes, filename)。"""
    old_code = (old_code or "").strip()
    new_code = (new_code or "").strip()
    new_lesson_uid = (new_lesson_uid or "").strip()
    if not old_code or not new_code or not new_lesson_uid:
        raise ValueError("缺少 old_code / new_code / new_lesson_uid")

    old_vol = get_diff_volume_by_code(old_code)
    new_vol = get_diff_volume_by_code(new_code)
    coarse = list_stored_pairs(
        old_vol_id=old_vol.id,
        new_vol_id=new_vol.id,
        old_code=old_code,
        new_code=new_code,
        enrich_changes=True,
    )
    items = list(coarse.get("items") or [])
    # 只保留这一课
    items = [
        it for it in items
        if (it.get("new") or {}).get("lesson_uid") == new_lesson_uid
    ]
    if not items:
        raise ValueError("未找到该课的对比结果，请先跑完整课一键")

    compares = _load_compares_by_new_page(old_vol=old_vol, new_vol=new_vol)

    wb = openpyxl.Workbook()
    used_names: set[str] = set()
    for it in items:
        title = (it.get("new") or {}).get("lesson_name") or new_lesson_uid
        sheet_name = _safe_sheet_name(title, used_names)
        _write_unit_sheet(
            wb,
            sheet_name=sheet_name,
            unit_title=title,
            old_vol=old_vol,
            new_vol=new_vol,
            items=[it],
            compares=compares,
        )

    buf = BytesIO()
    wb.save(buf)
    raw = _ensure_xlsx_preserve_cell_newlines(buf.getvalue())
    filename = f"{old_code}_{new_code}_lesson_compare.xlsx"
    return raw, filename
