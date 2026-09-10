"""从 PDF 目录区提取单元/节（对齐 step1 核心规则）。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .benchmark_xlsx import sort_catalog_rows
from .catalog_lesson_line import is_catalog_lesson_line
from .pdf_pages import (
    extract_page_texts_pdfplumber,
    is_toc_like_page,
    ocr_page_texts,
)

_CJK_SPACED = re.compile(r"([\u4e00-\u9fff])[\s\u00a0\u3000]+(?=[\u4e00-\u9fff])")
_UNIT_SPLIT_RE = re.compile(r"(?=第[一二三四五六七八九十百千\d]+单元)|(?=\d{1,2}\s*单元)")
_UNIT_TITLE_RE = re.compile(r"(第[一二三四五六七八九十百千\d]+单元[^0-9]{0,48})")
_UNIT_TITLE_ARABIC_RE = re.compile(r"^(\d{1,2}\s*单元\s*[^\d]{0,48})")
_LEADER_RE = re.compile(r"[·●…．.．_—\-]+")


def collapse_vertical_cjk_spacing(text: str) -> str:
    """目录页竖排/字间空格：「单 元 小 结」→「单元小结」。"""
    if not text:
        return text
    if not re.search(r"[\u4e00-\u9fff][\s\u00a0\u3000]+[\u4e00-\u9fff]", text):
        return text
    t = text
    for _ in range(80):
        nt = _CJK_SPACED.sub(r"\1", t)
        if nt == t:
            break
        t = nt
    return re.sub(r"\s+", " ", t).strip()


def _lesson_line_count(page_text: str) -> int:
    """页面上「1 空气占据空间吗 / 1光的传播」式目录行数量。

    注意：勿把正文步骤「1.把几支…」在替换标点后误判为目录行。
    """
    n = 0
    for raw in (page_text or "").replace("\r", "\n").split("\n"):
        raw_s = (raw or "").strip()
        if not raw_s:
            continue
        # 正文步骤编号（数字+点/顿号）不是目录课时
        if re.match(r"^\d{1,2}[.．、]", raw_s):
            continue
        line = collapse_vertical_cjk_spacing(raw_s)
        # 不要把 ASCII「.」当成 leader 抹掉，否则「1.步骤」会变成「1 步骤」
        line = re.sub(r"[·●…_—\-]+", " ", line)
        line = re.sub(r"\s+", " ", line)
        line = re.sub(r"\s+\d{1,3}\s*$", "", line).strip()
        # 「1 课名」或苏教常见「1课名」（数字与汉字间无空格）
        if re.match(r"^\d{1,2}\s*[\u4e00-\u9fff]", line):
            n += 1
    return n


def _toc_title_hit(page_text: str) -> bool:
    """目录页标题（含 OCR 常见误识）。"""
    compact = re.sub(r"\s+", "", page_text or "")
    return any(
        k in compact
        for k in ("目录", "誉目", "眷目", "脊目", "着目", "目次", "日录")
    )


def find_catalog_page_span(pages_text: list[str], *, max_scan: int = 22) -> slice:
    """保留连续多节「数字+课题名」的目录页（排除前言/CIP）。"""
    if not pages_text:
        return slice(0, 0)
    n = min(max_scan, len(pages_text))
    first = -1
    last = -1
    for p in range(n):
        text = pages_text[p] or ""
        lesson_n = _lesson_line_count(text)
        # 强信号：目录标题，或「单元」+ 多条数字课时（避免正文步骤页）
        strong = _toc_title_hit(text) or (lesson_n >= 3 and "单元" in text)
        if strong:
            if first < 0:
                first = p
            last = p
            continue
        if first >= 0 and lesson_n == 0 and not _toc_title_hit(text):
            break
    if first >= 0:
        return slice(first, last + 1)

    empty_targets: list[dict[str, Any]] = []
    for p in range(n):
        t = pages_text[p] or ""
        if _toc_title_hit(t) and is_toc_like_page(t, empty_targets):
            return slice(p, min(p + 3, n))
        if "目录" in t and is_toc_like_page(t, empty_targets):
            return slice(p, min(p + 3, n))
    return slice(0, min(9, len(pages_text)))


def _lines_from_page_span(pages_text: list[str], span: slice) -> list[str]:
    lines: list[str] = []
    for p in pages_text[span]:
        if p:
            lines.extend(p.split("\n"))
    return lines


def _catalog_score(catalog: list[dict[str, Any]]) -> tuple[int, int, int]:
    """(单元数, 课时数, 最大节号) — 用于挑选最佳识别结果。"""
    units = {str(r.get("unit") or "").strip() for r in catalog}
    units.discard("")
    lesson_nos: list[int] = []
    for r in catalog:
        les = str(r.get("lesson") or "")
        if "单元小结" in les:
            continue
        m = re.match(r"^(\d{1,2})\s+", les)
        if m:
            lesson_nos.append(int(m.group(1)))
    return len(units), len(lesson_nos), max(lesson_nos) if lesson_nos else 0


def _catalog_suspect(catalog: list[dict[str, Any]]) -> bool:
    if not catalog:
        return True
    units, count, max_no = _catalog_score(catalog)
    if count < 12 or units < 3:
        return True
    if max_no < count * 0.6:
        return True
    return False


class CatalogLineParser:
    """目录行解析（对齐 step1_extract_catalog_v2 核心规则）。"""

    EXCLUDE_KEYWORDS = (
        "前言",
        "编者的话",
        "版权",
        "图书在版编目",
        "定价",
        "出版社",
        "附录",
        "后记",
        "索引",
        "词汇表",
        "义务教育",
        "教科书",
        "ISBN",
        "CIP",
        "科学探究",
        "问题情境",
        "拓展延伸",
        "拓展迁移",
        "拓展活动",
        "课堂练习",
        "课后练习",
        "活动手册",
        "指南车信箱",
        "安全警示",
        "思想与方法",
        "工程实践",
        "责任编辑",
    )
    LESSON_RE = re.compile(r"^\d{1,2}\s*[\u4e00-\u9fff]")

    def should_exclude(self, text: str) -> bool:
        t = text.strip()
        if not t or t in {"目录", "单元小结", "誉目", "眷目", "目次"}:
            return True
        if any(k in t for k in self.EXCLUDE_KEYWORDS):
            return True
        if re.match(r"^\d+$", t):
            return True
        return False

    def clean_line(self, text: str) -> str:
        t = collapse_vertical_cjk_spacing(text.strip())
        # 「1光的传播」→「1 光的传播」，便于后续课时识别
        t = re.sub(r"^(\d{1,2})([\u4e00-\u9fff])", r"\1 \2", t)
        t = _LEADER_RE.sub(" ", t)
        t = re.sub(r"\s+", " ", t)
        t = re.sub(r"\s+\d{1,3}\s*$", "", t).strip()
        return t

    def _parse_page_suffix(self, text: str) -> tuple[str, int | None]:
        m = re.search(r"\s+(\d{1,3})\s*$", text.strip())
        if not m:
            return text, None
        page = int(m.group(1))
        if page < 1 or page > 500:
            return text, None
        return text[: m.start()].strip(), page

    def extract_unit_title(self, text: str) -> str | None:
        """识别单元行：「第一单元…」「1单元 光与色彩」或「绪论」。"""
        s = collapse_vertical_cjk_spacing(text.strip())
        s = _LEADER_RE.sub(" ", s)
        s = re.sub(r"\s+\d{1,3}\s*$", "", s).strip()
        if re.match(r"^绪论\s*$", s):
            return "绪论"
        m = _UNIT_TITLE_RE.search(s)
        if m:
            unit = self.clean_line(m.group(1))
            return unit or None
        m = _UNIT_TITLE_ARABIC_RE.match(s)
        if m:
            unit = self.clean_line(m.group(1))
            # 「1 单元 光与色彩」规范化
            unit = re.sub(r"^(\d{1,2})\s*单元\s*", r"\1单元 ", unit).strip()
            return unit or None
        return None

    def is_lesson_line(self, line: str) -> bool:
        t = self.clean_line(line)
        if len(t) < 2 or len(t) > 120:
            return False
        if self.should_exclude(t) or "单元小结" in t:
            return False
        # 「1单元 …」是单元行，不是课时
        if re.match(r"^\d{1,2}\s*单元", t):
            return False
        if is_catalog_lesson_line(t):
            return True
        if t.count("？") + t.count("?") > 1:
            return False
        return False

    def _split_unit_segments(self, line: str) -> list[str]:
        if (line or "").count("单元") < 2:
            return [line]
        matches = list(
            re.finditer(r"(?:第[一二三四五六七八九十百千\d]+|\d{1,2})\s*单元", line)
        )
        if len(matches) < 2:
            return [line]
        parts: list[str] = []
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            part = line[m.start() : end].strip()
            if part:
                parts.append(part)
        return parts or [line]

    def _expand_dual_column_lessons(self, line: str) -> list[str]:
        """双栏目录一行多课：「1光的传播 2 13弹力」→ 两条。"""
        s = collapse_vertical_cjk_spacing((line or "").strip())
        if not s or "单元" in s:
            return [s] if s else []
        # 找出所有「课号+课名」；中间夹页码（纯数字）会被自然跳过
        hits = re.findall(
            r"(\d{1,2})\s*([\u4e00-\u9fff][^\d]{1,36})",
            s,
        )
        if len(hits) < 2:
            return [s]
        out: list[str] = []
        for no, name in hits:
            name = re.sub(r"[·●…._—\-]+", " ", name).strip()
            name = re.sub(r"\s+", " ", name)
            name = re.sub(r"\s+\d{1,3}$", "", name).strip()
            if len(name) < 2 or len(name) > 36:
                continue
            if any(w in name for w in ("学而思", "科学专用", "传递追责", "仅供", "授权")):
                continue
            out.append(f"{no} {name}")
        return out or [s]

    def parse_lines(self, lines: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        current_unit: str | None = None
        # 双栏目录：同一行两个单元标题时，左栏课对左单元、右栏课对右单元
        column_units: list[str] = []
        for raw in lines:
            raw = (raw or "").strip()
            if not raw:
                continue
            raw = collapse_vertical_cjk_spacing(raw)
            line, page_no = self._parse_page_suffix(raw)
            line = self.clean_line(line)
            if not line or self.should_exclude(line):
                continue
            segments = self._split_unit_segments(line)
            unit_titles = []
            for segment in segments:
                seg = self.clean_line(segment)
                if not seg or self.should_exclude(seg):
                    continue
                unit_title = self.extract_unit_title(seg)
                if unit_title:
                    unit_titles.append(unit_title)
                    current_unit = unit_title
                    continue
                if "单元小结" in seg and current_unit:
                    out.append(
                        {
                            "unit": current_unit,
                            "lesson": "单元小结",
                            "pdf_page": page_no,
                        }
                    )
                    continue
                pieces = self._expand_dual_column_lessons(seg)
                for idx, piece in enumerate(pieces):
                    piece = self.clean_line(piece)
                    if not piece or self.should_exclude(piece):
                        continue
                    if not self.is_lesson_line(piece):
                        continue
                    unit_for = current_unit
                    if len(column_units) >= 2 and len(pieces) >= 2:
                        unit_for = column_units[min(idx, len(column_units) - 1)]
                    if not unit_for:
                        continue
                    out.append(
                        {
                            "unit": unit_for,
                            "lesson": piece,
                            "pdf_page": page_no if len(pieces) == 1 else None,
                        }
                    )
            if len(unit_titles) >= 2:
                column_units = unit_titles[:2]
            elif len(unit_titles) == 1 and not column_units:
                column_units = [unit_titles[0]]
        return out


def extract_catalog_from_pdf(pdf_path: Path, *, ocr_max_pages: int = 9) -> list[dict[str, Any]]:
    parser = CatalogLineParser()
    text_pages = extract_page_texts_pdfplumber(pdf_path, max_pages=max(ocr_max_pages, 20))
    span = find_catalog_page_span(text_pages)
    lines = _lines_from_page_span(text_pages, span)
    catalog = parser.parse_lines(lines)

    if not _catalog_suspect(catalog):
        return sort_catalog_rows(catalog)

    from .pdf_catalog_step1 import extract_catalog_lines_step1_ocr

    ocr_page_count = max(span.stop, ocr_max_pages)
    step1_lines = extract_catalog_lines_step1_ocr(pdf_path, max_pages=ocr_page_count)
    step1_catalog = parser.parse_lines(step1_lines)

    candidates: list[tuple[str, list[dict[str, Any]]]] = [
        ("text", catalog),
        ("step1_ocr", step1_catalog),
    ]

    if _catalog_suspect(catalog) or _catalog_suspect(step1_catalog):
        ocr_pages = ocr_page_texts(pdf_path, dpi=150, max_pages=ocr_page_count)
        ocr_lines = _lines_from_page_span(ocr_pages, slice(0, len(ocr_pages)))
        ocr_catalog = parser.parse_lines(ocr_lines)
        candidates.append(("page_ocr", ocr_catalog))

    _tag, best = max(candidates, key=lambda x: _catalog_score(x[1]))
    if _catalog_score(best)[1] == 0:
        return []
    return sort_catalog_rows(best)
