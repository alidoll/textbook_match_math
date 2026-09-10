# -*- coding: utf-8 -*-
"""教材库册次副本码：{PREFIX}-{g}{S|X}-OLD 与 -OLD-v{n}（n≥2）。"""
from __future__ import annotations

import re

from ..old_library.edition_registry import EDITIONS, EditionDef, get_edition
from ..old_library.volume_codes import make_volume_code, normalize_term

_LIB_CODE_RE = re.compile(
    r"^([A-Z0-9]+)-(\d+)([SX])-(OLD|NEW)(?:-v(\d+))?$",
    re.I,
)


def parse_library_volume_code(volume_code: str) -> tuple[EditionDef, int, str, str, int]:
    """返回 (edition, grade, term, book_type, copy_version)。无 -v 后缀时 version=1。"""
    code = (volume_code or "").strip()
    m = _LIB_CODE_RE.match(code)
    if not m:
        raise ValueError(f"册次编码无效：{volume_code}")
    prefix, grade_s, sem, book_type, ver_s = m.groups()
    grade = int(grade_s)
    term = "上" if sem.upper() == "S" else "下"
    book = book_type.lower()
    version = int(ver_s) if ver_s else 1
    if version < 1:
        raise ValueError(f"册次编码无效：{volume_code}")
    matches = [ed for ed in EDITIONS.values() if ed.code_prefix == prefix.upper()]
    if len(matches) != 1:
        # 大小写：code_prefix 可能已是大写
        matches = [ed for ed in EDITIONS.values() if ed.code_prefix.upper() == prefix.upper()]
    if len(matches) != 1:
        raise ValueError(f"未识别的册次编码：{volume_code}")
    return matches[0], grade, term, book, version


def make_library_volume_code(
    edition: EditionDef,
    *,
    grade: int,
    term: str,
    book_type: str = "old",
    version: int = 1,
) -> str:
    base = make_volume_code(edition, grade=grade, term=term, book_type=book_type)
    ver = int(version or 1)
    if ver <= 1:
        return base
    return f"{base}-v{ver}"


def slot_code_prefix(edition: EditionDef, *, grade: int, term: str) -> str:
    """同册次格下所有副本的码前缀（含 base OLD）。"""
    return make_volume_code(edition, grade=grade, term=term, book_type="old")


def list_copy_codes_for_slot(edition: EditionDef, *, grade: int, term: str) -> list[str]:
    """查询 DB 中该格全部副本码（按 version 排序）。调用方在已有 app context 时用。"""
    from ...models import Volume

    base = slot_code_prefix(edition, grade=grade, term=term)
    # base 或 base-vN
    rows = (
        Volume.query.filter(
            Volume.book_type == "old",
            (Volume.volume_code == base) | (Volume.volume_code.like(f"{base}-v%")),
        )
        .order_by(Volume.created_at.asc(), Volume.volume_code.asc())
        .all()
    )
    return [v.volume_code for v in rows]


def next_copy_version(edition: EditionDef, *, grade: int, term: str) -> int:
    from ...models import Volume

    base = slot_code_prefix(edition, grade=grade, term=term)
    codes = [
        v.volume_code
        for v in Volume.query.filter(
            Volume.book_type == "old",
            (Volume.volume_code == base) | (Volume.volume_code.like(f"{base}-v%")),
        ).all()
    ]
    if not codes:
        return 1
    max_v = 1
    for c in codes:
        try:
            _, _, _, _, ver = parse_library_volume_code(c)
            max_v = max(max_v, ver)
        except ValueError:
            continue
    return max_v + 1


def default_copy_label(*, uploaded_day: str | None = None) -> str:
    from datetime import date

    day = uploaded_day or date.today().isoformat()
    return f"{day} · 教材副本"


def copy_version_from_volume_code(volume_code: str) -> int:
    """教材库 *-OLD / *-OLD-vN；非馆藏码返回 1。"""
    try:
        return parse_library_volume_code(volume_code)[4]
    except ValueError:
        return 1
