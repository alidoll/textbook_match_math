"""PDF 逐页文本提取 + 按课时标题定位起始页。"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .text_norm import (
    is_numbered_lesson_no,
    lesson_seq_int,
    norm_lesson_title,
    norm_text,
    strip_lesson_seq,
)

_log = logging.getLogger(__name__)

# 目录页扫描上限（物理页，0-based 下标范围）
_TOC_SCAN_PAGES = 25

# 小科「第 X 单元」/ 数学「第 X 章」扉页横幅
_UNIT_HEADER_RE = re.compile(r"第[一二三四五六七八九十百千\d]+(?:单元|章)")
_CIRCLED_NUMS = "①②③④⑤⑥⑦⑧⑨⑩"
_CIRCLED_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]")

# 短课题名子串匹配易误中单元导航/诗歌正文（如「建个植物角」）
_SHORT_KEY_MAX_LEN = 4

# 前言/致辞页：正文常顺带提及第一课课名，不得当课节起始
_FRONT_MATTER_MARKERS = (
    "亲爱的小朋友",
    "给小朋友的信",
    "给朋友的信",
    "给孩子的信",
    "编者大朋友",
    "致同学",
    "编者的话",
    "写给小朋友",
    "本书主要栏目",
    "主要栏目及说明",
    "栏目及说明",
    "使用说明",
    "图书在版编目",
)


def is_front_matter_page(page_text: str) -> bool:
    """封面后的致辞/栏目说明等，不是课节正文起始页。"""
    raw = page_text or ""
    head = raw[:480]
    if any(m in head for m in _FRONT_MATTER_MARKERS):
        return True
    # 「给…的信」类标题常拆成多行/花字，页首再配称呼
    if ("的信" in head[:80] or "的话" in head[:80]) and (
        "亲爱的" in head or "小朋友" in head or "同学们" in head
    ):
        return True
    return False


def lesson_no_circled(lesson_no: str) -> str | None:
    n = lesson_seq_int(lesson_no)
    if n is None or not (1 <= n <= 10):
        return None
    return _CIRCLED_NUMS[n - 1]


def _lesson_name_in_unit_title(tgt: dict[str, Any]) -> bool:
    """无节号课时：课名即单元主题（如「第六单元 科学的历程」）。"""
    name = norm_text((tgt.get("lesson_name") or "").strip())
    unit = norm_text((tgt.get("unit_title") or "").strip())
    if not name or not unit or len(name) <= _SHORT_KEY_MAX_LEN:
        return False
    m = _UNIT_HEADER_RE.search(unit)
    if not m:
        return False
    unit_body = unit[m.end():]
    if not unit_body:
        return False
    if name == unit_body:
        return True
    if len(name) >= 4 and (name in unit_body or unit_body in name):
        return True
    return False


def matches_unit_intro_lesson_start(page_text: str, tgt: dict[str, Any]) -> bool:
    """无节号且课名即单元主题时，「第 X 单元 + 课名」扉页即课节起始页。"""
    if is_numbered_lesson_no((tgt.get("lesson_no") or "").strip()):
        return False
    if tgt.get("is_summary"):
        return False
    if not _lesson_name_in_unit_title(tgt):
        return False
    if not is_unit_intro_divider_page(page_text):
        return False
    name_norm = norm_text((tgt.get("lesson_name") or "").strip())
    return bool(name_norm and name_norm in norm_text(page_text))


def matches_chapter_intro_for_unit(page_text: str, tgt: dict[str, Any]) -> bool:
    """数学「第 X 章」扉页归属该章首课（含 15.1 等有节号课）。

    与小科「第 X 单元」导航页区分：单元导航仍由 trim_leading 去掉；
    章导入页应保留在下一章第一节。
    """
    if tgt.get("is_summary"):
        return False
    unit = norm_text((tgt.get("unit_title") or "").strip())
    m = re.search(r"第[一二三四五六七八九十百千\d]+章", unit)
    if not m:
        return False
    if not is_unit_intro_divider_page(page_text):
        return False
    return m.group(0) in norm_text(page_text)


def _looks_like_catalog_line_norm(line_norm: str) -> bool:
    """目录行：短课题名 + 行末页码（无问句/长引导语）。

    苏教等短页正文常在页末带手册页码「5」，整页仅数十字符且以数字结尾，
    不能仅凭「行末页码」当成目录，否则会把真实课节标题页误杀。
    """
    if len(line_norm) > 55:
        return False
    m = re.search(r"^(.*?)(\d{1,3})$", line_norm)
    if not m:
        return False
    body = m.group(1)
    if len(body) > 24:
        return False
    if re.search(r"[？?。！!，,：:]", body):
        return False
    return True


def _section_prefix_anchors(name: str) -> list[str]:
    """①–⑩ / 1–10 + 课题名（PDF 节号可能与基准目录节号不一致）。"""
    anchors: list[str] = []
    seen: set[str] = set()
    for circled in _CIRCLED_NUMS:
        a = norm_text(f"{circled}{name}")
        if a not in seen:
            seen.add(a)
            anchors.append(a)
    for d in range(1, 11):
        for sep in ("", " "):
            a = norm_text(f"{d}{sep}{name}")
            if a not in seen:
                seen.add(a)
                anchors.append(a)
    return anchors


def _name_at_title_position(
    rest: str,
    name_norm: str,
    stripped_norm: str,
) -> bool:
    """节号后的 rest 应以课题名开头（避免正文「常见的哺乳动物」误中）。"""
    for candidate in (name_norm, stripped_norm):
        if not candidate:
            continue
        if rest.startswith(candidate):
            return True
        idx = rest.find(candidate)
        # 仅允许节号与课名之间极短噪声（空格/标点）；「什么植物的种子」等夹字须拒绝
        if 0 < idx <= 2:
            prev = rest[idx - 1]
            if prev.isalnum() and not prev.isdigit():
                continue
            if "\u4e00" <= prev <= "\u9fff":
                continue
            return True
    return False


def _line_matches_any_section_title(
    line: str,
    line_norm: str,
    name_norm: str,
    stripped_norm: str,
) -> bool:
    if _looks_like_catalog_line_norm(line_norm):
        return False
    for circled in _CIRCLED_NUMS:
        if not line.startswith(circled):
            continue
        # 须为「②课题名」标题行；排除「②我们搜集的是什么植物的种子」等步骤问句
        rest = norm_text(line[len(circled) :].lstrip())
        if _name_at_title_position(rest, name_norm, stripped_norm):
            return True
    for d in range(1, 11):
        prefix = norm_text(str(d))
        if not line_norm.startswith(prefix):
            continue
        rest = line_norm[len(prefix) :]
        if _name_at_title_position(rest, name_norm, stripped_norm):
            return True
    return False


def lesson_header_anchors(tgt: dict[str, Any]) -> list[str]:
    """课节标题锚点（归一化），用于 OCR 整页一行时的全文检索。"""
    lesson_name = (tgt.get("lesson_name") or "").strip()
    lesson_no = (tgt.get("lesson_no") or "").strip()
    if not lesson_name:
        return []

    stripped = strip_lesson_seq(lesson_name)
    names = [lesson_name]
    if stripped and stripped != lesson_name:
        names.append(stripped)

    anchors: list[str] = []
    seen: set[str] = set()
    for name in names:
        for a in _section_prefix_anchors(name):
            if a not in seen:
                seen.add(a)
                anchors.append(a)
        # 有节号课（含第 1 课）只用「节号+课名」全文锚点。
        # 第 1 课裸课名易误中前言「给小朋友的信」等正文提及。
        if not is_numbered_lesson_no(lesson_no):
            a = norm_lesson_title(name)
            if len(a) > _SHORT_KEY_MAX_LEN and a not in seen:
                seen.add(a)
                anchors.append(a)
    return anchors


def _is_numbered_section_anchor(anchor: str) -> bool:
    """①–⑩ 或 1–10 开头的课节锚点（如「5植物角」），短但仍可靠。"""
    if not anchor:
        return False
    if anchor[0] in _CIRCLED_NUMS:
        return True
    return anchor[0].isdigit()


def _anchor_near_page_start(full_norm: str, anchor: str) -> bool:
    """标题锚点应靠近页首（OCR 常把整页合成一行）。"""
    if not anchor or anchor not in full_norm:
        return False
    idx = full_norm.find(anchor)
    # 只用锚点附近短窗判断目录行，避免正文页末「见手册第N页」把整页误判成目录
    window = full_norm[idx : idx + max(len(anchor) + 28, 40)]
    if _looks_like_catalog_line_norm(window):
        return False
    if idx <= 160:
        return True
    return idx < len(full_norm) * 0.4


def has_lesson_header(page_text: str, tgt: dict[str, Any]) -> bool:
    """页面上是否存在课节标题行：节号 + 课题名（非正文随口提及）。"""
    if is_front_matter_page(page_text):
        return False

    lesson_name = (tgt.get("lesson_name") or "").strip()
    lesson_no = (tgt.get("lesson_no") or "").strip()
    if not lesson_name:
        return False

    name_norm = norm_lesson_title(lesson_name)
    stripped = strip_lesson_seq(lesson_name)
    stripped_norm = norm_lesson_title(stripped) if stripped else ""
    circled = lesson_no_circled(lesson_no) if lesson_no else None
    no_norm = norm_text(lesson_no) if lesson_no else ""

    # 裸课名仅允许整行短标题（真标题行）；第 1 课也不再靠全文裸锚点
    allow_bare_title_line = (
        not is_numbered_lesson_no(lesson_no) or lesson_seq_int(lesson_no) == 1
    )

    for raw_line in (page_text or "").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if len(line) < 2:
            continue
        line_norm = norm_text(line)

        if circled and line.startswith(circled):
            if name_norm in line_norm or (stripped_norm and stripped_norm in line_norm):
                return True

        if is_numbered_lesson_no(lesson_no) and no_norm and line_norm.startswith(no_norm):
            rest = line_norm[len(no_norm):]
            if _name_at_title_position(rest, name_norm, stripped_norm):
                return True

        if allow_bare_title_line:
            for candidate in (name_norm, stripped_norm):
                if candidate and line_norm.startswith(candidate):
                    if len(line_norm) <= len(candidate) + 6:
                        return True

        if _line_matches_any_section_title(line, line_norm, name_norm, stripped_norm):
            return True

    if matches_unit_intro_lesson_start(page_text, tgt):
        return True

    full_norm = norm_text(page_text)
    for anchor in lesson_header_anchors(tgt):
        if len(anchor) <= _SHORT_KEY_MAX_LEN and not _is_numbered_section_anchor(anchor):
            continue
        if _anchor_near_page_start(full_norm, anchor):
            return True

    # 苏教短标题：课号与课名都在页首区但不相邻（如「11…。拧螺丝观察」）
    if is_numbered_lesson_no(lesson_no) and name_norm and len(name_norm) >= 2:
        early = full_norm[:220]
        idx = early.find(name_norm)
        if 0 <= idx <= 140:
            prev = early[idx - 1] if idx > 0 else ""
            if idx == 0 or prev in "。！？；;…、" or prev.isdigit():
                if (no_norm and no_norm in early[: idx + 1]) or (
                    circled and circled in (page_text or "")[:260]
                ):
                    return True

    return False


def is_unit_nav_like_page(page_text: str, tgt: dict[str, Any] | None = None) -> bool:
    """
    单元/章导航扉页：含「第X单元|章」横幅，列出本单元各课，正文仅顺带提及课名。
    如「第二单元 植物的生长」+「建个植物角…」——不是「① 植物角」课节起始页。
    """
    if tgt and tgt.get("is_summary"):
        return False

    raw = page_text or ""
    if len(raw.strip()) < 4 or not _UNIT_HEADER_RE.search(raw):
        return False

    if tgt and has_lesson_header(raw, tgt):
        return False

    if len(_CIRCLED_RE.findall(raw)) >= 2:
        return True

    return True


def page_matches_lesson(page_text: str, tgt: dict[str, Any]) -> bool:
    """判断一页是否为该课时的正文起始页。"""
    if is_front_matter_page(page_text):
        return False

    if has_lesson_header(page_text, tgt):
        return True

    if is_unit_nav_like_page(page_text, tgt):
        return False

    lesson_no = (tgt.get("lesson_no") or "").strip()
    # 有数字节号时必须有标题行，避免延续页正文命中下一课关键词
    if is_numbered_lesson_no(lesson_no) and not tgt.get("is_summary"):
        return False

    keys = tgt.get("keys") or []
    norm_page = norm_text(page_text)
    for key in keys:
        if not key or len(key) < 2 or key not in norm_page:
            continue
        if len(key) <= _SHORT_KEY_MAX_LEN:
            continue
        return True
    return False


def _page_contains(page_text: str, keys: list[str]) -> bool:
    norm_page = norm_text(page_text)
    for key in keys:
        if key and len(key) >= 2 and key in norm_page:
            return True
    return False


def count_distinct_lesson_hits(page_text: str, targets: list[dict[str, Any]]) -> int:
    """一页上出现多少个不同课时的匹配键。"""
    norm_page = norm_text(page_text)
    hits = 0
    for tgt in targets:
        for key in tgt.get("keys") or []:
            if key and len(key) >= 3 and key in norm_page:
                hits += 1
                break
    return hits


def catalog_line_count(page_text: str) -> int:
    """目录行特征：中文标题 + 行末页码。"""
    count = 0
    for raw in (page_text or "").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if len(line) < 4 or len(line) > 90:
            continue
        if not re.search(r"[\u4e00-\u9fff]", line):
            continue
        if re.search(r"\d{1,3}\s*$", line):
            count += 1
    return count


def is_toc_like_page(page_text: str, targets: list[dict[str, Any]]) -> bool:
    """
    判断是否为目录页（多课名 / 目录行 / 显式「目录」）。
    避免「推拉游戏」等在目录页被当成正文起始页。
    """
    if not page_text or len(page_text.strip()) < 6:
        return False

    hits = count_distinct_lesson_hits(page_text, targets)
    cat_lines = catalog_line_count(page_text)
    raw = page_text

    if "目录" in raw and (hits >= 1 or cat_lines >= 2):
        return True
    # 多课命中须有目录行特征，避免正文提及前后课（如「蚕宝宝在长大」页提到「蚕宝宝出生了」）
    if hits >= 2 and cat_lines >= 2:
        return True
    if cat_lines >= 3:
        return True
    if hits >= 1 and cat_lines >= 2:
        return True
    return False


def find_last_toc_page_index(
    pages_text: list[str],
    targets: list[dict[str, Any]],
    *,
    scan_pages: int = _TOC_SCAN_PAGES,
) -> int:
    """前若干页中最后一页目录的 0-based 下标；无则 -1。"""
    last = -1
    for p in range(min(scan_pages, len(pages_text))):
        text = pages_text[p] or ""
        if is_toc_like_page(text, targets):
            last = p
            continue
        # 已进入第一课正文：后续含表格/页码的续页不应再拉长目录区
        if _page_has_body_lesson_header(text, targets):
            break
    return last


def _page_has_body_lesson_header(
    page_text: str,
    targets: list[dict[str, Any]],
) -> bool:
    """页首附近出现课节标题行，且非显式「目录」页。"""
    if is_front_matter_page(page_text):
        return False
    head = (page_text or "")[:120]
    if "目录" in head:
        return False
    for tgt in targets:
        lesson_no = (tgt.get("lesson_no") or "").strip()
        # 「课题N」与裸数字同属有节；绪论等空课号也要能截断目录区
        if not is_numbered_lesson_no(lesson_no) and lesson_no:
            continue
        if has_lesson_header(page_text, tgt):
            return True
    return False


def compute_content_search_start(
    pages_text: list[str],
    targets: list[dict[str, Any]],
    *,
    skip_pages: int = 3,
) -> int:
    """正文搜索起始页（0-based）：跳过封面等 + 整段目录区之后。"""
    toc_end = find_last_toc_page_index(pages_text, targets)
    if toc_end >= 0:
        return max(skip_pages, toc_end + 1)
    return max(0, skip_pages)


def extract_page_texts_pdfplumber(pdf_path: Path, max_pages: int | None = None) -> list[str]:
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        limit = total if max_pages is None else min(total, max_pages)
        for i in range(limit):
            text = pdf.pages[i].extract_text() or ""
            pages.append(text)
    return pages


def ocr_page_texts(
    pdf_path: Path,
    *,
    dpi: int = 120,
    max_pages: int | None = None,
    start_page: int = 0,
) -> list[str]:
    import fitz
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    doc = fitz.open(str(pdf_path))
    pages: list[str] = []
    try:
        total = len(doc)
        begin = max(0, start_page)
        end = total if max_pages is None else min(total, begin + max_pages)
        for i in range(begin, end):
            page = doc.load_page(i)
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            result, _ = ocr(arr)
            if result:
                pages.append(" ".join(str(item[1]) for item in result if item and len(item) > 1))
            else:
                pages.append("")
    finally:
        doc.close()
    return pages


_ocr_engine: Any = None


def _get_rapid_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_engine = RapidOCR()
    return _ocr_engine


def ocr_single_page_text(
    pdf_path: Path,
    page_0: int,
    *,
    dpi: int = 120,
    doc: Any = None,
    max_side: int = 1400,
) -> str:
    import fitz
    import numpy as np

    ocr = _get_rapid_ocr()
    own_doc = None
    try:
        page_doc = doc
        if page_doc is None:
            own_doc = fitz.open(str(pdf_path))
            page_doc = own_doc
        page = page_doc.load_page(page_0)
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        h, w = arr.shape[:2]
        side = max(h, w)
        if side > max_side:
            scale = max_side / side
            from PIL import Image

            arr = np.asarray(
                Image.fromarray(arr).resize((int(w * scale), int(h * scale)))
            )
        result, _ = ocr(arr)
        if result:
            return " ".join(str(item[1]) for item in result if item and len(item) > 1)
        return ""
    finally:
        if own_doc is not None:
            own_doc.close()


class PdfOcrCache:
    """按页 lazy OCR，避免扫描版整册一次性识别。"""

    def __init__(self, pdf_path: Path, *, dpi: int = 120) -> None:
        self.pdf_path = pdf_path
        self.dpi = dpi
        self._texts: dict[int, str] = {}
        self._doc: Any = None
        self._total: int | None = None

    def page_count(self) -> int:
        self._ensure_doc()
        return self._total or 0

    def _ensure_doc(self) -> None:
        if self._doc is None:
            import fitz

            self._doc = fitz.open(str(self.pdf_path))
            self._total = len(self._doc)

    def get(self, page_0: int) -> str:
        if page_0 not in self._texts:
            self._ensure_doc()
            self._texts[page_0] = ocr_single_page_text(
                self.pdf_path, page_0, dpi=self.dpi, doc=self._doc
            )
        return self._texts[page_0]

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None


class LazyPageTexts:
    """list 接口的 lazy 页文本，供 trim / 匹配复用。"""

    def __init__(self, cache: PdfOcrCache) -> None:
        self._cache = cache

    def __len__(self) -> int:
        return self._cache.page_count()

    def __getitem__(self, index: int) -> str:
        return self._cache.get(index)


def is_pdf_text_sparse(
    pdf_path: Path,
    *,
    sparse_threshold: float = 0.45,
    max_scan_pages: int = 30,
) -> bool:
    """扫描版判定：只检查前若干页，避免整册 pdfplumber 拖慢解析。"""
    pages = extract_page_texts_pdfplumber(pdf_path, max_pages=max_scan_pages)
    if not pages:
        return True
    sparse = sum(1 for p in pages if len(p.strip()) < 24)
    return sparse / max(len(pages), 1) >= sparse_threshold


def extract_all_page_texts(
    pdf_path: Path,
    *,
    ocr_dpi: int = 120,
    sparse_threshold: float = 0.45,
) -> tuple[list[str], str]:
    """
    先 pdfplumber；若多数页几乎无文字则整册 OCR。
    返回 (每页文本, mode) mode=text|ocr
    """
    pages = extract_page_texts_pdfplumber(pdf_path)
    if not pages:
        return ocr_page_texts(pdf_path, dpi=ocr_dpi), "ocr"

    sparse = sum(1 for p in pages if len(p.strip()) < 24)
    if sparse / max(len(pages), 1) >= sparse_threshold:
        _log.info("PDF %s 文本层稀疏，改用 OCR（%d 页）", pdf_path.name, len(pages))
        return ocr_page_texts(pdf_path, dpi=ocr_dpi), "ocr"
    return pages, "text"


def _try_match_lesson_start_at_page(
    page_text: str,
    tgt: dict[str, Any],
    targets: list[dict[str, Any]],
) -> bool:
    """课节标题优先于目录页规则（避免正文起始页被误判为目录而跳过）。"""
    if is_front_matter_page(page_text):
        return False
    if page_matches_lesson(page_text, tgt):
        return True
    if is_toc_like_page(page_text, targets):
        return False
    return False


def find_lesson_start_pages(
    pages_text: list[str],
    targets: list[dict[str, Any]],
    *,
    skip_pages: int = 3,
) -> list[int | None]:
    """为每个 target 找起始页（0-based）；目录页上的命中会被跳过。

    苏教等目录常把「专项学习」插在单元之间，但 PDF 正文可能在全册靠后。
    若与有节号课同一趟单调扫描，会先命中后页专项、把中间正式课挤掉。
    故先定位有节号课，再回填无节号/专项。
    """
    total = len(pages_text)
    starts: list[int | None] = [None] * len(targets)
    search_from = compute_content_search_start(pages_text, targets, skip_pages=skip_pages)

    numbered_idxs = [
        i
        for i, tgt in enumerate(targets)
        if is_numbered_lesson_no((tgt.get("lesson_no") or "").strip())
    ]
    other_idxs = [i for i in range(len(targets)) if i not in set(numbered_idxs)]

    sf = search_from
    for idx in numbered_idxs:
        tgt = targets[idx]
        found: int | None = None
        for p in range(sf, total):
            if not _try_match_lesson_start_at_page(pages_text[p], tgt, targets):
                continue
            found = p
            break
        starts[idx] = found
        if found is not None:
            sf = found + 1

    for idx in other_idxs:
        tgt = targets[idx]
        lo = search_from
        for j in range(idx - 1, -1, -1):
            if starts[j] is not None:
                lo = starts[j] + 1
                break
        hi = total
        for j in range(idx + 1, len(targets)):
            if starts[j] is not None:
                hi = starts[j]
                break
        # 专项常在册末：邻接有节号课尚未定位时，允许扫到文末
        if hi <= lo:
            lo = search_from
            hi = total
        found = None
        for p in range(lo, hi):
            if not _try_match_lesson_start_at_page(pages_text[p], tgt, targets):
                continue
            found = p
            break
        if found is None and hi < total:
            for p in range(hi, total):
                if not _try_match_lesson_start_at_page(pages_text[p], tgt, targets):
                    continue
                found = p
                break
        starts[idx] = found

    _fill_unmatched_lesson_starts(pages_text, targets, starts, skip_pages=skip_pages)
    return starts


def is_lesson_supplement_section_page(page_text: str) -> bool:
    """课内附加板块页（非下一课起始）：科学探究 / 拓展迁移 / 反思评价等。"""
    raw = (page_text or "").strip()
    if len(raw) < 4:
        return False
    markers = ("科学探究", "拓展迁移", "拓展与迁移", "反思评价")
    lines = [ln.strip() for ln in raw.replace("\r", "\n").split("\n") if ln.strip()]
    for line in lines[:5]:
        ln = norm_text(line)
        for marker in markers:
            if ln.startswith(marker) or ln == marker:
                return True
            if marker in ln and len(ln) <= len(marker) + 20:
                return True
    return False


def page_likely_lesson_start(page_text: str, tgt: dict[str, Any]) -> bool:
    """课节起始页弱匹配：页首区域含课题名，且非小结/扉页/附加板块。"""
    text = page_text or ""
    if len(text.strip()) < 4:
        return False
    if has_lesson_header(text, tgt):
        return True
    if is_unit_summary_page(text):
        return False
    if is_unit_intro_divider_page(text) and not matches_unit_intro_lesson_start(
        text, tgt
    ):
        return False
    if is_lesson_supplement_section_page(text):
        return False

    lesson_name = (tgt.get("lesson_name") or "").strip()
    stripped = strip_lesson_seq(lesson_name)
    lesson_no = (tgt.get("lesson_no") or "").strip()
    names = [n for n in (lesson_name, stripped) if n and len(n) >= 3]

    for name in names:
        name_norm = norm_text(name)
        early = norm_text(text[:360])
        pos = early.find(name_norm)
        if pos < 0:
            continue
        # 课中正文常复述课题名（如「是否都是植物的种子」），不得当成起始页
        if pos > 40:
            continue
        if is_numbered_lesson_no(lesson_no):
            no_norm = norm_text(lesson_no)
            circled = lesson_no_circled(lesson_no)
            # 节号须出现在课题名附近，避免页内「②③」等步骤号误充课号
            number_ok = False
            if no_norm:
                no_pos = early.find(no_norm)
                if no_pos >= 0 and abs(no_pos - pos) <= 12:
                    number_ok = True
            if circled and circled in early[: max(24, pos + 8)]:
                number_ok = True
            if number_ok:
                return True
            multi_part = "（" in lesson_name or "(" in lesson_name
            # 短课名且无节号：仅当课题名几乎顶格（真标题行），排除问句中夹带
            if not multi_part and 3 <= len(name_norm) <= 8 and pos <= 2:
                return True
            continue
        return pos <= 8
    return False


def refine_lesson_starts_near_hints(
    pages_text: list[str] | LazyPageTexts,
    targets: list[dict[str, Any]],
    hints_1based: list[int],
    *,
    window_before: int = 2,
    window_after: int = 14,
) -> list[int]:
    """
    目录下一课页码常落在「科学探究」等尾页，而非课节标题页。
    在提示页附近向前扫描，跳过小结/扉页/附加板块，定位真实课名起始页（0-based）。
    """
    total = len(pages_text)
    starts: list[int] = []
    search_from = 0

    for tgt, hint in zip(targets, hints_1based, strict=True):
        center = max(0, int(hint) - 1)
        lo = max(search_from, center - window_before)
        hi = min(total, center + window_after + 1)
        candidates: list[int] = []

        for p in range(lo, hi):
            if p < search_from:
                continue
            text = pages_text[p]
            if is_toc_like_page(text, targets):
                continue
            if is_unit_summary_page(text):
                continue
            if is_lesson_supplement_section_page(text):
                continue
            if page_likely_lesson_start(text, tgt):
                candidates.append(p)

        if candidates:
            found = min(candidates, key=lambda p: abs(p - center))
        else:
            found = None
            for p in range(max(search_from, center), hi):
                text = pages_text[p]
                if is_toc_like_page(text, targets):
                    continue
                if is_unit_summary_page(text):
                    continue
                if is_lesson_supplement_section_page(text):
                    continue
                if page_likely_lesson_start(text, tgt):
                    found = p
                    break

        starts.append(found if found is not None else center)
        search_from = starts[-1] + 1

    return starts


def find_lesson_starts_with_hints(
    pages_text: list[str] | LazyPageTexts,
    targets: list[dict[str, Any]],
    hints_1based: list[int | None],
    *,
    skip_pages: int = 3,
    window_half: int = 12,
) -> list[int | None]:
    """
    扫描版：用定稿/目录页码提示在窗口内 lazy OCR 定位，避免整册识别。
    窗口内未命中时回退到提示页本身（定稿表已人工校对）。
    """
    total = len(pages_text)
    starts: list[int | None] = [None] * len(targets)
    search_from = max(0, skip_pages)

    if not any(hints_1based):
        scan_n = min(_TOC_SCAN_PAGES, total)
        toc_sample = [pages_text[p] for p in range(scan_n)]
        search_from = compute_content_search_start(
            toc_sample, targets, skip_pages=skip_pages
        )

    for idx, tgt in enumerate(targets):
        hint = hints_1based[idx] if idx < len(hints_1based) else None
        found: int | None = None

        if hint is not None and hint >= 1:
            center = hint - 1
            lo = max(search_from, center - window_half)
            hi = min(total, center + window_half + 1)
            scan_pages = sorted({center, *range(lo, hi)})
            for p in scan_pages:
                if p < search_from:
                    continue
                if _try_match_lesson_start_at_page(pages_text[p], tgt, targets):
                    found = p
                    break
            if found is None:
                found = center

        if found is None:
            for p in range(search_from, total):
                if _try_match_lesson_start_at_page(pages_text[p], tgt, targets):
                    found = p
                    break

        starts[idx] = found
        if found is not None:
            search_from = found + 1

    _fill_unmatched_lesson_starts(pages_text, targets, starts, skip_pages=skip_pages)
    return starts


def _fill_unmatched_lesson_starts(
    pages_text: list[str],
    targets: list[dict[str, Any]],
    starts: list[int | None],
    *,
    skip_pages: int,
) -> None:
    """在前后已定位课节之间的空隙内，为未命中的课再搜一次。"""
    total = len(pages_text)
    content_start = compute_content_search_start(pages_text, targets, skip_pages=skip_pages)

    for i, tgt in enumerate(targets):
        if starts[i] is not None:
            continue
        lo = content_start
        for j in range(i - 1, -1, -1):
            if starts[j] is not None:
                lo = starts[j] + 1
                break
        hi = total
        for j in range(i + 1, len(targets)):
            if starts[j] is not None:
                hi = starts[j]
                break
        # 专项等无节号课正文常在更后：邻接上限过紧时放宽到文末
        search_hi = hi if hi > lo else total
        if search_hi <= lo and lo < total:
            search_hi = total
        found: int | None = None
        for p in range(lo, search_hi if search_hi > lo else total):
            if is_toc_like_page(pages_text[p], targets):
                continue
            if _try_match_lesson_start_at_page(pages_text[p], tgt, targets):
                found = p
                break
            if page_likely_lesson_start(pages_text[p], tgt):
                found = p
                break
        if found is None and search_hi < total:
            for p in range(search_hi, total):
                if is_toc_like_page(pages_text[p], targets):
                    continue
                if _try_match_lesson_start_at_page(pages_text[p], tgt, targets):
                    found = p
                    break
                if page_likely_lesson_start(pages_text[p], tgt):
                    found = p
                    break
        if found is not None:
            starts[i] = found
            _log.info(
                "空隙回填定位 %s → PDF p%d",
                tgt.get("lesson_name"),
                found + 1,
            )


def compute_page_ends(
    starts: list[int | None],
    total_pages: int,
    pages_text: list[str] | None = None,
    targets: list[dict[str, Any]] | None = None,
) -> list[int | None]:
    """0-based 起始页 → 1-based page_end。

    结束页取「物理页上紧随其后的下一课起始 − 1」。
    苏教等目录把专项插在中间、正文却在册末时，不能按目录顺序取下一课，
    否则会出现 end < start 被整课丢弃。
    """
    ends: list[int | None] = [None] * len(starts)
    for vi, start in enumerate(starts):
        if start is None:
            continue
        later = [s for s in starts if s is not None and s > start]
        end_0 = (min(later) - 1) if later else (total_pages - 1)

        if pages_text and targets:
            end_0 = _trim_end_at_next_lesson_header(
                pages_text, targets, vi, start, end_0
            )

        if end_0 is not None and end_0 >= start:
            ends[vi] = end_0 + 1  # 1-based
    return ends


def _trim_end_at_next_lesson_header(
    pages_text: list[str],
    targets: list[dict[str, Any]],
    current_idx: int,
    start_0: int,
    end_0: int,
) -> int:
    """若范围内出现后续课的标题行，提前截断（补救下一课起始未单独匹配）。"""
    for p in range(start_0 + 1, min(end_0 + 1, len(pages_text))):
        for j in range(current_idx + 1, len(targets)):
            if has_lesson_header(pages_text[p], targets[j]):
                return p - 1
    return end_0


def _line_is_unit_banner(line: str) -> bool:
    """行首「第 X 单元/章」横幅（非正文里顺带出现的字样）。"""
    s = line.strip()
    m = re.match(r"^(第[一二三四五六七八九十百千\d]+(?:单元|章))(.*)$", s)
    if not m:
        return False
    rest = (m.group(2) or "").strip()
    if not rest:
        return True
    if rest.startswith("小结") or "单元小结" in rest or "本章小结" in rest:
        return False
    if rest in ("末页", "末") or rest.endswith("末页"):
        return False
    return len(rest) < 48


def is_unit_intro_divider_page(page_text: str) -> bool:
    """单元/章导航扉页：页首「第 X 单元|章」+ 主题，非课节正文。"""
    raw = page_text or ""
    if len(raw.strip()) < 4:
        return False
    for line in raw.replace("\r", "\n").split("\n"):
        if _line_is_unit_banner(line):
            return True
    full_norm = norm_text(raw)
    m = _UNIT_HEADER_RE.search(full_norm)
    if m is None:
        return False
    if m.start() > 80:
        return False
    tail = full_norm[m.end(): m.end() + 8]
    if tail.startswith("小结") or tail.startswith("末页"):
        return False
    return True


def trim_unit_boundary_pages(
    pages_text: list[str],
    targets: list[dict[str, Any]],
    starts: list[int | None],
    ends: list[int | None],
    *,
    trim_leading: bool = True,
    trim_trailing: bool = True,
) -> tuple[list[int | None], list[int | None]]:
    """
    去掉误入课节范围的单元导航/扉页。

    - 每单元最后一节：若末页为下一单元扉页则去掉末页
    - 每单元第一节（非全书第一节）：若首页为单元扉页则去掉首页
    - 任意课节：若末页为单元扉页（如第二单元导航误入植物角末尾）则去掉末页
    """
    if not pages_text:
        return starts, ends

    starts_out = list(starts)
    ends_out = list(ends)
    n = len(targets)

    unit_last: dict[int, int] = {}
    unit_first: dict[int, int] = {}
    for i, tgt in enumerate(targets):
        un = int(tgt.get("unit_no") or 0)
        unit_last[un] = i
        unit_first.setdefault(un, i)

    min_unit = min(unit_first) if unit_first else 0

    def trim_trailing_divider(idx: int) -> None:
        start_0 = starts_out[idx]
        end_1 = ends_out[idx]
        if start_0 is None or end_1 is None:
            return
        if start_0 < 0 or start_0 >= len(pages_text):
            return
        if end_1 <= start_0 + 1:
            return
        last_0 = end_1 - 1
        if last_0 < 0 or last_0 >= len(pages_text):
            return
        if not is_unit_intro_divider_page(pages_text[last_0]):
            return
        ends_out[idx] = end_1 - 1
        _log.info(
            "去掉课节末尾单元扉页 PDF p%d：%s",
            end_1,
            targets[idx].get("lesson_name"),
        )
        # 下一课紧挨原末页之后时，把裁掉的扉页并入下一课（数学章导入等）
        if idx + 1 < n:
            next_start = starts_out[idx + 1]
            if next_start is not None and next_start == end_1:
                starts_out[idx + 1] = end_1 - 1
                _log.info(
                    "将单元/章扉页 PDF p%d 并入下一课：%s",
                    end_1,
                    targets[idx + 1].get("lesson_name"),
                )

    def trim_leading_divider(idx: int) -> None:
        start_0 = starts_out[idx]
        end_1 = ends_out[idx]
        if start_0 is None or end_1 is None:
            return
        if start_0 < 0 or start_0 >= len(pages_text):
            return
        if end_1 <= start_0 + 1:
            return
        if not is_unit_intro_divider_page(pages_text[start_0]):
            return
        if matches_unit_intro_lesson_start(pages_text[start_0], targets[idx]):
            return
        if matches_chapter_intro_for_unit(pages_text[start_0], targets[idx]):
            return
        starts_out[idx] = start_0 + 1
        _log.info(
            "去掉课节开头单元扉页 PDF p%d：%s",
            start_0 + 1,
            targets[idx].get("lesson_name"),
        )

    for idx in range(n):
        if trim_trailing:
            trim_trailing_divider(idx)

    for un, idx in unit_last.items():
        if trim_trailing:
            trim_trailing_divider(idx)

    for un, idx in unit_first.items():
        if un <= min_unit:
            continue
        if trim_leading:
            trim_leading_divider(idx)

    return starts_out, ends_out


def trim_leading_unit_summary_pages(
    pages_text: list[str],
    targets: list[dict[str, Any]],
    starts: list[int | None],
    ends: list[int | None],
) -> list[int | None]:
    """去掉课节范围开头的上一单元「单元小结」页（常见于跨单元首课起始偏早）。"""
    if not pages_text:
        return starts

    starts_out = list(starts)
    for i, tgt in enumerate(targets):
        if tgt.get("is_summary"):
            continue
        start_0 = starts_out[i]
        end_1 = ends[i]
        if start_0 is None or end_1 is None:
            continue
        while end_1 > start_0 + 1 and start_0 < len(pages_text):
            if not is_unit_summary_page(pages_text[start_0]):
                break
            _log.info(
                "去掉课节开头单元小结页 PDF p%d：%s",
                start_0 + 1,
                tgt.get("lesson_name"),
            )
            start_0 += 1
        starts_out[i] = start_0
    return starts_out


def is_unit_summary_page(page_text: str) -> bool:
    """页为单元小结（标题在页首附近，非正文顺带提及）。"""
    raw = (page_text or "").strip()
    if len(raw) < 2:
        return False
    norm = norm_text(raw)
    if "单元小结" not in norm:
        return False
    idx = norm.find("单元小结")
    if idx <= min(200, max(40, len(norm) // 3)):
        return True
    for line in raw.replace("\r", "\n").split("\n"):
        ln = norm_text(line.strip())
        if ln == "单元小结" or ln.startswith("单元小结"):
            return True
    return False


def trim_unit_summary_pages(
    pages_text: list[str],
    targets: list[dict[str, Any]],
    starts: list[int | None],
    ends: list[int | None],
) -> list[int | None]:
    """去掉课节范围末尾的单元小结页（非「单元小结」课时本身）。"""
    if not pages_text:
        return ends

    ends_out = list(ends)
    for i, tgt in enumerate(targets):
        if tgt.get("is_summary"):
            continue
        start_0 = starts[i]
        end_1 = ends_out[i]
        if start_0 is None or end_1 is None:
            continue
        min_end = start_0 + 1
        while end_1 > min_end:
            last_0 = end_1 - 1
            if last_0 < 0 or last_0 >= len(pages_text):
                break
            if not is_unit_summary_page(pages_text[last_0]):
                break
            _log.info(
                "去掉课节末尾单元小结页 PDF p%d：%s",
                end_1,
                tgt.get("lesson_name"),
            )
            end_1 -= 1
        ends_out[i] = end_1
    return ends_out


def trim_unit_summary_pages_from_pdf(
    pdf_path: Path,
    targets: list[dict[str, Any]],
    starts: list[int | None],
    ends: list[int | None],
    *,
    ocr_dpi: int = 96,
) -> list[int | None]:
    """扫描版：从 PDF 末页识别并去掉单元小结页。"""
    import fitz

    ends_out = list(ends)
    with fitz.open(str(pdf_path)) as doc:
        for i, tgt in enumerate(targets):
            if tgt.get("is_summary"):
                continue
            start_0 = starts[i]
            end_1 = ends_out[i]
            if start_0 is None or end_1 is None:
                continue
            min_end = start_0 + 1
            while end_1 > min_end:
                last_0 = end_1 - 1
                if last_0 < 0 or last_0 >= len(doc):
                    break
                text = (doc[last_0].get_text("text") or "").strip()
                if len(text) < 6:
                    text = ocr_single_page_text(
                        pdf_path, last_0, dpi=ocr_dpi, doc=doc
                    )
                if not is_unit_summary_page(text):
                    break
                _log.info(
                    "去掉课节末尾单元小结页 PDF p%d：%s",
                    end_1,
                    tgt.get("lesson_name"),
                )
                end_1 -= 1
            ends_out[i] = end_1
    return ends_out


def is_appendix_start_page(page_text: str) -> bool:
    """附录正文起始页（非目录行、非课内提及）。"""
    import re

    toc_leaders = re.compile(r"\.{2,}|…{2,}|·{4,}")
    raw = page_text or ""
    if len(raw.strip()) < 2:
        return False
    for line in raw.replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s:
            continue
        if toc_leaders.search(s):
            continue
        if re.match(r"^附录[一二三四五六七八九十\d].*\s+\d{1,3}\s*$", s):
            continue
        ln = norm_text(s)
        if ln.startswith("附录") or ln == "附录":
            return True
        break
    return False


def is_postscript_start_page(page_text: str) -> bool:
    """后记 / 编者的话 等尾页（非课节正文）。"""
    import re

    toc_leaders = re.compile(r"\.{2,}|…{2,}|·{4,}")
    raw = page_text or ""
    if len(raw.strip()) < 2:
        return False
    for line in raw.replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s:
            continue
        if toc_leaders.search(s):
            continue
        if re.match(r"^(后记|编者的话|编后记)\s+\d{1,3}\s*$", s):
            continue
        ln = norm_text(s)
        if ln == "后记" or ln.startswith("后记"):
            return True
        if ln in ("编者的话", "编后记") or ln.startswith("编者的话"):
            return True
        if len(s) <= 16 and s == "后记":
            return True
        break
    return False


def find_backmatter_page_index(
    pages_text: list[str],
    *,
    min_page_0: int = 0,
) -> int | None:
    """附录/后记等尾页起始 0-based；取最靠前的一页（附录通常在后记之前）。"""
    if not pages_text:
        return None
    tail = max(40, int(len(pages_text) * 0.25))
    rear_start = max(min_page_0, len(pages_text) - tail)
    earliest: int | None = None
    for i in range(len(pages_text) - 1, rear_start - 1, -1):
        if is_appendix_start_page(pages_text[i]) or is_postscript_start_page(
            pages_text[i]
        ):
            earliest = i
    return earliest


def find_postscript_page_index(
    pages_text: list[str],
    *,
    min_page_0: int = 0,
) -> int | None:
    """附录/后记起始页 0-based（兼容旧名）。"""
    return find_backmatter_page_index(pages_text, min_page_0=min_page_0)


def trim_backmatter_pages(
    pages_text: list[str],
    starts: list[int | None],
    ends: list[int | None],
) -> list[int | None]:
    """附录/后记及其后页面不纳入课节范围（通常截断全书最后一节末尾）。"""
    psi = find_backmatter_page_index(pages_text)
    if psi is None:
        return ends

    max_end_1 = psi  # 1-based：末页为尾页前一页
    ends_out = list(ends)
    for i, (start_0, end_1) in enumerate(zip(starts, ends_out)):
        if start_0 is None or end_1 is None:
            continue
        page_start_1 = start_0 + 1
        if end_1 > max_end_1 and page_start_1 <= max_end_1:
            _log.info(
                "尾页在 PDF p%d，%s 末页 %d→%d",
                psi + 1,
                i,
                end_1,
                max_end_1,
            )
            ends_out[i] = max_end_1
    return ends_out


# 新教材扫描版：整页仅中心水印时墨迹占比极低（实测 p102 ≈ 0.006）
_WATERMARK_ONLY_TOTAL_DARK = 0.012
_WATERMARK_ONLY_OUTSIDE_DARK = 0.006
_WATERMARK_TEXT_HINTS = ("学而思", "科学专用", "传递追责", "专育")


def _dark_ratios_from_gray_array(arr: Any) -> tuple[float, float]:
    import numpy as np

    h, w = arr.shape
    dark = arr < 200
    total_dark = float(dark.sum() / dark.size)
    cy, cx = h // 2, w // 2
    rh, rw = int(h * 0.22), int(w * 0.22)
    y0, y1 = max(0, cy - rh), min(h, cy + rh)
    x0, x1 = max(0, cx - rw), min(w, cx + rw)
    outside = np.ones_like(dark, dtype=bool)
    outside[y0:y1, x0:x1] = False
    outside_dark = float((dark & outside).sum() / outside.sum())
    return total_dark, outside_dark


def page_dark_ratios_from_pdf(pdf_path: Path, page_0: int, *, dpi: int = 72) -> tuple[float, float]:
    import fitz
    import numpy as np

    doc = fitz.open(str(pdf_path))
    try:
        page = doc.load_page(page_0)
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        return _dark_ratios_from_gray_array(arr)
    finally:
        doc.close()


def _dark_ratios_from_fitz_page(page: Any, *, dpi: int = 72) -> tuple[float, float]:
    import fitz
    import numpy as np

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return _dark_ratios_from_gray_array(arr)


def is_blank_page(page_text: str) -> bool:
    """几乎无文本层的页面（常见末页空白页）。"""
    t = (page_text or "").strip()
    if not t:
        return True
    # 只用去空白长度：norm_text 会剥拼音/拉丁，易把「p71」误判空白
    compact = re.sub(r"\s+", "", t)
    return len(compact) < 3


def _is_watermark_only_text(page_text: str) -> bool:
    """OCR 仅剩中心水印字样（新教材每页都有水印，仅水印≈空白）。"""
    raw = (page_text or "").strip()
    if not raw:
        return True
    compact = re.sub(r"\s+", "", raw)
    if len(compact) < 3:
        return True
    n = norm_text(raw)
    if len(n) < 3:
        # 可见字符被归一化剥掉（页码字母等），不应当水印空白
        return False
    if len(n) >= 30:
        return False
    if any(hint in raw for hint in _WATERMARK_TEXT_HINTS) and len(n) < 25:
        return True
    return False


def is_watermark_only_blank_page(
    *,
    page_text: str | None = None,
    dark_ratios: tuple[float, float] | None = None,
    pdf_path: Path | None = None,
    page_0: int | None = None,
) -> bool:
    """
    新教材扫描页：除中心水印外无正文。
    依据墨迹占比（水印页全页 dark≈0.006，正文页通常 >0.05）。
    """
    if page_text is not None and _is_watermark_only_text(page_text):
        return True
    if dark_ratios is None and pdf_path is not None and page_0 is not None:
        dark_ratios = page_dark_ratios_from_pdf(pdf_path, page_0)
    if dark_ratios is None:
        return False
    total_dark, outside_dark = dark_ratios
    if total_dark < _WATERMARK_ONLY_TOTAL_DARK:
        return True
    if total_dark < 0.02 and outside_dark < _WATERMARK_ONLY_OUTSIDE_DARK:
        return True
    return False


def is_effectively_blank_page(
    page_text: str | None = None,
    *,
    pdf_path: Path | None = None,
    page_0: int | None = None,
    dark_ratios: tuple[float, float] | None = None,
) -> bool:
    """空白页或仅水印页（用于末页修剪）。"""
    if page_text is not None and not is_blank_page(page_text):
        return _is_watermark_only_text(page_text)

    if dark_ratios is None and pdf_path is not None and page_0 is not None:
        dark_ratios = page_dark_ratios_from_pdf(pdf_path, page_0)
    if dark_ratios is not None:
        return is_watermark_only_blank_page(dark_ratios=dark_ratios)

    return is_blank_page(page_text or "")


def trim_trailing_blank_pages(
    pages_text: list[str],
    starts: list[int | None],
    ends: list[int | None],
    *,
    pdf_path: Path | None = None,
) -> list[int | None]:
    """去掉课节范围末尾的连续空白页 / 仅水印页。"""
    ends_out = list(ends)
    for i, (start_0, end_1) in enumerate(zip(starts, ends_out)):
        if start_0 is None or end_1 is None:
            continue
        min_end = start_0 + 1  # 至少保留 1 页
        while end_1 > min_end:
            last_0 = end_1 - 1
            if last_0 < 0:
                break
            text = ""
            if pages_text and last_0 < len(pages_text):
                text = pages_text[last_0]
            if not is_effectively_blank_page(
                text,
                pdf_path=pdf_path,
                page_0=last_0,
            ):
                break
            _log.info("去掉课节末尾空白/水印页 PDF p%d", end_1)
            end_1 -= 1
        ends_out[i] = end_1
    return ends_out


def trim_trailing_blank_pages_from_pdf(
    pdf_path: Path,
    starts: list[int | None],
    ends: list[int | None],
) -> list[int | None]:
    """基于 PDF 像素检测，批量修剪各课末尾空白/仅水印页。"""
    import fitz

    ends_out = list(ends)
    with fitz.open(str(pdf_path)) as doc:
        for i, (start_0, end_1) in enumerate(zip(starts, ends_out)):
            if start_0 is None or end_1 is None:
                continue
            min_end = start_0 + 1
            while end_1 > min_end:
                last_0 = end_1 - 1
                if last_0 < 0 or last_0 >= len(doc):
                    break
                text = (doc[last_0].get_text("text") or "").strip()
                ratios = _dark_ratios_from_fitz_page(doc[last_0])
                if not is_effectively_blank_page(text, dark_ratios=ratios):
                    break
                _log.info("去掉课节末尾空白/水印页 PDF p%d", end_1)
                end_1 -= 1
            ends_out[i] = end_1
    return ends_out


def trim_trailing_blank_end_from_pdf(
    pdf_path: Path,
    page_start_1: int,
    page_end_1: int,
) -> int:
    """从 PDF 末尾去掉连续空白/仅水印页，返回调整后的 1-based page_end。"""
    import fitz

    end = page_end_1
    with fitz.open(str(pdf_path)) as doc:
        while end > page_start_1:
            last_0 = end - 1
            if last_0 < 0 or last_0 >= len(doc):
                break
            text = (doc[last_0].get_text("text") or "").strip()
            ratios = _dark_ratios_from_fitz_page(doc[last_0])
            if not is_effectively_blank_page(text, dark_ratios=ratios):
                break
            _log.info("去掉课节末尾空白/水印页 PDF p%d", end)
            end -= 1
    return end


def body_text_for_range(
    pages_text: list[str],
    page_start: int,
    page_end: int,
) -> str:
    """page_start/end 为 1-based。"""
    if page_start < 1 or page_end < page_start:
        return ""
    parts: list[str] = []
    for p in range(page_start - 1, min(page_end, len(pages_text))):
        t = (pages_text[p] or "").strip()
        if t:
            parts.append(t)
    return "\n\n".join(parts)
