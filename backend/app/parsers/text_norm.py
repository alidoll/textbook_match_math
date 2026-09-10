"""教材 PDF 文本归一化（页匹配用）。"""
from __future__ import annotations

import re
import unicodedata

# 各类连接号/破折号（Excel/Word/PDF 编码不一，匹配前统一剥除）
_DASH_AND_SPACE_RE = re.compile(
    r"[\s\u3000"
    r"\-‐‑‒–—―－"  # ASCII hyphen + Unicode dash block + 全角减号
    r"\u2010\u2011\u2012\u2013\u2014\u2015\uff0d"
    r"·\u00b7\u2022]+"
)
# 苏教等教材 PDF 常在汉字间插入拼音（今 jīn 天 / 2tiānqì天气），
# 部分字体用 IPA（ɑ ɡ）代替 a/g，去空格后需一并剥除
_PINYIN_OR_LATIN_RUN_RE = re.compile(
    r"(?:[A-Za-zÀ-ÿĀ-žāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜüǖǘǚǜńňǹḿ]|[\u0250-\u02AF])+",
    re.UNICODE,
)


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFC", (s or "").strip())
    s = s.lower()
    # 教材标题常见「哺（bǔ）乳动物」式拼音，旧规则盖不住 ǔ、ī 等扩展拉丁字母
    s = re.sub(r'[（(][^）)]*[）)]', '', s)
    s = re.sub(r'\([^)]*\)', '', s)
    s = _DASH_AND_SPACE_RE.sub('', s)
    s = _PINYIN_OR_LATIN_RUN_RE.sub('', s)
    s = re.sub(
        r'["""\u201c\u201d\'\'`「」『』【】《》〈〉\u2018\u2019\uff02]+',
        '',
        s,
    )
    return s


def norm_lesson_title(s: str) -> str:
    """
    课题名专用归一化：在 norm_text 基础上处理基准库常见笔误。

    Excel/编码常把连接号落成「一」，如「从垃圾说起一资源的回收和利用」。
    """
    t = norm_text(s)
    if re.search(r"[\u4e00-\u9fff]一[\u4e00-\u9fff]", t):
        t = re.sub(r"(?<=[\u4e00-\u9fff])一(?=[\u4e00-\u9fff])", "", t)
    return t


def strip_lesson_seq(s: str) -> str:
    t = (s or "").strip()
    t = re.sub(r"^课题\s*\d+\s*", "", t).strip()
    return re.sub(r"^\d+\s*", "", t).strip()


def lesson_seq_int(lesson_no: str) -> int | None:
    """从课号提取正整数序号：'1' / '课题1' → 1；非节号 → None。"""
    s = (lesson_no or "").strip()
    if not s:
        return None
    if s.isdigit():
        n = int(s)
        return n if n > 0 else None
    m = re.match(r"^课题\s*(\d+)$", s)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None
    return None


def is_numbered_lesson_no(lesson_no: str) -> bool:
    """正整数节号才算「有节」；含化学「课题N」；空、0、非数字均视为无节号。"""
    return lesson_seq_int(lesson_no) is not None
