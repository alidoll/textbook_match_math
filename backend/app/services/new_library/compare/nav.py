"""compare 页上下文导航：返回建块、本册列表、上一课/下一课。"""
from __future__ import annotations

from ....models import Volume
from ...lesson_lookup import get_lesson_by_uid
from ..compare_results import list_compare_queue_for_volume


def build_compare_nav(*, new_lesson_uid: str) -> dict:
    new_les = get_lesson_by_uid(new_lesson_uid, book_type="new")
    vol = Volume.query.get(new_les.volume_id)
    if not vol:
        raise ValueError("未找到册次")

    queue = list_compare_queue_for_volume(vol.id)
    uids = [les.lesson_uid for les in queue]
    idx = uids.index(new_les.lesson_uid) if new_les.lesson_uid in uids else -1

    def _lesson_brief(les):
        if not les:
            return None
        return {
            "lesson_uid": les.lesson_uid,
            "lesson_label": f"{les.lesson_no} {les.lesson_name}",
            "compare_url": f"/new-library/lessons/{les.lesson_uid}/compare",
        }

    prev_les = queue[idx - 1] if idx > 0 else None
    next_les = queue[idx + 1] if idx >= 0 and idx + 1 < len(queue) else None

    vol_code = vol.volume_code
    return {
        "ok": True,
        "volume_code": vol_code,
        "volume_title": vol.display_title or vol_code,
        "lesson_uid": new_les.lesson_uid,
        "in_queue": idx >= 0,
        "queue_index": idx,
        "queue_size": len(queue),
        "annotate_url": f"/new-library/lessons/{new_les.lesson_uid}/annotate",
        "workbench_url": f"/new-library/compare-results?volume={vol_code}",
        "prev_lesson": _lesson_brief(prev_les),
        "next_lesson": _lesson_brief(next_les),
    }
