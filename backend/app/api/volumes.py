from __future__ import annotations

from flask import jsonify

from ..models import Lesson, Volume
from ..services.volume_filters import sqlalchemy_exclude_test_volumes
from . import api_bp


@api_bp.get("/volumes")
def list_volumes():
    rows = (
        Volume.query.filter(sqlalchemy_exclude_test_volumes())
        .order_by(Volume.created_at.desc())
        .all()
    )
    out = []
    for v in rows:
        lesson_count = Lesson.query.filter_by(volume_id=v.id).count()
        out.append(
            {
                "id": v.id,
                "volume_code": v.volume_code,
                "display_title": v.display_title,
                "edition": v.edition,
                "grade": v.grade,
                "semester": v.semester,
                "book_type": v.book_type,
                "lesson_count": lesson_count,
            }
        )
    return jsonify({"ok": True, "volumes": out})
