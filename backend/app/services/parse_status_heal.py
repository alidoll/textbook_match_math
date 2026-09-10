# -*- coding: utf-8 -*-
"""页码划分状态自愈：页码已齐但 status 卡在 processing 时标为 done。"""
from __future__ import annotations

import logging

from ..extensions import db
from ..models import Lesson, Volume
from ..query.lesson_order import order_lessons_query
from .lesson_filters import filter_master_class_lessons

_log = logging.getLogger(__name__)


def heal_stuck_parse_status(volume: Volume) -> bool:
    """
    若 parse_status=processing 且全部主课均有 page_start/page_end，则标为 done。
    避免后台线程崩溃/锁超时后 UI 永久转圈、批量脚本永久等待。
    返回是否发生了修复。
    """
    if not volume or (volume.parse_status or "") != "processing":
        return False
    lessons = filter_master_class_lessons(
        order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    )
    if not lessons:
        return False
    if not all(les.page_start and les.page_end for les in lessons):
        return False
    volume.parse_status = "done"
    volume.parse_error = None
    db.session.commit()
    _log.warning(
        "自愈卡住的划页状态 %s：页码已齐（%s 课）→ done",
        volume.volume_code,
        len(lessons),
    )
    return True
