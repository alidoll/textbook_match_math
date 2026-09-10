from __future__ import annotations

import sys

from flask import jsonify
from sqlalchemy import text

from ..extensions import db
from ..models import Lesson, Volume
from ..services.volume_filters import sqlalchemy_exclude_test_volumes
from . import api_bp


@api_bp.get("/health")
def health():
  """数据库连通性检查。"""
  try:
    from ..services.textbook_diff.atom_compare import (
        _CACHE_VERSION,
        _TEXT_COMPARE_CACHE_VERSION,
    )

    # MySQL: SELECT VERSION(); SQLite: SELECT sqlite_version()
    if db.session.get_bind().dialect.name == "sqlite":
        row = db.session.execute(text("SELECT sqlite_version() AS v")).mappings().one()
    else:
        row = db.session.execute(text("SELECT VERSION() AS v")).mappings().one()
    volume_count = Volume.query.filter(sqlalchemy_exclude_test_volumes()).count()
    lesson_count = (
        Lesson.query.join(Volume, Lesson.volume_id == Volume.id)
        .filter(sqlalchemy_exclude_test_volumes())
        .count()
    )
    counts = {"volumes": volume_count, "lessons": lesson_count}
    return jsonify(
      {
        "ok": True,
        "mysql_version": row["v"],
        "counts": counts,
        "python": sys.executable,
        "in_venv": (".venv" in sys.executable.replace("\\", "/")),
        "atoms_cache_version": _CACHE_VERSION,
        "text_compare_version": _TEXT_COMPARE_CACHE_VERSION,
      }
    )
  except Exception as exc:
    return jsonify({"ok": False, "error": str(exc)}), 503
