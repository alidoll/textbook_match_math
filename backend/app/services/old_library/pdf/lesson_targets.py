"""从 DB lessons 构建 PDF 页匹配目标。"""
from __future__ import annotations

import re
from typing import Any

from ....models import Lesson
from ....parsers.text_norm import (
    is_numbered_lesson_no,
    lesson_seq_int,
    norm_lesson_title,
    norm_text,
    strip_lesson_seq,
)


def build_lesson_targets(lessons: list[Lesson]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for les in lessons:
        label = (
            f"{les.lesson_no} {les.lesson_name}".strip()
            if is_numbered_lesson_no(les.lesson_no or "")
            else (les.lesson_name or "").strip()
        )
        is_summary = "小结" in (les.lesson_name or "")
        keys: list[str] = []

        if is_summary:
            keys.append(norm_text("单元小结"))
            m = re.match(r"^(第[一二三四五六七八九十百千\d]+单元)", les.unit_title or "")
            if m:
                keys.append(norm_text(m.group(1) + "小结"))
        else:
            keys.append(norm_lesson_title(label))
            keys.append(norm_lesson_title(les.lesson_name or ""))
            stripped = strip_lesson_seq(les.lesson_name or "")
            if stripped:
                keys.append(norm_lesson_title(stripped))
            circled = None
            if les.lesson_no and is_numbered_lesson_no(les.lesson_no):
                n = lesson_seq_int(les.lesson_no)
                if n is not None and 1 <= n <= 10:
                    circled = "①②③④⑤⑥⑦⑧⑨⑩"[n - 1]
            if circled and les.lesson_name:
                keys.append(norm_lesson_title(f"{circled}{les.lesson_name}"))
                if stripped:
                    keys.append(norm_lesson_title(f"{circled}{stripped}"))
            for sep in ("—", "——", "–", "-", "一", "--"):
                raw_name = les.lesson_name or ""
                if sep in raw_name:
                    prefix = raw_name.split(sep, 1)[0].strip()
                    if len(prefix) > 4:
                        keys.append(norm_lesson_title(prefix))

        # 去重保序
        seen: set[str] = set()
        deduped: list[str] = []
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                deduped.append(k)

        targets.append(
            {
                "lesson_id": les.id,
                "lesson_uid": les.lesson_uid,
                "lesson_no": les.lesson_no,
                "lesson_name": les.lesson_name,
                "unit_title": les.unit_title,
                "unit_no": les.unit_no,
                "keys": deduped,
                "is_summary": is_summary,
            }
        )
    return targets
