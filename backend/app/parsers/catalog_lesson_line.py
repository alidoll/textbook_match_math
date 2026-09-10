"""教材目录「课时行」识别：课题、整理与提升、实验活动、跨学科实践活动等。"""
from __future__ import annotations

import re
from typing import Any

from .benchmark_xlsx import cell_str

# 整行即目录条目的固定标题（化学等人教版单元末 / 数学冀教章末常见）
SPECIAL_CATALOG_TITLES = frozenset(
    {
        "整理与提升",
        "复习与提高",
        "回顾与反思",
        "复习题",
    }
)

_CJK_SPACED = re.compile(r"([\u4e00-\u9fff])[\s\u00a0\u3000]+(?=[\u4e00-\u9fff])")
_PAGE_SUFFIX_RE = re.compile(r"(?:[·●…．.．_—\-]+\s*|\s+)\d{1,3}\s*$")

_SPECIAL_PREFIX_RE = re.compile(
    r"^(?:"
    r"课题\s*(\d+)\s*"
    r"|实验活动\s*(\d+)\s*"
    r"|跨学科实践活动\s*(\d+)\s*"
    r"|综合实践活动\s*(\d+)\s*"
    r"|微项目\s*(\d+)\s*"
    r")(.*)$",
    re.DOTALL,
)

_NUMERIC_LESSON_RE = re.compile(r"^(\d{1,2})\s*[*＊]?\s*[\u4e00-\u9fff「」《》A-Za-z]")
# 初中数学目录：12.1 分式
_MATH_SECTION_RE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\s*[*＊]?\s+[\u4e00-\u9fff「」《》A-Za-z]"
)
# 数学章内栏目（读一读 / 数学活动 / 主题探究）
_MATH_BLOCK_RE = re.compile(
    r"^(?:读一读|数学活动|主题探究)(?:\s|$|：|:|·|（|\()"
)
# 小学语文目录常见非数字条目前缀
_YUWEN_BLOCK_RE = re.compile(
    r"^(?:口语交际|习作|语文园地|快乐读书吧)(?:\s|$|：|:|·)"
)
# 语文书末附录表（目录有独立行；对比按表名对齐）
_YUWEN_APPENDIX_TABLE_RE = re.compile(r"^(?:识字表|写字表|词语表)(?:\s|$|¹|1)")
# 古诗/现代诗分篇、习作例文等副标题（无课号）
_YUWEN_SUBTITLE_RE = re.compile(
    r"^(?:例文\s+)?"
    r"[\u4e00-\u9fff《》「」『』·、A-Za-z0-9]{2,24}$"
)


def is_yuwen_subtitle_line(text: str) -> bool:
    """语文目录缩进副标题：诗名、例文篇名（非独立课号行）。"""
    t = normalize_catalog_lesson_line(text)
    if not t or t in ("例文", "阅读", "口语交际", "习作", "语文园地"):
        return False
    if is_catalog_lesson_line(t) or looks_like_exercise_line(t):
        return False
    if _YUWEN_BLOCK_RE.match(t) or _YUWEN_APPENDIX_TABLE_RE.match(t):
        return False
    if re.match(r"^\d", t):
        return False
    return bool(_YUWEN_SUBTITLE_RE.match(t))


def strip_yuwen_subtitle_label(text: str) -> str:
    t = normalize_catalog_lesson_line(text)
    t = re.sub(r"^例文\s+", "", t).strip()
    return t


# 目录粘连行：LLM/OCR 偶发把相邻多课挤进同一 lesson 字段
_GLUED_BLOCK_SPLIT_RE = re.compile(
    r"(?=(?:口语交际|习作例文|习作|语文园地|快乐读书吧))"
)
_GLUED_NUM_SPLIT_RE = re.compile(
    # 勿在「16.5」的小数点后切开（数学节号）；仅在独立课号前切开
    r"(?=(?<![\d.])\d{1,2}\s*[*＊]?\s*(?=[\u4e00-\u9fff《「]{2,}))"
)
_TRAILING_ORPHAN_PAGE_RE = re.compile(r"(?<=[\u4e00-\u9fff])\d{1,3}$")


def split_glued_catalog_lesson_line(text: str) -> list[str]:
    """若一行内出现多个课型锚点（数字课/口语交际/习作/园地/读书吧），拆成多行。

    仅作结构修复，不针对具体课文名硬编码。
    """
    t = normalize_catalog_lesson_line(text)
    if not t or len(t) < 16:
        return [t] if t else []

    # 「实验活动1 标题」「课题1 标题」等：序号属于栏目，勿按裸数字课号切开
    prefix_anchors = list(
        re.finditer(
            r"(?:课题|实验活动|跨学科实践活动|综合实践活动|微项目)\s*\d+",
            t,
        )
    )
    if len(prefix_anchors) == 1 and prefix_anchors[0].start() == 0:
        return [t]

    def _split_by(pattern: re.Pattern[str], s: str) -> list[str]:
        parts = [p.strip() for p in pattern.split(s) if p and p.strip()]
        return parts if parts else ([s] if s else [])

    # 先按栏目课切开，再在片段内按数字课号切
    stage1 = _split_by(_GLUED_BLOCK_SPLIT_RE, t)
    pieces: list[str] = []
    for chunk in stage1:
        sub = _split_by(_GLUED_NUM_SPLIT_RE, chunk)
        if len(sub) >= 2:
            pieces.extend(sub)
        else:
            pieces.append(chunk)

    cleaned: list[str] = []
    for p in pieces:
        p = _TRAILING_ORPHAN_PAGE_RE.sub("", p).strip()
        if p and len(p) >= 2:
            cleaned.append(p)

    # 至少拆出 2 段且总长说明确有粘连
    if len(cleaned) < 2:
        return [t]
    if sum(len(p) for p in cleaned) < len(t) * 0.5:
        return [t]
    return cleaned


def expand_glued_catalog_rows(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """展开粘连课时行；unit/页码沿用原行。"""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        lesson = str(row.get("lesson") or "")
        parts = split_glued_catalog_lesson_line(lesson)
        if len(parts) <= 1:
            out.append(dict(row))
            continue
        for part in parts:
            nr = dict(row)
            nr["lesson"] = part
            # 拆开后页码不可靠，留给后续页码匹配；避免多课共用同一页
            if "pdf_page" in nr:
                nr["pdf_page"] = None
            out.append(nr)
    return out

_EXERCISE_MARKERS = (
    "下列",
    "判断",
    "填空",
    "简答",
    "计算",
    "连线",
    "选择",
    "举例",
    "观察你",
    "简要说明",
    "哪些是",
    "该词语",
    "该说法",
    "是否正确",
)


def looks_like_exercise_line(text: str) -> bool:
    """正文习题/复习题（非目录条目）。"""
    t = normalize_catalog_lesson_line(text)
    if not t:
        return False
    # 数学目录「12.1 …」「复习题」「回顾与反思」等不是正文题干
    if match_special_catalog_title(t) or _MATH_SECTION_RE.match(t) or _MATH_BLOCK_RE.match(t):
        return False
    if re.search(r"[（(]\s*[）)]", t):
        return True
    if t.count("？") + t.count("?") >= 1:
        return True
    if any(m in t for m in _EXERCISE_MARKERS):
        return True
    # 裸「N 标题」且过长，多为整理/复习页里的 numbered 习题
    if re.match(r"^\d+\s+", t) and not re.match(r"^课题\s*\d+", t) and len(t) > 36:
        return True
    return False


def collapse_catalog_line_spacing(text: str) -> str:
    """「整 理 与 提 升」→「整理与提升」。"""
    if not text:
        return text
    t = text
    for _ in range(40):
        nt = _CJK_SPACED.sub(r"\1", t)
        if nt == t:
            break
        t = nt
    return re.sub(r"\s+", " ", t).strip()


def match_special_catalog_title(text: str) -> str | None:
    s = collapse_catalog_line_spacing(text or "")
    s = _PAGE_SUFFIX_RE.sub("", s).strip()
    if s in SPECIAL_CATALOG_TITLES:
        return s
    compact = re.sub(r"\s+", "", s)
    for title in SPECIAL_CATALOG_TITLES:
        if compact == title:
            return title
    return None


def normalize_catalog_whitespace(text: str) -> str:
    """仅压缩空白，保留「第一单元 走进化学世界」等词间空格。"""
    return re.sub(r"[ \t\u00a0\u3000]+", " ", (text or "").strip())


def normalize_catalog_lesson_line(text: str) -> str:
    s = normalize_catalog_whitespace(text)
    s = _PAGE_SUFFIX_RE.sub("", s).strip()
    matched = match_special_catalog_title(s)
    if matched:
        return matched
    return s


def catalog_row_key(row: dict[str, Any]) -> tuple[str, str]:
    unit = normalize_catalog_whitespace(str(row.get("unit") or ""))
    lesson = normalize_catalog_lesson_line(str(row.get("lesson") or ""))
    return unit, lesson


def catalog_semantic_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """同一课时在不同来源下文案不同（「第一单元」vs「第一单元 走进化学世界」）时仍视为同一行。"""
    from .text_norm import norm_text, strip_lesson_seq
    from .unit_title import unit_no_from_title

    unit = normalize_catalog_whitespace(str(row.get("unit") or ""))
    u_no = unit_no_from_title(unit)
    unit_key: Any = u_no if u_no is not None else unit

    lesson_raw = normalize_catalog_lesson_line(str(row.get("lesson") or ""))
    if not lesson_raw:
        return (unit_key, "")

    special = match_special_catalog_title(lesson_raw)
    if special:
        return (unit_key, "S", special)

    m = re.match(r"^实验活动\s*(\d+)", lesson_raw)
    if m:
        return (unit_key, "E", int(m.group(1)))

    m = re.match(r"^跨学科实践活动\s*(\d+)", lesson_raw)
    if m:
        return (unit_key, "X", int(m.group(1)))

    m = re.match(r"^综合实践活动\s*(\d+)", lesson_raw)
    if m:
        return (unit_key, "G", int(m.group(1)))

    m = re.match(r"^微项目\s*(\d+)", lesson_raw)
    if m:
        return (unit_key, "W", int(m.group(1)))

    # 数学 12.1：须整段课号参与去重，不能只取「12」否则 12.1/12.2 会被并成一行
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\b", lesson_raw)
    if m:
        return (unit_key, "M", int(m.group(1)), int(m.group(2)))

    m = re.match(r"^(读一读|数学活动)\s*(.*)", lesson_raw)
    if m:
        return (unit_key, "MB", m.group(1), norm_text(m.group(2) or ""))

    m = re.match(r"^主题探究\s*[（(]([^）)]+)[）)]", lesson_raw)
    if m:
        return (unit_key, "MP", m.group(1).strip())

    m = re.match(r"^主题探究\s*(.*)", lesson_raw)
    if m:
        return (unit_key, "MP", norm_text(m.group(1) or ""))

    # (?!\.\d) 避免「12.1」被当成裸课号 12
    m = re.match(r"^(?:课题\s*)?(\d+)(?!\.\d)\s*\*?\s*(.*)", lesson_raw)
    if m:
        return (unit_key, "N", int(m.group(1)))

    if _YUWEN_BLOCK_RE.match(lesson_raw):
        head = re.match(r"^(口语交际|习作|语文园地|快乐读书吧)", lesson_raw)
        kind = head.group(1) if head else "园地"
        rest = norm_text(strip_lesson_seq(lesson_raw[len(kind) :]))
        return (unit_key, "Y", kind, rest)

    return (unit_key, "R", lesson_raw)


def _catalog_row_score(row: dict[str, Any]) -> int:
    lesson = str(row.get("lesson") or "")
    if looks_like_exercise_line(lesson):
        return -1000
    score = 0
    if re.match(r"^课题\s*\d+", lesson):
        score += 200
    if match_special_catalog_title(lesson):
        score += 200
    if re.match(r"^实验活动\s*\d+", lesson):
        score += 150
    if re.match(r"^跨学科", lesson):
        score += 150
    score += min(len(str(row.get("unit") or "")), 40)
    score -= max(0, len(lesson) - 32)
    return score


def _merge_catalog_row(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    """合并重复行：优先目录标题，其次更完整的单元名。"""
    if len(str(incoming.get("unit") or "")) > len(str(existing.get("unit") or "")):
        existing["unit"] = incoming["unit"]
    pick = incoming if _catalog_row_score(incoming) > _catalog_row_score(existing) else existing
    existing["lesson"] = pick["lesson"]


def merge_catalog_rows(
    primary: list[dict[str, Any]],
    supplemental: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并目录行；按语义去重，supplemental 补充 primary 缺失项。"""
    merged: list[dict[str, Any]] = []
    key_to_idx: dict[tuple[Any, ...], int] = {}

    def upsert(row: dict[str, Any]) -> None:
        key = catalog_semantic_key(row)
        if key[1] == "":
            return
        if key in key_to_idx:
            _merge_catalog_row(merged[key_to_idx[key]], row)
            return
        key_to_idx[key] = len(merged)
        merged.append(dict(row))

    for row in primary:
        upsert(row)
    for row in supplemental:
        upsert(row)
    return merged


def dedupe_catalog_rows(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按语义去重，保留首次出现顺序。"""
    return merge_catalog_rows(catalog, [])


def is_catalog_lesson_line(text: str) -> bool:
    """是否为应入库的目录课时行（不限于「课题 N」）。"""
    t = normalize_catalog_lesson_line(text)
    if len(t) < 2 or len(t) > 120:
        return False
    if looks_like_exercise_line(t):
        return False
    if match_special_catalog_title(t) or "单元小结" in t:
        return True
    if _SPECIAL_PREFIX_RE.match(t):
        return True
    if _MATH_SECTION_RE.match(t):
        return True
    if _MATH_BLOCK_RE.match(t):
        return True
    if _NUMERIC_LESSON_RE.match(t):
        return True
    if _YUWEN_BLOCK_RE.match(t):
        return True
    if _YUWEN_APPENDIX_TABLE_RE.match(t):
        return True
    if re.match(r"^课题\s*\d+", t):
        return True
    return False


def catalog_lesson_sort_key(lesson_raw: str) -> tuple[int, int, str]:
    """单元内排序：数字课题 → 整理/复习 → 实验/跨学科 → 单元小结。"""
    s = cell_str(lesson_raw)
    if "单元小结" in s:
        return (3, 9999, s)
    if match_special_catalog_title(s) or "单元小结" in s:
        order = {
            "整理与提升": 10,
            "复习与提高": 20,
            "回顾与反思": 70,
            "复习题": 80,
        }.get(match_special_catalog_title(s) or "", 15)
        return (2, order, s)
    m = re.match(r"^实验活动\s*(\d+)", s)
    if m:
        return (2, 30 + int(m.group(1)), s)
    m = re.match(r"^跨学科实践活动\s*(\d+)", s)
    if m:
        return (2, 40 + int(m.group(1)), s)
    m = re.match(r"^综合实践活动\s*(\d+)", s)
    if m:
        return (2, 50 + int(m.group(1)), s)
    m = re.match(r"^微项目\s*(\d+)", s)
    if m:
        return (2, 60 + int(m.group(1)), s)
    m = re.match(r"^(读一读|数学活动|主题探究)", s)
    if m:
        return (2, 65, s)
    m = re.match(r"^课题\s*(\d+)", s)
    if m:
        return (0, int(m.group(1)), s)
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\b", s)
    if m:
        return (0, int(m.group(1)) * 100 + int(m.group(2)), s)
    m = re.match(r"^(\d+)", s)
    if m:
        return (0, int(m.group(1)), s)
    return (2, 0, s)


def parse_catalog_lesson_fields(
    lesson_raw: str,
    *,
    fallback_no: int,
) -> tuple[str, str, int]:
    """解析为 (lesson_no, lesson_name, sort_order)。"""
    raw = normalize_catalog_lesson_line(lesson_raw)
    if not raw:
        raise ValueError("空课时名")
    if "单元小结" in raw:
        return "0", "单元小结", 99990

    matched = match_special_catalog_title(raw)
    if matched:
        order = {
            "整理与提升": 9010,
            "复习与提高": 9020,
            "回顾与反思": 9070,
            "复习题": 9080,
        }[matched]
        return matched, "", order

    m = re.match(r"^课题\s*(\d+)\s*(.*)", raw)
    if m:
        no, name = m.group(1), m.group(2).strip()
        # 与教材目录一致：展示「课题1 标题」，不用裸序号
        return f"课题{no}", name or raw, int(no) * 10

    m = re.match(r"^实验活动\s*(\d+)\s*(.*)", raw)
    if m:
        no, name = m.group(1), m.group(2).strip()
        label = f"实验活动{no}"
        return label, name, 9030 + int(no)

    m = re.match(r"^跨学科实践活动\s*(\d+)\s*(.*)", raw, re.DOTALL)
    if m:
        no, name = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
        label = f"跨学科实践活动{no}"
        return label, name, 9040 + int(no)

    m = re.match(r"^综合实践活动\s*(\d+)\s*(.*)", raw)
    if m:
        no, name = m.group(1), m.group(2).strip()
        label = f"综合实践活动{no}"
        return label, name, 9050 + int(no)

    m = re.match(r"^微项目\s*(\d+)\s*(.*)", raw)
    if m:
        no, name = m.group(1), m.group(2).strip()
        label = f"微项目{no}"
        return label, name, 9060 + int(no)

    # 数学：12.1 分式（须先于裸「12 xxx」，避免课号被截成 12）
    m = re.match(r"^(\d{1,2}\.\d{1,2})\s*[*＊]?\s*(.*)", raw)
    if m:
        no, name = m.group(1), m.group(2).strip()
        major, minor = no.split(".", 1)
        return no, name or raw, int(major) * 100 + int(minor)

    m = re.match(r"^(读一读|数学活动)\s*(.*)", raw)
    if m:
        label, name = m.group(1), m.group(2).strip()
        # 同一章可有多条「读一读/数学活动」，课号带标题以免入库去重误杀
        no = f"{label} {name}".strip() if name else label
        return no, name, 9065 if label == "读一读" else 9066

    m = re.match(r"^主题探究\s*[（(]([^）)]+)[）)]\s*(.*)", raw)
    if m:
        tag, name = m.group(1).strip(), m.group(2).strip()
        label = f"主题探究（{tag}）"
        return label, name, 9068

    m = re.match(r"^主题探究\s*(.*)", raw)
    if m:
        name = m.group(1).strip()
        no = f"主题探究 {name}".strip() if name else "主题探究"
        return no, name, 9068

    m = re.match(r"^(\d+)\s*\*\s*(.*)", raw)
    if m:
        no, name = m.group(1), m.group(2).strip()
        return f"{no}*", name or raw, int(no) * 10 + 1

    m = re.match(r"^(\d+)\s+(.*)", raw)
    if m:
        no, name = m.group(1), m.group(2).strip()
        return no, name or raw, int(no) * 10

    if _YUWEN_BLOCK_RE.match(raw):
        # 口语交际 / 习作 / 语文园地 / 快乐读书吧：lesson_no 用整行前缀，便于排序
        head = re.match(r"^(口语交际|习作|语文园地|快乐读书吧)", raw)
        label = head.group(1) if head else raw[:4]
        name = raw[len(label) :].strip()
        if not name or name == label:
            name = ""
        order_map = {
            "口语交际": 8000,
            "习作": 8100,
            "语文园地": 8200,
            "快乐读书吧": 8300,
        }
        return label, name, order_map.get(label, 8400)

    m = _YUWEN_APPENDIX_TABLE_RE.match(raw)
    if m:
        label = raw[:3]  # 识字表/写字表/词语表
        for name in ("识字表", "写字表", "词语表"):
            if raw.startswith(name):
                label = name
                break
        order_map = {"识字表": 9100, "写字表": 9110, "词语表": 9120}
        return label, "", order_map.get(label, 9130)

    fb = fallback_no * 10
    return str(fallback_no), raw, fb


def normalize_huaxue_lesson_for_storage(
    *,
    unit_no: int,
    lesson_no: str,
    lesson_name: str,
) -> tuple[str, str, str]:
    """
    化学人教版入库字段整理（所见即所得，不改写栏目类型）。

    返回 (lesson_no, lesson_name, uid_lesson_no)。
    - 绪论：右栏仅标题（左栏已是「绪论」）
    - 已是「课题N / 实验活动N / …」前缀：原样保留
    - 裸数字课号：不再自动补「课题」（避免「6 实验活动」→「课题6 实验活动」）
    """
    no = str(lesson_no or "").strip()
    name = str(lesson_name or "").strip()
    if unit_no == 0 and re.fullmatch(r"\d+", no):
        return "", name, no
    return no, name, no or name


_TOC_SKIP_SUBSTR = (
    "前言",
    "编者",
    "版权",
    "定价",
    "出版社",
    "附录",
    "后记",
    "ISBN",
    "责任编辑",
    "义务教育",
    "教科书",
    "人民教育出版社",
    "课程教材研究所",
    "侵权必究",
    "网址",
    "邮编",
)

_UNIT_HEADER_RE = re.compile(
    r"^(第[一二三四五六七八九十\d]+单元)\s*(.*)$",
)
_XULUN_HEADER_RE = re.compile(r"^绪论(?:\s+(.*))?$")
_PAGE_NUM_RE = re.compile(r"^\d{1,3}$")


def _toc_line_skip(line: str) -> bool:
    s = normalize_catalog_whitespace(line)
    if not s or s == "目录":
        return True
    if _PAGE_NUM_RE.match(s):
        return True
    return any(k in s for k in _TOC_SKIP_SUBSTR)


def _parse_unit_header(line: str) -> tuple[str, str | None] | None:
    """识别单元/绪论标题行；返回 (unit_title, inline_lesson|None)。"""
    s = normalize_catalog_whitespace(line)
    m_x = _XULUN_HEADER_RE.match(s)
    if m_x:
        inline = (m_x.group(1) or "").strip() or None
        return "绪论", inline
    m_u = _UNIT_HEADER_RE.match(s)
    if m_u:
        head, tail = m_u.group(1), (m_u.group(2) or "").strip()
        unit_title = f"{head} {tail}".strip() if tail else head
        return unit_title, None
    return None


_TOC_END_MARKERS = (
    "课程标准中列出的",
    "供选择；文中图标",
    "针对每课题梳理",
    "建构主题大概念",
    "学业要求设计的习题",
)


def _toc_section_ended(line: str) -> bool:
    return any(m in line for m in _TOC_END_MARKERS)


def _looks_like_toc_title_line(line: str) -> bool:
    """OCR 常把「目录」拆成「目 录」或粘连噪声。"""
    s = normalize_catalog_whitespace(line)
    if not s:
        return False
    if s == "目录":
        return True
    compact = re.sub(r"[\s\u00a0\u3000·…．.]+", "", s)
    return compact == "目录"


def _looks_like_title_continuation(line: str) -> bool:
    """目录长标题换行后的续行（非新课题、非页码、非单元标题）。"""
    t = normalize_catalog_lesson_line(line)
    if len(t) < 1 or len(t) > 80:
        return False
    if _PAGE_NUM_RE.match(t):
        return False
    if _looks_like_toc_title_line(t):
        return False
    if _toc_section_ended(t) or _toc_line_skip(t):
        return False
    if _parse_unit_header(t) is not None:
        return False
    if is_catalog_lesson_line(t):
        return False
    # 常见续行：以「的/与/和/并/及」开头，或无课题前缀的短片段
    if re.match(r"^[的与和并及、，,]", t):
        return True
    if re.match(r"^(?:课题|实验活动|跨学科|综合实践|整理与提升|复习与提高|\d+\s)", t):
        return False
    return True


def _merge_wrapped_lesson_title(all_lines: list[str], idx: int, lesson_line: str) -> tuple[str, int]:
    """
    把后续续行拼进课题标题。
    返回 (完整标题, 最后消费的行下标)。
    """
    merged = normalize_catalog_lesson_line(lesson_line)
    last = idx
    for off in range(1, 4):
        j = idx + off
        if j >= len(all_lines):
            break
        nxt_raw = normalize_catalog_whitespace(all_lines[j])
        if not nxt_raw:
            continue
        if not _looks_like_title_continuation(nxt_raw):
            break
        piece = normalize_catalog_lesson_line(nxt_raw)
        # 中文目录续行直接拼接，不插空格
        merged = f"{merged}{piece}"
        last = j
    return normalize_catalog_lesson_line(merged), last


def parse_chemistry_toc_lines(all_lines: list[str]) -> list[dict[str, Any]]:
    """人教版化学等：按 PDF 目录行顺序解析（绪论 / 课题 / 整理与提升 / 复习与提高 / 实验活动）。"""
    catalog: list[dict[str, Any]] = []
    current_unit: str | None = None
    pending_prefix: str | None = None
    toc_active = False
    skip_until = -1

    for idx, raw in enumerate(all_lines):
        if idx <= skip_until:
            continue
        line = normalize_catalog_whitespace(raw)
        if _looks_like_toc_title_line(line):
            toc_active = True
            pending_prefix = None
            continue
        header = _parse_unit_header(line) if not toc_active else None
        if header is not None and not toc_active:
            toc_active = True
            current_unit, inline = header
            pending_prefix = None
            if inline and current_unit == "绪论":
                lesson = (
                    inline
                    if re.match(r"^(?:课题\s*)?\d+\s+", inline)
                    else f"1 {inline}"
                )
                catalog.append({"unit": "绪论", "lesson": normalize_catalog_lesson_line(lesson)})
            continue
        if not toc_active:
            continue
        if _toc_section_ended(line):
            current_unit = None
            pending_prefix = None
            toc_active = False
            continue
        if _toc_line_skip(line):
            pending_prefix = None
            continue

        header = _parse_unit_header(line)
        if header is not None:
            current_unit, inline = header
            pending_prefix = None
            if inline and current_unit == "绪论":
                lesson = (
                    inline
                    if re.match(r"^(?:课题\s*)?\d+\s+", inline)
                    else f"1 {inline}"
                )
                catalog.append({"unit": "绪论", "lesson": normalize_catalog_lesson_line(lesson)})
            continue

        if not current_unit:
            continue

        if pending_prefix:
            merged = normalize_catalog_lesson_line(f"{pending_prefix} {line}")
            if is_catalog_lesson_line(merged):
                merged, last = _merge_wrapped_lesson_title(all_lines, idx, merged)
                catalog.append({"unit": current_unit, "lesson": merged})
                skip_until = last
                pending_prefix = None
                continue
            pending_prefix = None

        lesson_line = normalize_catalog_lesson_line(line)
        if re.match(r"^(?:跨学科实践活动|实验活动)\s*\d+\s*$", lesson_line):
            pending_prefix = lesson_line
            continue

        if is_catalog_lesson_line(lesson_line):
            # 绪论仅含开篇课，不含单元末板块
            if current_unit == "绪论" and lesson_line in SPECIAL_CATALOG_TITLES:
                continue
            merged, last = _merge_wrapped_lesson_title(all_lines, idx, lesson_line)
            catalog.append({"unit": current_unit, "lesson": merged})
            skip_until = last

    return catalog


def _toc_match_key(lesson_line: str) -> str:
    from .text_norm import norm_text, strip_lesson_seq

    s = normalize_catalog_lesson_line(lesson_line)
    special = match_special_catalog_title(s)
    if special:
        return special
    m = re.match(r"^跨学科实践活动\s*(\d+)", s)
    if m:
        return f"跨学科{m.group(1)}"
    m = re.match(r"^实验活动\s*(\d+)", s)
    if m:
        return f"实验活动{m.group(1)}"
    m = re.match(r"^综合实践活动\s*(\d+)", s)
    if m:
        return f"综合实践{m.group(1)}"
    mk = norm_text(strip_lesson_seq(s))
    return mk or s


def _read_following_page_no(
    all_lines: list[str],
    idx: int,
    *,
    max_lookahead: int = 5,
) -> int | None:
    """
    读取目录行后的印刷页码。
    化学等人教版 PDF 常把长标题拆成两行，页码在续行之后单独占一行。
    """
    for off in range(1, max_lookahead + 1):
        j = idx + off
        if j >= len(all_lines):
            break
        nxt = normalize_catalog_whitespace(all_lines[j])
        if _PAGE_NUM_RE.match(nxt):
            pg = int(nxt)
            if 1 <= pg <= 400:
                return pg
        if _parse_unit_header(nxt) is not None:
            break
        if is_catalog_lesson_line(nxt):
            break
    return None


def _append_toc_entry(
    entries: list[Any],
    *,
    unit: str,
    lesson_line: str,
    page_1: int | None,
) -> None:
    from .pdf_toc import TocEntry

    if page_1 is None:
        return
    mk = _toc_match_key(lesson_line)
    if not mk or len(mk) < 2:
        return
    entries.append(
        TocEntry(
            unit_norm=normalize_catalog_whitespace(unit)[:48],
            title_raw=normalize_catalog_lesson_line(lesson_line),
            page_1=page_1,
            match_key=mk,
        )
    )


def parse_chemistry_toc_entries(all_lines: list[str]) -> list[Any]:
    """人教版化学目录：带印刷页码的 TocEntry（供划分页码 / 目录偏移缓存）。"""
    entries: list[Any] = []
    current_unit: str | None = None
    pending_prefix: str | None = None
    toc_active = False
    skip_until = -1

    for idx, raw in enumerate(all_lines):
        if idx <= skip_until:
            continue
        line = normalize_catalog_whitespace(raw)
        if line == "目录":
            toc_active = True
            pending_prefix = None
            continue
        if not toc_active:
            continue
        if _toc_section_ended(line):
            current_unit = None
            pending_prefix = None
            toc_active = False
            continue
        if _toc_line_skip(line):
            pending_prefix = None
            continue

        header = _parse_unit_header(line)
        if header is not None:
            current_unit, inline = header
            pending_prefix = None
            if inline and current_unit == "绪论":
                lesson = (
                    inline
                    if re.match(r"^(?:课题\s*)?\d+\s+", inline)
                    else f"1 {inline}"
                )
                lesson_line = normalize_catalog_lesson_line(lesson)
                _append_toc_entry(
                    entries,
                    unit="绪论",
                    lesson_line=lesson_line,
                    page_1=_read_following_page_no(all_lines, idx),
                )
            continue

        if not current_unit:
            continue

        if pending_prefix:
            merged = normalize_catalog_lesson_line(f"{pending_prefix} {line}")
            if is_catalog_lesson_line(merged):
                merged, last = _merge_wrapped_lesson_title(all_lines, idx, merged)
                _append_toc_entry(
                    entries,
                    unit=current_unit,
                    lesson_line=merged,
                    page_1=_read_following_page_no(all_lines, last),
                )
                skip_until = last
                pending_prefix = None
                continue
            pending_prefix = None

        lesson_line = normalize_catalog_lesson_line(line)
        if re.match(r"^(?:跨学科实践活动|实验活动)\s*\d+\s*$", lesson_line):
            pending_prefix = lesson_line
            continue

        if is_catalog_lesson_line(lesson_line):
            if current_unit == "绪论" and lesson_line in SPECIAL_CATALOG_TITLES:
                continue
            merged, last = _merge_wrapped_lesson_title(all_lines, idx, lesson_line)
            _append_toc_entry(
                entries,
                unit=current_unit,
                lesson_line=merged,
                page_1=_read_following_page_no(all_lines, last),
            )
            skip_until = last

    return entries
