"""旧教材 PDF 文件名 ↔ 册次自动匹配（类似课件 ZIP 批量匹配）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from ....parsers.text_norm import norm_text

_CN_GRADE = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

# 文件名噪音：学科/渠道/扫描标记等
_NOISE_RE = re.compile(
    r"(小学)?科学|新教材|旧教材|教材|课本|扫描版?|学而思|专用|修订|电子版|"
    r"PDF|pdf|教科书|全册|合订",
    re.IGNORECASE,
)

# JR-5S-OLD / XK-3X-OLD / SJ-4S-NEW / XKDX-5S-DNEW
_VOLUME_CODE_RE = re.compile(
    r"(?P<code>[A-Za-z0-9]+)-(?P<grade>\d{1,2})(?P<sem>[SXsx])-"
    r"(?P<book>D?OLD|D?NEW|d?old|d?new|OLD|NEW|old|new)"
)

# 2上 / 2下 / 五年级上 / 五年级上册 / 五上 / 5年级上册 / 四年级-上册
_GRADE_TERM_PATTERNS = [
    re.compile(
        r"(?P<g>\d{1,2})\s*年级\s*(?P<t>上|下)\s*册?"
    ),
    re.compile(
        r"(?P<g>[一二三四五六七八九])\s*年级\s*(?P<t>上|下)\s*册?"
    ),
    re.compile(
        r"(?<!\d)(?P<g>\d{1,2})\s*[_\-·]?\s*(?P<t>上|下)(?!\s*册?\s*[年级课])"
    ),
    re.compile(
        r"(?P<g>[一二三四五六七八九])\s*(?P<t>上|下)(?!\s*册?\s*[年级课])"
    ),
]


@dataclass(frozen=True)
class TextbookPdfMatch:
    volume_code: str
    grade: int
    term: str
    score: float
    reason: str


def _stem(filename: str) -> str:
    name = Path(filename).name
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name.strip()


def _clean_stem(stem: str) -> str:
    s = stem.replace("【", " ").replace("】", " ").replace("[", " ").replace("]", " ")
    s = _NOISE_RE.sub(" ", s)
    s = re.sub(r"[_\-·.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_grade_term_from_filename(filename: str) -> tuple[int | None, str | None, str]:
    """从文件名解析 (grade, term, reason)；解析失败返回 (None, None, '')."""
    stem = _stem(filename)
    m = _VOLUME_CODE_RE.search(stem)
    if m:
        grade = int(m.group("grade"))
        term = "上" if m.group("sem").upper() == "S" else "下"
        return grade, term, f"册次码 {m.group(0).upper()}"

    cleaned = _clean_stem(stem)
    for pat in _GRADE_TERM_PATTERNS:
        m2 = pat.search(cleaned) or pat.search(stem)
        if not m2:
            continue
        g_raw = m2.group("g")
        grade = int(g_raw) if g_raw.isdigit() else _CN_GRADE.get(g_raw)
        if not grade:
            continue
        term = m2.group("t")
        return grade, term, f"识别 {g_raw}{term}"
    return None, None, ""


def edition_hint_in_filename(filename: str, edition_label: str, code_prefix: str) -> float:
    """文件名是否暗示该版本：1.0 明确命中，0.5 中性，0.0 明显是其他版本。"""
    stem = _stem(filename)
    text = stem + " " + _clean_stem(stem)
    label = (edition_label or "").strip()
    prefix = (code_prefix or "").strip().upper()
    norm = norm_text(text)

    # 明确册次码前缀
    m = _VOLUME_CODE_RE.search(stem)
    if m:
        return 1.0 if m.group("code").upper() == prefix else 0.0

    if label and label in text:
        return 1.0
    # 去掉「版」后的短名：冀人 / 湘科 / 苏教
    short = label.replace("版", "").replace("社", "")
    if short and len(short) >= 2 and short in text:
        return 0.95

    # 常见缩写
    aliases = {
        "冀人版": ["冀人", "jiren", "jr"],
        "湘科版": ["湘科", "xiangke", "xk"],
        "苏教版": ["苏教", "sujiao", "sj"],
        "粤教科版": ["粤教", "粤科", "yuejiao", "yj"],
        "大象社版": ["大象", "daxiang", "dx"],
        "教科版": ["教科", "jiaoke", "jk"],
        "人教鄂教版": ["人教鄂教", "鄂教", "renejiao", "re"],
        "青岛版": ["青岛", "qingdao", "qd"],
    }
    for alias in aliases.get(label, []):
        if alias.lower() in text.lower() or alias in text:
            return 0.9

    # 命中其他版本标签 → 扣分
    other_labels = [
        "冀人版",
        "湘科版",
        "苏教版",
        "粤教科版",
        "大象社版",
        "教科版",
        "人教鄂教版",
        "青岛版",
    ]
    for other in other_labels:
        if other == label:
            continue
        if other in text or other.replace("版", "") in text:
            return 0.0

    # 无版本线索：中性，仅靠年级学期时压分，避免跨版误绑
    return 0.55


def score_textbook_pdf_to_volume(
    filename: str,
    *,
    volume_code: str,
    grade: int,
    term: str,
    edition_label: str,
    code_prefix: str,
    display_title: str = "",
) -> tuple[float, str]:
    stem = _stem(filename)
    stem_u = stem.upper()
    code_u = (volume_code or "").upper()

    if code_u and (stem_u == code_u or stem_u.startswith(f"{code_u}.") or code_u in stem_u):
        return 1.0, f"文件名含 {volume_code}"

    g, t, gt_reason = parse_grade_term_from_filename(filename)
    if not g or not t:
        # 还能靠显示名模糊比
        title_norm = norm_text(display_title or f"{edition_label}{grade}年级{term}册")
        file_norm = norm_text(_clean_stem(stem))
        if title_norm and file_norm:
            ratio = fuzz.token_set_ratio(file_norm, title_norm) / 100.0
            if ratio >= 0.72:
                return ratio * 0.85, "标题模糊匹配"
        return 0.0, "未识别年级学期"

    if g != int(grade) or t != term:
        return 0.0, f"年级学期不符（文件={g}{t}）"

    edition_score = edition_hint_in_filename(filename, edition_label, code_prefix)
    if edition_score <= 0.0:
        return 0.0, "版本不符"

    # 年级学期命中 + 版本线索
    # 中性（无版本字样）压到阈值以下，强制用户选带版本名/册次码的文件，或手动指定
    if edition_score < 0.8:
        return 0.68, f"{gt_reason} · 缺版本线索（请文件名含版本或册次码）"

    base = 0.82
    if edition_score >= 0.9:
        base = 0.96
    elif edition_score >= 0.8:
        base = 0.92
    reason = gt_reason
    if edition_score >= 0.9:
        reason = f"{gt_reason} · 版本命中"
    return base, reason


def assign_textbook_pdfs(
    filenames: list[str],
    volumes: list[dict[str, Any]],
    *,
    min_score: float = 0.72,
) -> dict[str, Any]:
    """多份 PDF ↔ 多册一对一分配。"""
    names = [str(n).strip() for n in filenames if str(n).strip()]
    edges: list[tuple[float, int, str, dict[str, Any]]] = []
    for fi, filename in enumerate(names):
        for vol in volumes:
            score, reason = score_textbook_pdf_to_volume(
                filename,
                volume_code=str(vol.get("volume_code") or ""),
                grade=int(vol.get("grade") or 0),
                term=str(vol.get("term") or ""),
                edition_label=str(vol.get("edition_label") or vol.get("edition") or ""),
                code_prefix=str(vol.get("code_prefix") or ""),
                display_title=str(vol.get("display_title") or ""),
            )
            if score > 0:
                edges.append((score, fi, reason, vol))

    edges.sort(key=lambda x: (-x[0], x[1], str(x[3].get("volume_code") or "")))
    used_files: set[int] = set()
    used_vols: set[str] = set()
    matches: list[dict[str, Any]] = []

    for score, fi, reason, vol in edges:
        code = str(vol.get("volume_code") or "")
        if fi in used_files or code in used_vols:
            continue
        if score < min_score:
            continue
        used_files.add(fi)
        used_vols.add(code)
        matches.append(
            {
                "filename": names[fi],
                "volume_code": code,
                "grade": vol.get("grade"),
                "term": vol.get("term"),
                "display_title": vol.get("display_title"),
                "score": round(score, 4),
                "reason": reason,
            }
        )

    unmatched: list[dict[str, Any]] = []
    for fi, filename in enumerate(names):
        if fi in used_files:
            continue
        ranked = []
        for vol in volumes:
            score, reason = score_textbook_pdf_to_volume(
                filename,
                volume_code=str(vol.get("volume_code") or ""),
                grade=int(vol.get("grade") or 0),
                term=str(vol.get("term") or ""),
                edition_label=str(vol.get("edition_label") or vol.get("edition") or ""),
                code_prefix=str(vol.get("code_prefix") or ""),
                display_title=str(vol.get("display_title") or ""),
            )
            if score > 0:
                ranked.append(
                    {
                        "volume_code": vol.get("volume_code"),
                        "grade": vol.get("grade"),
                        "term": vol.get("term"),
                        "score": round(score, 4),
                        "reason": reason,
                    }
                )
        ranked.sort(key=lambda r: -r["score"])
        unmatched.append({"filename": filename, "suggestions": ranked[:3]})

    return {
        "matches": matches,
        "unmatched": unmatched,
        "min_score": min_score,
    }
