"""新教材教学区块预建（按分段表 / 教材页范围）。"""
from __future__ import annotations

from ....extensions import db
from ....models import Block, LessonPage
from ...lesson_lookup import get_lesson_by_uid
from ...old_library.annotate.blocks import _ensure_blocks_editable
from ...old_library.annotate.seed_blocks import parse_slides_spec


def parse_textbook_pages_spec(spec: str | int | list) -> tuple[int, int]:
    """解析教材页：1、'1-2'、[1,2] → (start, end)。"""
    pages = parse_slides_spec(spec)
    if not pages:
        raise ValueError("textbook_pages 不能为空")
    return min(pages), max(pages)


def _clear_lesson_blocks(lesson_id: str) -> int:
    q = Block.query.filter_by(lesson_id=lesson_id)
    count = q.count()
    q.delete(synchronize_session=False)
    return count


def _lesson_page_indices(lesson_id: str) -> set[int]:
    rows = LessonPage.query.filter_by(lesson_id=lesson_id).all()
    return {int(r.page_index) for r in rows}


def seed_new_blocks_from_table(
    *,
    lesson_uid: str,
    blocks: list[dict],
    replace_existing: bool = False,
) -> dict:
    """按分段表预建新块：每项含 block_name + textbook_pages（页范围）。"""
    les = get_lesson_by_uid(lesson_uid, book_type="new")
    _ensure_blocks_editable(les)

    if not blocks:
        raise ValueError("blocks 不能为空")

    known_pages = _lesson_page_indices(les.id)
    if not known_pages:
        raise ValueError("本课尚无教材页图，请先解析 PDF 并生成页图")

    parsed: list[tuple[str, int, int]] = []
    for i, row in enumerate(blocks):
        if not isinstance(row, dict):
            raise ValueError(f"blocks[{i}] 须为对象")
        name = str(row.get("block_name") or row.get("label") or "").strip()
        if not name:
            raise ValueError(f"blocks[{i}] 缺少 block_name / label")
        spec = row.get("textbook_pages") or row.get("pages") or row.get("new_tb_pgs")
        if spec is None:
            raise ValueError(f"blocks[{i}]（{name}）缺少 textbook_pages")
        start, end = parse_textbook_pages_spec(spec)
        missing = [p for p in range(start, end + 1) if p not in known_pages]
        if missing:
            raise ValueError(
                f"blocks[{i}]（{name}）教材页不存在：P{missing[0]}（本课共 {len(known_pages)} 页）"
            )
        parsed.append((name, start, end))

    existing_count = Block.query.filter_by(lesson_id=les.id).count()
    if existing_count and not replace_existing:
        raise ValueError("本课已有区块；分段预建请传 replace_existing=true 或先手动清空")

    removed = 0
    if replace_existing and existing_count:
        removed = _clear_lesson_blocks(les.id)

    created: list[dict] = []
    for i, (name, start, end) in enumerate(parsed, start=1):
        block = Block(
            lesson_id=les.id,
            block_code=f"N{i:02d}",
            block_name=name,
            atom_codes=[],
            course_slide_indices=None,
            textbook_page_start=start,
            textbook_page_end=end,
            sort_order=i,
        )
        db.session.add(block)
        created.append(
            {
                "block_code": block.block_code,
                "block_name": block.block_name,
                "textbook_page_start": start,
                "textbook_page_end": end,
            }
        )

    db.session.commit()
    return {
        "ok": True,
        "created_count": len(created),
        "removed_count": removed,
        "blocks": created,
    }
