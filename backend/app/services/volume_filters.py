"""过滤 pytest / 开发误写入库的测试册次（不在业务列表展示）。"""
from __future__ import annotations

from sqlalchemy import ColumnElement, not_, or_

from ..models import Volume


def is_test_volume_code(code: str | None) -> bool:
    """真册次形如 XK-2X-NEW；测试册次为 T-* 或 *-SEED*。"""
    if not code:
        return False
    c = code.strip()
    upper = c.upper()
    if upper.startswith("OLD-SEED") or upper.startswith("NEW-SEED"):
        return True
    return c.startswith("T-")


def sqlalchemy_exclude_test_volumes() -> ColumnElement[bool]:
    """用于 Volume.query.filter(...) 排除测试册次。"""
    return not_(
        or_(
            Volume.volume_code.like("OLD-SEED%"),
            Volume.volume_code.like("NEW-SEED%"),
            Volume.volume_code.like("T-%"),
        )
    )
