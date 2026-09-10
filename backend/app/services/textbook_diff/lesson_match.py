"""教材对比册次内课时匹配（同版本·同年级·同学期）。"""
from __future__ import annotations

import re

from ...models import Lesson, Volume
from ...parsers.text_norm import norm_text
from ...query.lesson_order import order_lessons_query
from ..lesson_filters import filter_master_class_lessons, is_subtitle_lesson

# 每单元重复出现的栏目：必须按单元对齐，不能只按 lesson_no 全局唯一匹配
_RECURRING_ACTIVITY_KEYS = (
    "语文园地",
    "口语交际",
    "习作",
    "快乐读书吧",
)


def _lesson_rows(volume: Volume) -> list[Lesson]:
    cached = getattr(volume, "_cached_master_lessons", None)
    if cached is not None:
        return cached
    rows = [
        les
        for les in filter_master_class_lessons(
            order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
        )
        if not is_subtitle_lesson(les, subject=volume.subject)
    ]
    volume._cached_master_lessons = rows  # type: ignore[attr-defined]
    return rows


def _all_lesson_rows_for_pages(volume: Volume) -> list[Lesson]:
    """页码落点可命中全部课时行。"""
    return filter_master_class_lessons(
        order_lessons_query(Lesson.query.filter_by(volume_id=volume.id)).all()
    )


def _lesson_page_index(volume: Volume) -> list[tuple[int, int, Lesson]]:
    cached = getattr(volume, "_cached_lesson_page_index", None)
    if cached is not None:
        return cached
    index: list[tuple[int, int, Lesson]] = []
    for les in _all_lesson_rows_for_pages(volume):
        ps, pe = les.page_start, les.page_end
        if ps and pe and ps <= pe:
            index.append((int(ps), int(pe), les))
    index.sort(key=lambda x: x[0])
    volume._cached_lesson_page_index = index  # type: ignore[attr-defined]
    return index


def lesson_at_pdf_page(volume: Volume, pdf_page: int) -> dict | None:
    """根据 PDF 页码找到所属课时。"""
    if pdf_page < 1:
        return None
    for ps, pe, les in _lesson_page_index(volume):
        if ps <= pdf_page <= pe:
            return {
                "lesson_uid": les.lesson_uid,
                "lesson_no": les.lesson_no,
                "lesson_name": les.lesson_name,
                "unit_title": les.unit_title,
                "page_start": ps,
                "page_end": pe,
            }
        if pdf_page < ps:
            break
    return None


def _activity_label(les: Lesson) -> str:
    return f"{les.lesson_no or ''}{les.lesson_name or ''}"


def is_recurring_activity_lesson(les: Lesson) -> bool:
    label = _activity_label(les)
    return any(k in label for k in _RECURRING_ACTIVITY_KEYS)


def _title_tokens(les: Lesson) -> set[str]:
    """主标题 + 括号内副标题（诗名/例文），供粗分锚点。"""
    tokens: set[str] = set()
    m = re.search(r"（([^）]+)）", str(les.lesson_name or ""))
    if m:
        for part in re.split(r"[；;、,/]", m.group(1)):
            t = norm_text(part)
            if len(t) >= 2:
                tokens.add(t)
    base = re.sub(r"（[^）]*）", "", str(les.lesson_name or "")).strip()
    bt = norm_text(base)
    if len(bt) >= 2:
        tokens.add(bt)
    # 栏目课：lesson_name 常空，lesson_no 即「语文园地」
    no = norm_text(str(les.lesson_no or ""))
    if len(no) >= 2 and any(k in str(les.lesson_no or "") for k in _RECURRING_ACTIVITY_KEYS):
        tokens.add(no)
    return tokens


def _best_old_by_title(
    new_les: Lesson,
    old_lessons: list[Lesson],
    *,
    used_old: set[str],
) -> Lesson | None:
    new_tok = _title_tokens(new_les)
    if not new_tok:
        return None
    best: Lesson | None = None
    best_score = 0
    for old in old_lessons:
        if old.lesson_uid in used_old:
            continue
        old_tok = _title_tokens(old)
        score = len(new_tok & old_tok)
        if score <= 0:
            continue
        # 每单元重复栏目：同名不够，必须同单元才加分选中
        if is_recurring_activity_lesson(new_les):
            if old.unit_no is not None and new_les.unit_no is not None:
                if int(old.unit_no) != int(new_les.unit_no):
                    continue
            elif norm_text(old.unit_title or "") != norm_text(new_les.unit_title or ""):
                # 单元号缺失时退回比 unit_title
                if not (
                    new_les.unit_title
                    and old.unit_title
                    and norm_text(new_les.unit_title)[:4] == norm_text(old.unit_title)[:4]
                ):
                    continue
            score += 10
        if score > best_score:
            best_score = score
            best = old
    return best if best_score > 0 else None


def _best_old_by_lesson_no(
    new_les: Lesson,
    old_lessons: list[Lesson],
    *,
    used_old: set[str],
) -> Lesson | None:
    no = str(new_les.lesson_no or "").strip()
    if not no:
        return None
    cands = [
        o
        for o in old_lessons
        if str(o.lesson_no or "").strip() == no and o.lesson_uid not in used_old
    ]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    # 多门同名课（语文园地×N）：按单元号对齐
    if new_les.unit_no is not None:
        same = [o for o in cands if o.unit_no == new_les.unit_no]
        if same:
            return same[0]
    nt = norm_text(new_les.unit_title or "")
    if nt:
        same_t = [o for o in cands if norm_text(o.unit_title or "") == nt]
        if same_t:
            return same_t[0]
        # 「第一单元」前缀
        prefix = nt[:4]
        same_p = [o for o in cands if norm_text(o.unit_title or "").startswith(prefix)]
        if same_p:
            return same_p[0]
    return cands[0]


def match_diff_lesson_pairs(*, old_vol: Volume, new_vol: Volume) -> list[dict]:
    """
    新↔旧课时对齐：
    1) 课名/副标题锚点重叠（语文古诗分篇等）；重复栏目要求同单元
    2) 同 lesson_no（重复栏目按 unit_no 消歧）
    3) 按序号对齐
    """
    old_lessons = _lesson_rows(old_vol)
    new_lessons = _lesson_rows(new_vol)
    used_old: set[str] = set()

    pairs: list[dict] = []
    for i, new_les in enumerate(new_lessons):
        old_les = None
        match_method = "index"

        by_title = _best_old_by_title(new_les, old_lessons, used_old=used_old)
        if by_title is not None:
            old_les = by_title
            match_method = "title_anchor"
        else:
            by_no = _best_old_by_lesson_no(new_les, old_lessons, used_old=used_old)
            if by_no is not None:
                old_les = by_no
                match_method = "lesson_no"
        if old_les is None and i < len(old_lessons):
            cand = old_lessons[i]
            if cand.lesson_uid not in used_old:
                old_les = cand
                match_method = "index"

        if old_les is not None:
            used_old.add(old_les.lesson_uid)

        pairs.append(
            {
                "index": i + 1,
                "new": {
                    "lesson_uid": new_les.lesson_uid,
                    "lesson_no": new_les.lesson_no,
                    "lesson_name": new_les.lesson_name,
                    "unit_title": new_les.unit_title,
                    "page_start": new_les.page_start,
                    "page_end": new_les.page_end,
                },
                "old": (
                    {
                        "lesson_uid": old_les.lesson_uid,
                        "lesson_no": old_les.lesson_no,
                        "lesson_name": old_les.lesson_name,
                        "unit_title": old_les.unit_title,
                        "page_start": old_les.page_start,
                        "page_end": old_les.page_end,
                    }
                    if old_les
                    else None
                ),
                "match_method": match_method,
            }
        )
    return pairs
