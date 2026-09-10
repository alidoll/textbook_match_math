"""课件 ZIP 文件名 ↔ 课时自动匹配。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from ....parsers.text_norm import norm_lesson_title, norm_text, strip_lesson_seq

_NOISE_RE = re.compile(
    r"^(课件|courseware|ppt|slides?|zip|压缩包)[_\-\s]*",
    re.IGNORECASE,
)
_UNIT_LESSON_RE = re.compile(
    r"(?<!\d)(?P<u>\d{1,2})\s*[-_.、]\s*(?P<l>\d{1,2})(?!\d)"
)


@dataclass(frozen=True)
class CoursewareMatchCandidate:
    lesson_uid: str
    unit_title: str
    lesson_no: str
    lesson_name: str
    old_course_id: str | None
    score: float
    reason: str


def _stem_variants(filename: str) -> list[str]:
    stem = Path(filename).name
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    raw = stem.strip()
    cleaned = _NOISE_RE.sub("", raw).strip()
    spaced = re.sub(r"[_\-]+", " ", cleaned)
    spaced = re.sub(r"\s+", " ", spaced).strip()
    out: list[str] = []
    for s in (raw, cleaned, spaced):
        if s and s not in out:
            out.append(s)
    return out


def _lesson_rows_from_volume(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for les in lessons:
        rows.append(
            {
                "lesson_uid": str(les["lesson_uid"]),
                "unit_no": int(les.get("unit_no") or 0),
                "unit_title": str(les.get("unit_title") or ""),
                "lesson_no": str(les.get("lesson_no") or "").strip(),
                "lesson_name": str(les.get("lesson_name") or "").strip(),
                "old_course_id": (les.get("old_course_id") or None),
            }
        )
    return rows


def score_courseware_filename(filename: str, lesson: dict[str, Any]) -> tuple[float, str]:
    """返回 (0–1 得分, 匹配依据简述)。"""
    fname = Path(filename).name
    fname_lower = fname.lower()
    best = 0.0
    reason = ""

    course_id = (lesson.get("old_course_id") or "").strip()
    if course_id and course_id.lower() in fname_lower:
        return 1.0, f"课件 id {course_id}"

    unit_no = int(lesson.get("unit_no") or 0)
    lesson_no = str(lesson.get("lesson_no") or "").strip()
    lesson_name = str(lesson.get("lesson_name") or "").strip()
    unit_title = str(lesson.get("unit_title") or "").strip()

    if unit_no and lesson_no and lesson_no.isdigit():
        m = _UNIT_LESSON_RE.search(fname)
        if m and int(m.group("u")) == unit_no and int(m.group("l")) == int(lesson_no):
            return 0.98, f"单元课序 {unit_no}-{lesson_no}"

        for sep in ("-", "_", ".", "、"):
            token = f"{unit_no}{sep}{lesson_no}"
            if token in fname_lower.replace(" ", ""):
                best = max(best, 0.96)
                reason = reason or f"序号 {unit_no}{sep}{lesson_no}"

    label = f"{lesson_no} {lesson_name}".strip() if lesson_no else lesson_name
    name_norm = norm_lesson_title(strip_lesson_seq(lesson_name) or lesson_name)
    label_norm = norm_lesson_title(label)
    unit_norm = norm_text(unit_title)
    combined_norm = norm_lesson_title(f"{unit_title} {label}")

    for variant in _stem_variants(fname):
        vnorm = norm_lesson_title(variant)
        if not vnorm:
            continue
        if name_norm and (name_norm in vnorm or vnorm in name_norm):
            best = max(best, 0.94)
            reason = reason or "课名一致"
        if label_norm:
            ratio = fuzz.token_set_ratio(vnorm, label_norm) / 100.0
            if ratio > best:
                best = ratio
                reason = "课序号+名称"
        if combined_norm:
            ratio = fuzz.token_set_ratio(vnorm, combined_norm) / 100.0
            if ratio > best:
                best = ratio
                reason = "单元+课时"
        if unit_norm and unit_norm in norm_text(variant):
            name_ratio = fuzz.partial_ratio(vnorm, name_norm) / 100.0 if name_norm else 0.0
            if name_ratio > best:
                best = name_ratio
                reason = reason or "单元+课名片段"

    if lesson_no and lesson_no.isdigit():
        prefix = f"{lesson_no}"
        for variant in _stem_variants(fname):
            if re.match(rf"^{re.escape(prefix)}\s*[\s_\-、.]", variant):
                name_ratio = fuzz.token_set_ratio(
                    norm_lesson_title(variant), name_norm
                ) / 100.0
                if name_ratio > best:
                    best = name_ratio
                    reason = reason or f"节号 {lesson_no}"

    return best, reason or "模糊匹配"


def rank_courseware_filename(
    filename: str,
    lessons: list[dict[str, Any]],
    *,
    top_n: int = 5,
) -> list[CoursewareMatchCandidate]:
    ranked: list[CoursewareMatchCandidate] = []
    for les in _lesson_rows_from_volume(lessons):
        score, reason = score_courseware_filename(filename, les)
        if score <= 0:
            continue
        ranked.append(
            CoursewareMatchCandidate(
                lesson_uid=les["lesson_uid"],
                unit_title=les["unit_title"],
                lesson_no=les["lesson_no"],
                lesson_name=les["lesson_name"],
                old_course_id=les["old_course_id"],
                score=round(score, 4),
                reason=reason,
            )
        )
    ranked.sort(key=lambda c: (-c.score, c.lesson_uid))
    return ranked[:top_n]


def assign_courseware_filenames(
    filenames: list[str],
    lessons: list[dict[str, Any]],
    *,
    min_score: float = 0.62,
) -> dict[str, Any]:
    """
    为多份 ZIP 分配课时（一对一）。
    返回 matches / unmatched_files / ambiguous_files。
    """
    lesson_rows = _lesson_rows_from_volume(lessons)
    uploadable = [r for r in lesson_rows if (r.get("old_course_id") or "").strip()]

    edges: list[tuple[float, int, str, dict[str, Any]]] = []
    for fi, filename in enumerate(filenames):
        for les in uploadable:
            score, reason = score_courseware_filename(filename, les)
            edges.append((score, fi, reason, les))

    edges.sort(key=lambda x: (-x[0], x[1], x[3]["lesson_uid"]))

    used_files: set[int] = set()
    used_lessons: set[str] = set()
    matches: list[dict[str, Any]] = []

    for score, fi, reason, les in edges:
        if fi in used_files or les["lesson_uid"] in used_lessons:
            continue
        if score < min_score:
            continue
        used_files.add(fi)
        used_lessons.add(les["lesson_uid"])
        matches.append(
            {
                "filename": filenames[fi],
                "lesson_uid": les["lesson_uid"],
                "unit_title": les["unit_title"],
                "lesson_no": les["lesson_no"],
                "lesson_name": les["lesson_name"],
                "old_course_id": les["old_course_id"],
                "score": round(score, 4),
                "reason": reason,
            }
        )

    unmatched: list[dict[str, Any]] = []
    for fi, filename in enumerate(filenames):
        if fi in used_files:
            continue
        top = rank_courseware_filename(filename, uploadable, top_n=3)
        unmatched.append(
            {
                "filename": filename,
                "suggestions": [
                    {
                        "lesson_uid": c.lesson_uid,
                        "unit_title": c.unit_title,
                        "lesson_no": c.lesson_no,
                        "lesson_name": c.lesson_name,
                        "score": c.score,
                        "reason": c.reason,
                    }
                    for c in top
                ],
            }
        )

    return {
        "matches": matches,
        "unmatched": unmatched,
        "min_score": min_score,
    }
