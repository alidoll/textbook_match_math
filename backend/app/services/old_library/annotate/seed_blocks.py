"""教学区块预建：按课件页或分段表批量生成壳子。"""
from __future__ import annotations

from ....extensions import db
from ....models import Block, CoursewareSlide, Lesson
from ....query.lesson_order import order_lessons_query
from ....services.dictionary import list_dictionary_entries
from ..lessons import get_lesson_by_uid
from ..volumes import get_old_volume_by_code
from .blocks import blocks_locked, block_occupancy, _ensure_blocks_editable


def parse_slides_spec(spec: str | int | list) -> list[int]:
    """解析 slides：'3-5'、'3'、或 [3,4,5]。"""
    if isinstance(spec, list):
        out: list[int] = []
        for item in spec:
            out.extend(parse_slides_spec(item))
        return sorted({int(x) for x in out if int(x) > 0})
    if isinstance(spec, int):
        n = int(spec)
        return [n] if n > 0 else []
    text = str(spec or "").strip()
    if not text:
        return []
    if "-" in text:
        a, b = text.split("-", 1)
        start, end = int(a.strip()), int(b.strip())
        if start > end:
            start, end = end, start
        return list(range(start, end + 1))
    n = int(text)
    return [n] if n > 0 else []


def guess_slide_block_name(slide_index: int, total_slides: int) -> str:
    """按页序猜测区块名（封面/导入/尾页等），中间页默认知识点讲解。"""
    labels = [x["label"] for x in list_dictionary_entries(category="block_stage")]
    title = labels[0] if labels else "课件名称页"
    intro = labels[1] if len(labels) > 1 else "导入"
    knowledge = labels[2] if len(labels) > 2 else "知识点讲解"
    summary = labels[5] if len(labels) > 5 else "知识小结"
    footer = labels[6] if len(labels) > 6 else "课件尾页（练习提醒）"

    if total_slides <= 0:
        return knowledge
    if slide_index <= 1:
        return title
    if slide_index >= total_slides:
        return footer
    if slide_index == 2:
        return intro
    if slide_index == total_slides - 1 and total_slides > 3:
        return summary
    return knowledge


def _lesson_slide_indices(lesson_id: str) -> list[int]:
    rows = (
        CoursewareSlide.query.filter_by(lesson_id=lesson_id)
        .order_by(CoursewareSlide.slide_index)
        .all()
    )
    return [int(s.slide_index) for s in rows]


def _clear_lesson_blocks(lesson_id: str) -> int:
    q = Block.query.filter_by(lesson_id=lesson_id)
    count = q.count()
    q.delete(synchronize_session=False)
    return count


def _add_block_row(
    *,
    lesson_id: str,
    stage_ref: str,
    slide_indices: list[int],
    sort_order: int,
) -> Block:
    from .block_stage import apply_stage_ref

    block = Block(
        lesson_id=lesson_id,
        block_code=f"B{sort_order:02d}",
        block_name="",
        atom_codes=[],
        course_slide_indices=slide_indices or None,
        textbook_page_start=None,
        textbook_page_end=None,
        sort_order=sort_order,
    )
    apply_stage_ref(block, stage_ref.strip())
    db.session.add(block)
    return block


def seed_blocks_from_slides(
    *,
    lesson_uid: str,
    only_missing_slides: bool = True,
    replace_existing: bool = False,
) -> dict:
    """每张课件页预建一块（仅绑 slide，原子后续 OCR 再挂）。"""
    les = get_lesson_by_uid(lesson_uid, book_type="old")
    _ensure_blocks_editable(les)

    slide_indices = _lesson_slide_indices(les.id)
    if not slide_indices:
        raise ValueError("本课尚无课件，请先在接入页上传 ZIP")

    existing_count = Block.query.filter_by(lesson_id=les.id).count()
    if existing_count and not replace_existing and not only_missing_slides:
        raise ValueError("本课已有区块，请勾选仅补未绑页或先清空")

    removed = 0
    if replace_existing and existing_count:
        removed = _clear_lesson_blocks(les.id)
        existing_count = 0

    _, slide_owners = block_occupancy(les.id)
    total = len(slide_indices)
    created: list[dict] = []
    sort_base = Block.query.filter_by(lesson_id=les.id).count()

    for slide in slide_indices:
        if only_missing_slides and slide in slide_owners:
            continue
        sort_order = sort_base + len(created) + 1
        name = guess_slide_block_name(slide, total)
        block = _add_block_row(
            lesson_id=les.id,
            stage_ref=name,
            slide_indices=[slide],
            sort_order=sort_order,
        )
        created.append(
            {
                "block_code": block.block_code,
                "stage_ref": name,
                "block_name": "",
                "course_slide_indices": [slide],
            }
        )

    if not created and not removed:
        return {
            "ok": True,
            "created_count": 0,
            "skipped": "全部课件页已入块",
        }

    db.session.commit()
    return {
        "ok": True,
        "created_count": len(created),
        "removed_count": removed,
        "blocks": created,
    }


def seed_blocks_from_segments(
    *,
    lesson_uid: str,
    segments: list[dict],
    replace_existing: bool = False,
) -> dict:
    """按分段表预建：每项含 label/block_name + slides（'3-5' 或列表）。"""
    les = get_lesson_by_uid(lesson_uid, book_type="old")
    _ensure_blocks_editable(les)

    if not segments:
        raise ValueError("segments 不能为空")

    slide_indices_all = _lesson_slide_indices(les.id)
    if not slide_indices_all:
        raise ValueError("本课尚无课件，请先在接入页上传 ZIP")

    known_slides = set(slide_indices_all)
    parsed: list[tuple[str, list[int]]] = []
    used_slides: set[int] = set()

    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            raise ValueError(f"segments[{i}] 须为对象")
        name = str(seg.get("label") or seg.get("block_name") or "").strip()
        if not name:
            raise ValueError(f"segments[{i}] 缺少 label / block_name")
        slides = parse_slides_spec(seg.get("slides") or seg.get("course_slide_indices") or "")
        if not slides:
            raise ValueError(f"segments[{i}]（{name}）缺少 slides")
        missing = [s for s in slides if s not in known_slides]
        if missing:
            raise ValueError(
                f"segments[{i}]（{name}）课件页不存在：P{missing[0]}（本课共 {len(slide_indices_all)} 张）"
            )
        overlap = used_slides.intersection(slides)
        if overlap:
            raise ValueError(f"segments[{i}]（{name}）与前面分段重复课件页：P{sorted(overlap)[0]}")
        used_slides.update(slides)
        parsed.append((name, slides))

    existing_count = Block.query.filter_by(lesson_id=les.id).count()
    if existing_count and not replace_existing:
        raise ValueError("本课已有区块；分段预建请传 replace_existing=true 或先手动清空")

    removed = 0
    if replace_existing and existing_count:
        removed = _clear_lesson_blocks(les.id)

    created: list[dict] = []
    for i, (name, slides) in enumerate(parsed, start=1):
        block = _add_block_row(
            lesson_id=les.id,
            stage_ref=name,
            slide_indices=slides,
            sort_order=i,
        )
        created.append(
            {
                "block_code": block.block_code,
                "stage_ref": name,
                "block_name": "",
                "course_slide_indices": slides,
            }
        )

    db.session.commit()
    return {
        "ok": True,
        "created_count": len(created),
        "removed_count": removed,
        "blocks": created,
    }


def seed_volume_blocks_from_slides(
    *,
    volume_code: str,
    only_empty: bool = True,
    only_missing_slides: bool = True,
    skip_locked: bool = True,
) -> dict:
    """整册批量：对有课件的课时按页预建块。"""
    volume = get_old_volume_by_code(volume_code)
    lessons = order_lessons_query(
        Lesson.query.filter_by(volume_id=volume.id)
    ).all()

    results: list[dict] = []
    total_created = 0
    for les in lessons:
        slide_count = CoursewareSlide.query.filter_by(lesson_id=les.id).count()
        block_count = Block.query.filter_by(lesson_id=les.id).count()
        if slide_count == 0:
            results.append(
                {
                    "lesson_uid": les.lesson_uid,
                    "lesson_name": les.lesson_name,
                    "status": "skipped",
                    "reason": "无课件",
                }
            )
            continue
        if only_empty and block_count > 0:
            results.append(
                {
                    "lesson_uid": les.lesson_uid,
                    "lesson_name": les.lesson_name,
                    "status": "skipped",
                    "reason": "已有区块",
                    "block_count": block_count,
                }
            )
            continue
        if skip_locked and blocks_locked(les):
            results.append(
                {
                    "lesson_uid": les.lesson_uid,
                    "lesson_name": les.lesson_name,
                    "status": "skipped",
                    "reason": "已锁定",
                }
            )
            continue
        try:
            out = seed_blocks_from_slides(
                lesson_uid=les.lesson_uid,
                only_missing_slides=only_missing_slides,
                replace_existing=False,
            )
            n = out.get("created_count", 0)
            total_created += n
            results.append(
                {
                    "lesson_uid": les.lesson_uid,
                    "lesson_name": les.lesson_name,
                    "status": "ok",
                    "created_count": n,
                    "detail": out.get("skipped"),
                }
            )
        except ValueError as exc:
            db.session.rollback()
            results.append(
                {
                    "lesson_uid": les.lesson_uid,
                    "lesson_name": les.lesson_name,
                    "status": "error",
                    "error": str(exc),
                }
            )

    return {
        "ok": True,
        "volume_code": volume.volume_code,
        "lesson_count": len(lessons),
        "total_created": total_created,
        "results": results,
    }
