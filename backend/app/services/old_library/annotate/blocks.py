"""教学区块 CRUD。"""
from __future__ import annotations

from datetime import datetime

from ....extensions import db
from ....models import Block, TextbookAtom
from ...lesson_lookup import get_lesson_by_uid
from .block_stage import apply_stage_ref, validate_block_labels


def _block_code_prefix(book_type: str) -> str:
    return "N" if book_type == "new" else "B"


def blocks_locked(lesson) -> bool:
    locked_at = getattr(lesson, "blocks_locked_at", None)
    return isinstance(locked_at, datetime)


def _ensure_blocks_editable(lesson) -> None:
    if blocks_locked(lesson):
        raise ValueError(
            "本课教学区块已锁定，无法修改。如需调整请先点「解锁区块」。"
        )


def block_occupancy(lesson_id: str) -> tuple[dict[str, Block], dict[int, Block]]:
    """返回本课已占用资源：atom_code → block，slide_index → block。"""
    atom_owners: dict[str, Block] = {}
    slide_owners: dict[int, Block] = {}
    for block in Block.query.filter_by(lesson_id=lesson_id).all():
        for code in block.atom_codes or []:
            atom_owners[str(code).strip()] = block
        for slide in block.course_slide_indices or []:
            try:
                slide_owners[int(slide)] = block
            except (TypeError, ValueError):
                continue
    return atom_owners, slide_owners


def _validate_selection_available(
    *,
    atom_codes: list[str],
    slide_indices: list[int],
    atom_owners: dict[str, Block],
    slide_owners: dict[int, Block],
    exclude_block_code: str | None = None,
) -> None:
    for code in atom_codes:
        owner = atom_owners.get(code)
        if owner and owner.block_code != exclude_block_code:
            raise ValueError(
                f"原子 {code} 已在区块 {owner.block_code}（{owner.block_name}），"
                "请先删除该区块或从区块中移除后再绑定。"
            )
    for slide in slide_indices:
        owner = slide_owners.get(slide)
        if owner and owner.block_code != exclude_block_code:
            raise ValueError(
                f"课件 P{slide} 已在区块 {owner.block_code}（{owner.block_name}），"
                "请先删除该区块或从区块中移除后再绑定。"
            )


def create_block(
    *,
    lesson_uid: str,
    block_name: str,
    atom_codes: list[str],
    course_slide_indices: list[int],
    book_type: str = "old",
    stage_ref: str | None = None,
    anchor_old_block_code: str | None = None,
    anchor_old_page_index: int | None = None,
) -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    _ensure_blocks_editable(les)
    name, ref = validate_block_labels(
        block_name=block_name,
        stage_ref=str(stage_ref or ""),
    )

    atom_codes = [str(c).strip() for c in atom_codes if str(c).strip()]
    slide_indices = sorted({int(i) for i in course_slide_indices if int(i) > 0})

    if not atom_codes and not slide_indices:
        if book_type == "new":
            raise ValueError("请至少选择教材原子")
        raise ValueError("请至少选择教材原子或课件页")
    if book_type == "new" and slide_indices:
        raise ValueError("新教材区块不绑定课件页")

    atom_owners, slide_owners = block_occupancy(les.id)
    _validate_selection_available(
        atom_codes=atom_codes,
        slide_indices=slide_indices,
        atom_owners=atom_owners,
        slide_owners=slide_owners,
    )

    existing_count = Block.query.filter_by(lesson_id=les.id).count()
    prefix = _block_code_prefix(book_type)
    block_code = f"{prefix}{existing_count + 1:02d}"

    page_indices: list[int] = []
    if atom_codes:
        atoms = TextbookAtom.query.filter(
            TextbookAtom.lesson_id == les.id,
            TextbookAtom.atom_code.in_(atom_codes),
        ).all()
        found = {a.atom_code for a in atoms}
        missing = [c for c in atom_codes if c not in found]
        if missing:
            raise ValueError(f"原子不存在：{', '.join(missing[:5])}")
        page_indices = sorted({a.page_index for a in atoms})

    metadata_json = None
    if book_type == "new" and anchor_old_block_code:
        from ...new_library.annotate.anchors import apply_anchor_to_metadata

        metadata_json = apply_anchor_to_metadata(
            lesson_id=les.id,
            metadata_json=None,
            anchor_old_block_code=anchor_old_block_code,
            old_page_index=anchor_old_page_index,
        )

    block = Block(
        lesson_id=les.id,
        block_code=block_code,
        block_name=name,
        atom_codes=atom_codes,
        course_slide_indices=slide_indices or None,
        textbook_page_start=min(page_indices) if page_indices else None,
        textbook_page_end=max(page_indices) if page_indices else None,
        sort_order=existing_count + 1,
        metadata_json=metadata_json,
    )
    apply_stage_ref(block, ref)
    db.session.add(block)
    db.session.commit()

    return {
        "ok": True,
        "block": {
            "id": block.id,
            "block_code": block.block_code,
            "block_name": block.block_name,
            "stage_ref": ref,
            "atom_codes": block.atom_codes,
            "course_slide_indices": block.course_slide_indices,
            "textbook_page_start": block.textbook_page_start,
            "textbook_page_end": block.textbook_page_end,
        },
    }


def update_block(
    *,
    lesson_uid: str,
    block_code: str,
    block_name: str,
    atom_codes: list[str],
    course_slide_indices: list[int],
    book_type: str = "old",
    stage_ref: str | None = None,
    anchor_old_block_code: str | None = None,
    anchor_old_page_index: int | None = None,
    clear_anchor_old: bool = False,
) -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    _ensure_blocks_editable(les)
    code = str(block_code).strip()
    block = Block.query.filter_by(lesson_id=les.id, block_code=code).first()
    if not block:
        raise ValueError(f"未找到区块 {block_code}")

    name, ref = validate_block_labels(
        block_name=block_name,
        stage_ref=str(stage_ref or ""),
    )

    atom_codes = [str(c).strip() for c in atom_codes if str(c).strip()]
    slide_indices = sorted({int(i) for i in course_slide_indices if int(i) > 0})

    if not atom_codes and not slide_indices:
        if book_type == "new":
            raise ValueError("请至少选择教材原子")
        raise ValueError("请至少选择教材原子或课件页")
    if book_type == "new" and slide_indices:
        raise ValueError("新教材区块不绑定课件页")

    atom_owners, slide_owners = block_occupancy(les.id)
    _validate_selection_available(
        atom_codes=atom_codes,
        slide_indices=slide_indices,
        atom_owners=atom_owners,
        slide_owners=slide_owners,
        exclude_block_code=code,
    )

    page_indices: list[int] = []
    if atom_codes:
        atoms = TextbookAtom.query.filter(
            TextbookAtom.lesson_id == les.id,
            TextbookAtom.atom_code.in_(atom_codes),
        ).all()
        found = {a.atom_code for a in atoms}
        missing = [c for c in atom_codes if c not in found]
        if missing:
            raise ValueError(f"原子不存在：{', '.join(missing[:5])}")
        page_indices = sorted({a.page_index for a in atoms})

    block.block_name = name
    block.atom_codes = atom_codes
    block.course_slide_indices = slide_indices or None
    block.textbook_page_start = min(page_indices) if page_indices else None
    block.textbook_page_end = max(page_indices) if page_indices else None
    apply_stage_ref(block, ref)
    if book_type == "new":
        if clear_anchor_old:
            from ...new_library.annotate.anchors import clear_anchor_from_metadata

            block.metadata_json = clear_anchor_from_metadata(block.metadata_json)
        elif anchor_old_block_code:
            from ...new_library.annotate.anchors import apply_anchor_to_metadata

            block.metadata_json = apply_anchor_to_metadata(
                lesson_id=les.id,
                metadata_json=block.metadata_json,
                anchor_old_block_code=anchor_old_block_code,
                old_page_index=anchor_old_page_index,
            )
    db.session.commit()

    return {
        "ok": True,
        "block": {
            "id": block.id,
            "block_code": block.block_code,
            "block_name": block.block_name,
            "stage_ref": ref,
            "atom_codes": block.atom_codes,
            "course_slide_indices": block.course_slide_indices,
            "textbook_page_start": block.textbook_page_start,
            "textbook_page_end": block.textbook_page_end,
        },
    }


def delete_block(*, lesson_uid: str, block_code: str, book_type: str = "old") -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    _ensure_blocks_editable(les)
    block = Block.query.filter_by(
        lesson_id=les.id, block_code=block_code
    ).first()
    if not block:
        raise ValueError(f"未找到区块 {block_code}")
    db.session.delete(block)
    db.session.commit()
    return {"ok": True, "deleted": block_code}


def delete_all_blocks(*, lesson_uid: str, book_type: str = "old") -> dict:
    """删除本课全部教学区块（核对 OCR/整理前误点建块时清空）。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    _ensure_blocks_editable(les)
    blocks = Block.query.filter_by(lesson_id=les.id).all()
    count = len(blocks)
    for block in blocks:
        db.session.delete(block)
    db.session.commit()
    return {"ok": True, "deleted_count": count}


def reorder_blocks(
    *, lesson_uid: str, block_codes: list[str], book_type: str = "old"
) -> dict:
    """按给定顺序重排本课区块，并重编号 block_code。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    _ensure_blocks_editable(les)
    codes = [str(c).strip() for c in block_codes if str(c).strip()]
    if not codes:
        raise ValueError("block_codes 不能为空")

    blocks = (
        Block.query.filter_by(lesson_id=les.id)
        .order_by(Block.sort_order, Block.block_code)
        .all()
    )
    by_code = {b.block_code: b for b in blocks}
    if len(codes) != len(by_code):
        raise ValueError("block_codes 须包含本课全部区块")
    missing = [c for c in codes if c not in by_code]
    if missing:
        raise ValueError(f"未知区块：{missing[0]}")

    if len(set(codes)) != len(codes):
        raise ValueError("block_codes 不能重复")

    ordered = [by_code[c] for c in codes]
    old_codes = [b.block_code for b in ordered]

    for i, block in enumerate(ordered):
        block.block_code = f"__R{i:03d}"
    db.session.flush()

    code_remap: dict[str, str] = {}
    new_codes: list[str] = []
    prefix = _block_code_prefix(book_type)
    for i, block in enumerate(ordered):
        new_code = f"{prefix}{i + 1:02d}"
        code_remap[old_codes[i]] = new_code
        block.block_code = new_code
        block.sort_order = i + 1
        new_codes.append(new_code)

    db.session.commit()
    return {"ok": True, "block_codes": new_codes, "code_remap": code_remap}


def lock_lesson_blocks(*, lesson_uid: str, book_type: str = "old") -> dict:
    """锁定本课全部区块，防止误删误改。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    count = Block.query.filter_by(lesson_id=les.id).count()
    if count == 0:
        raise ValueError("尚无区块，请先创建区块后再保存锁定")
    if blocks_locked(les):
        return {"ok": True, "blocks_locked": True, "already_locked": True}
    les.blocks_locked_at = datetime.now()
    db.session.commit()
    db.session.refresh(les)
    return {"ok": True, "blocks_locked": True, "block_count": count}


def unlock_lesson_blocks(*, lesson_uid: str, book_type: str = "old") -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    if not blocks_locked(les):
        return {"ok": True, "blocks_locked": False, "already_unlocked": True}
    les.blocks_locked_at = None
    db.session.commit()
    db.session.refresh(les)
    return {"ok": True, "blocks_locked": False}
