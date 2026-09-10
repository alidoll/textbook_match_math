"""教材原子提取与持久化。"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError

from ....extensions import db
from ....models import Block, LessonPage, TextbookAtom, Volume
from ....parsers.textbook_atom_extract import extract_atoms_for_lesson_page_image
from ....services.blobs import read_blob_bytes
from ...lesson_lookup import get_lesson_by_uid
from .atom_lineage import (
    build_ocr_snapshot_from_raw,
    init_lineage_from_extract,
    record_merge_lineage,
    remove_lineage_codes,
    remap_lineage_keys,
    resolve_snapshot_indices,
    IMAGE_OCR_SCANNED_KEY,
)


def _pdf_path_for_volume(volume: Volume, book_type: str) -> Path:
    if book_type == "new":
        from ...new_library.pdf.parse import _pdf_path_for_volume as fn
    else:
        from ..pdf.parse import _pdf_path_for_volume as fn
    return fn(volume)


def _blob_to_temp_path(blob) -> Path:
    data = read_blob_bytes(blob)
    suffix = ".png"
    if blob.mime_type and "jpeg" in blob.mime_type:
        suffix = ".jpg"
    tmp = Path(tempfile.mkdtemp(prefix="atom_")) / f"page{suffix}"
    tmp.write_bytes(data)
    return tmp


def _atom_row_to_raw(atom: TextbookAtom) -> dict[str, Any]:
    bb = atom.bbox_json or {}
    return {
        "atom_id": atom.atom_code,
        "atom_type": atom.atom_type or "text",
        "page": int(atom.page_index),
        "x_start": float(bb.get("x_start", 0)),
        "y_start": float(bb.get("y_start", 0)),
        "x_end": float(bb.get("x_end", 1)),
        "y_end": float(bb.get("y_end", 1)),
        "content": atom.content or "",
        "ocr_text": atom.ocr_text or "",
    }


def _renumber_image_atom_codes(
    *,
    lesson_id: str,
    page_index: int,
    write_list: list[dict[str, Any]],
) -> None:
    """插图写入前重新编号，避免与 retained 文字原子 atom_code 冲突。"""
    prefix = f"A{int(page_index):03d}-"
    used = {
        row.atom_code
        for row in TextbookAtom.query.filter_by(
            lesson_id=lesson_id,
            page_index=page_index,
        ).all()
    }
    max_idx = 0
    for code in used:
        if not str(code).startswith(prefix):
            continue
        suffix = str(code)[len(prefix):]
        try:
            max_idx = max(max_idx, int(suffix))
        except ValueError:
            continue
    for i, item in enumerate(write_list):
        item["atom_id"] = f"{prefix}{max_idx + i + 1:03d}"


def extract_atoms_for_page(
    *,
    lesson_uid: str,
    page_index: int,
    replace_page: bool = True,
    use_ocr: bool = True,
    fill_gaps: bool = False,
    book_type: str = "old",
    ocr_phase: str = "all",
) -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    lesson_id = str(les.id)
    lp = LessonPage.query.filter_by(
        lesson_id=lesson_id, page_index=page_index
    ).first()
    if not lp or not lp.blob_id:
        raise ValueError(f"第 {page_index} 页教材图不存在，请先解析 PDF 并生成页图")

    from ....models import FileBlob

    blob = FileBlob.query.get(lp.blob_id)
    if not blob:
        raise ValueError("教材页 blob 不存在")

    image_path = _blob_to_temp_path(blob)
    pdf_path = None
    pdf_page_index = None
    volume = Volume.query.get(les.volume_id)
    if volume and volume.blob_id and les.page_start:
        try:
            pdf_path = _pdf_path_for_volume(volume, book_type)
            # page_start 为 PDF 物理页（1 起算）；fitz 页索引从 0 起算
            pdf_page_index = les.page_start + page_index - 2
        except ValueError:
            pdf_path = None

    phase = (ocr_phase or "all").strip().lower()
    if phase not in ("all", "text", "images"):
        phase = "all"

    existing_text_atoms: list[dict[str, Any]] | None = None
    if phase == "images":
        rows = TextbookAtom.query.filter_by(
            lesson_id=lesson_id, page_index=page_index
        ).all()
        existing_text_atoms = [
            _atom_row_to_raw(a)
            for a in rows
            if (a.atom_type or "text") in ("text", "title")
        ]
        if not existing_text_atoms:
            raise ValueError(f"第 {page_index} 页须先完成文字 OCR")

    # OCR 可能耗时数分钟；先结束读事务并归还连接，避免整册建块时占满连接池
    db.session.commit()
    db.session.remove()

    try:
        raw_atoms = extract_atoms_for_lesson_page_image(
            page_index=page_index,
            image_path=image_path,
            pdf_path=pdf_path,
            pdf_page_index=pdf_page_index,
            use_ocr=use_ocr,
            fill_gaps=fill_gaps,
            source=book_type,
            ocr_phase=phase,
            existing_text_atoms=existing_text_atoms,
        )
    finally:
        try:
            image_path.unlink(missing_ok=True)
            image_path.parent.rmdir()
        except OSError:
            pass

    if not raw_atoms and phase != "images":
        raise ValueError("未能从该页提取原子（请检查 PyMuPDF / RapidOCR 依赖）")

    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    lesson_id = str(les.id)
    lp = LessonPage.query.filter_by(
        lesson_id=lesson_id, page_index=page_index
    ).first()
    if not lp:
        raise ValueError(f"第 {page_index} 页教材图不存在，请先解析 PDF 并生成页图")

    if phase == "text":
        TextbookAtom.query.filter(
            TextbookAtom.lesson_id == lesson_id,
            TextbookAtom.page_index == page_index,
            TextbookAtom.atom_type.in_(("text", "title")),
        ).delete(synchronize_session=False)
        db.session.flush()
        write_list = raw_atoms
    elif phase == "images":
        TextbookAtom.query.filter(
            TextbookAtom.lesson_id == lesson_id,
            TextbookAtom.page_index == page_index,
            TextbookAtom.atom_type == "image",
        ).delete(synchronize_session=False)
        db.session.flush()
        write_list = [
            item for item in raw_atoms if (item.get("atom_type") or "text") == "image"
        ]
        _renumber_image_atom_codes(
            lesson_id=lesson_id,
            page_index=page_index,
            write_list=write_list,
        )
    elif replace_page:
        TextbookAtom.query.filter_by(
            lesson_id=lesson_id, page_index=page_index
        ).delete()
        db.session.flush()
        write_list = raw_atoms
    else:
        write_list = raw_atoms

    written = 0
    try:
        for item in write_list:
            code = str(item.get("atom_id") or "")[:16]
            if not code:
                continue
            db.session.add(
                TextbookAtom(
                    lesson_id=lesson_id,
                    atom_code=code,
                    page_index=page_index,
                    atom_type=str(item.get("atom_type") or "text"),
                    bbox_json={
                        "x_start": float(item.get("x_start", 0)),
                        "y_start": float(item.get("y_start", 0)),
                        "x_end": float(item.get("x_end", 1)),
                        "y_end": float(item.get("y_end", 1)),
                    },
                    content=(item.get("content") or "")[:2000] or None,
                    ocr_text=(item.get("ocr_text") or "")[:8000] or None,
                )
            )
            written += 1

        all_rows = TextbookAtom.query.filter_by(
            lesson_id=lesson_id, page_index=page_index
        ).all()
        snapshot_atoms = [_atom_row_to_raw(a) for a in all_rows]
        snapshot = build_ocr_snapshot_from_raw(snapshot_atoms)
        lp.ocr_atoms_json = snapshot
        lineage = init_lineage_from_extract(snapshot_atoms)
        if phase == "images":
            lineage[IMAGE_OCR_SCANNED_KEY] = [1]
        lp.atom_lineage_json = lineage

        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ValueError(f"原子写入冲突（{page_index} 页），请重试 OCR") from exc
    return {
        "ok": True,
        "extracted": written,
        "page_index": page_index,
    }


def _strip_atoms_from_blocks(
    lesson_id: str,
    removed: set[str],
    *,
    replacement: str | None = None,
) -> None:
    for block in Block.query.filter_by(lesson_id=lesson_id).all():
        codes = list(block.atom_codes or [])
        if not any(c in removed for c in codes):
            continue
        updated: list[str] = []
        for code in codes:
            if code in removed:
                if replacement and replacement not in updated:
                    updated.append(replacement)
            else:
                updated.append(code)
        block.atom_codes = updated


def _next_unmerge_atom_code(lesson_id: str, page_index: int) -> str:
    n = TextbookAtom.query.filter_by(lesson_id=lesson_id, page_index=page_index).count()
    for i in range(n + 1, n + 200):
        code = f"A{page_index:03d}-U{i:02d}"
        if not TextbookAtom.query.filter_by(lesson_id=lesson_id, atom_code=code).first():
            return code
    raise ValueError("无法生成拆回原子编号")


def _lesson_page_row(lesson_id: str, page_index: int) -> LessonPage | None:
    return LessonPage.query.filter_by(lesson_id=lesson_id, page_index=page_index).first()


def _replace_atom_in_blocks(
    lesson_id: str,
    old_code: str,
    new_codes: list[str],
) -> None:
    for block in Block.query.filter_by(lesson_id=lesson_id).all():
        codes = list(block.atom_codes or [])
        if old_code not in codes:
            continue
        pos = codes.index(old_code)
        block.atom_codes = codes[:pos] + new_codes + codes[pos + 1:]


def _next_merged_atom_code(lesson_id: str, page_index: int) -> str:
    n = TextbookAtom.query.filter_by(lesson_id=lesson_id, page_index=page_index).count()
    for i in range(n + 1, n + 200):
        code = f"A{page_index:03d}-M{i:02d}"
        if not TextbookAtom.query.filter_by(lesson_id=lesson_id, atom_code=code).first():
            return code
    raise ValueError("无法生成合并原子编号")


def _is_placeholder_content(text: str | None, atom_code: str | None = None) -> bool:
    t = (text or "").strip()
    if t.startswith("[未拆分") or t.startswith("[整页未识别") or t == "[整页图像]":
        return True
    code = atom_code or ""
    return "-GAP-" in code or code.endswith("-FULL-001")


def _bbox_size(bbox: dict) -> tuple[float, float, float]:
    w = max(0.0, float(bbox.get("x_end", 1)) - float(bbox.get("x_start", 0)))
    h = max(0.0, float(bbox.get("y_end", 1)) - float(bbox.get("y_start", 0)))
    return w, h, w * h


def _validate_merge_group(atoms: list[TextbookAtom]) -> None:
    any_image = any(a.atom_type == "image" for a in atoms)

    for a in atoms:
        if _is_placeholder_content(a.content or a.ocr_text, a.atom_code):
            raise ValueError(
                f"「{a.atom_code}」是占位条，不能参与合并。请只选真实文字/标题/插图框。"
            )
        w, h, _ = _bbox_size(a.bbox_json or {})
        if a.atom_type == "image":
            if w > 0.98 or h > 0.65:
                raise ValueError(
                    f"「{a.atom_code}」范围异常，请检查是否为整页框。"
                )
        elif w > 0.92 or h > 0.28:
            if any_image and h <= 0.14 and w <= 0.82:
                pass
            else:
                raise ValueError(
                    f"「{a.atom_code}」范围过大（可能是误识别），请缩小选择后再合并。"
                )

    areas = [_bbox_size(a.bbox_json or {})[2] for a in atoms]
    median_area = sorted(areas)[len(areas) // 2]
    xs, ys, xe, ye = [], [], [], []
    for a in atoms:
        b = a.bbox_json or {}
        area = _bbox_size(b)[2]
        if area > max(median_area * 4, 0.02):
            continue
        xs.append(float(b.get("x_start", 0)))
        ys.append(float(b.get("y_start", 0)))
        xe.append(float(b.get("x_end", 1)))
        ye.append(float(b.get("y_end", 1)))
    if len(xs) < 2:
        xs = [float((a.bbox_json or {}).get("x_start", 0)) for a in atoms]
        ys = [float((a.bbox_json or {}).get("y_start", 0)) for a in atoms]
        xe = [float((a.bbox_json or {}).get("x_end", 1)) for a in atoms]
        ye = [float((a.bbox_json or {}).get("y_end", 1)) for a in atoms]

    union_h = max(ye) - min(ys)
    union_w = max(xe) - min(xs)
    max_h = 0.58 if any_image else 0.16
    if union_h > max_h:
        if any_image:
            raise ValueError(
                f"合并后竖向跨度过大（{union_h:.0%} 页高），"
                "请只合并相邻的插图/文字块。"
            )
        raise ValueError(
            f"合并后竖向跨度过大（{union_h:.0%} 页高）。请只合并相邻的一两行文字，"
            "不要选中占位条。"
        )
    if not any_image and union_w > 0.88 and union_h > 0.1:
        raise ValueError("合并范围像整段横条，请检查是否误选了占位原子。")


def merge_atoms(*, lesson_uid: str, atom_codes: list[str], book_type: str = "old") -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    codes = [str(c).strip() for c in atom_codes if str(c).strip()]
    if len(codes) < 2:
        raise ValueError("至少选择 2 个原子")

    atoms = (
        TextbookAtom.query.filter(
            TextbookAtom.lesson_id == les.id,
            TextbookAtom.atom_code.in_(codes),
        )
        .all()
    )
    found = {a.atom_code for a in atoms}
    missing = [c for c in codes if c not in found]
    if missing:
        raise ValueError(f"原子不存在：{', '.join(missing[:5])}")

    pages = {a.page_index for a in atoms}
    if len(pages) != 1:
        raise ValueError("只能合并同一教材页上的原子")
    page_index = atoms[0].page_index
    _validate_merge_group(atoms)
    from ....services.llm.atom_curate_heuristics import validate_merge_group_respects_title_bars

    merge_dicts = [
        {
            "atom_code": a.atom_code,
            "atom_type": a.atom_type,
            "bbox": dict(a.bbox_json or {}),
            "content": a.content or "",
            "ocr_text": a.ocr_text or "",
        }
        for a in atoms
    ]
    validate_merge_group_respects_title_bars(merge_dicts)

    def _sort_key(a: TextbookAtom) -> tuple:
        b = a.bbox_json or {}
        return (float(b.get("y_start", 0)), float(b.get("x_start", 0)))

    atoms.sort(key=_sort_key)
    bboxes = [a.bbox_json or {} for a in atoms]
    areas = [_bbox_size(b)[2] for b in bboxes]
    median_area = sorted(areas)[len(areas) // 2]
    use_bboxes = [
        b
        for b, area in zip(bboxes, areas)
        if area <= max(median_area * 4, 0.02)
    ] or bboxes
    xs = [float(b.get("x_start", 0)) for b in use_bboxes]
    ys = [float(b.get("y_start", 0)) for b in use_bboxes]
    xe = [float(b.get("x_end", 1)) for b in use_bboxes]
    ye = [float(b.get("y_end", 1)) for b in use_bboxes]

    parts: list[str] = []
    has_image = any(a.atom_type == "image" for a in atoms)
    for a in atoms:
        if a.atom_type == "image":
            continue
        t = (a.content or a.ocr_text or "").strip()
        if t and not t.startswith("["):
            parts.append(t)

    types = {a.atom_type for a in atoms}
    if has_image and not parts:
        atom_type = "image"
        content = "[插图]"
    elif has_image:
        atom_type = "title" if "title" in types else "text"
        content = ("[插图]\n" + "\n".join(parts))[:2000]
    else:
        atom_type = "title" if "title" in types else "text"
        content = "\n".join(parts)[:2000] if parts else "[合并文本]"

    new_code = _next_merged_atom_code(les.id, page_index)
    from ....services.llm.atom_curate_metadata import combine_atom_metadata

    merged_meta = combine_atom_metadata(*[a.metadata_json for a in atoms])
    merged = TextbookAtom(
        lesson_id=les.id,
        atom_code=new_code,
        page_index=page_index,
        atom_type=atom_type,
        bbox_json={
            "x_start": round(min(xs), 4),
            "y_start": round(min(ys), 4),
            "x_end": round(max(xe), 4),
            "y_end": round(max(ye), 4),
        },
        content=content,
        ocr_text=content[:8000],
        metadata_json=merged_meta,
    )
    db.session.add(merged)

    remove_set = set(codes)
    for atom in atoms:
        db.session.delete(atom)
    _strip_atoms_from_blocks(les.id, remove_set, replacement=new_code)

    lp = _lesson_page_row(les.id, page_index)
    if lp:
        lineage = dict(lp.atom_lineage_json or {})
        lp.atom_lineage_json = record_merge_lineage(lineage, codes, new_code)

    db.session.commit()

    return {
        "ok": True,
        "atom_code": new_code,
        "removed": codes,
        "page_index": page_index,
    }


def save_page_atoms(
    *, lesson_uid: str, page_index: int, book_type: str = "old"
) -> dict:
    """保存本页原子：删除库中本页全部旧记录，仅写回当前真实原子并按顺序重编号。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    page_index = int(page_index)
    if page_index < 1:
        raise ValueError("page_index 无效")

    atoms = TextbookAtom.query.filter_by(
        lesson_id=les.id, page_index=page_index
    ).all()
    if not atoms:
        raise ValueError(f"第 {page_index} 页没有原子")

    real_atoms = [
        a
        for a in atoms
        if not _is_placeholder_content(a.content or a.ocr_text, a.atom_code)
    ]
    if not real_atoms:
        raise ValueError(f"第 {page_index} 页没有可保存的原子（仅剩占位条）")

    def _sort_key(a: TextbookAtom) -> tuple:
        b = a.bbox_json or {}
        return (float(b.get("y_start", 0)), float(b.get("x_start", 0)))

    real_atoms.sort(key=_sort_key)

    mapping: dict[str, str] = {}
    snapshots: list[dict] = []
    for i, atom in enumerate(real_atoms, start=1):
        new_code = f"A{page_index:03d}-{i:03d}"
        mapping[atom.atom_code] = new_code
        snapshots.append(
            {
                "atom_type": atom.atom_type,
                "bbox_json": dict(atom.bbox_json or {}),
                "content": atom.content,
                "ocr_text": atom.ocr_text,
                "atom_code": new_code,
                "metadata_json": dict(atom.metadata_json or {}) or None,
            }
        )

    placeholder_codes = {a.atom_code for a in atoms} - set(mapping)
    removed_count = len(atoms)

    lp = _lesson_page_row(les.id, page_index)
    if lp and lp.atom_lineage_json:
        lp.atom_lineage_json = remap_lineage_keys(
            lp.atom_lineage_json,
            mapping,
            dropped_codes=placeholder_codes,
        )

    for block in Block.query.filter_by(lesson_id=les.id).all():
        codes = list(block.atom_codes or [])
        if not any(c in mapping or c in placeholder_codes for c in codes):
            continue
        block.atom_codes = sorted(
            [mapping[c] for c in codes if c in mapping],
            key=lambda code: int(str(code).rsplit("-", 1)[-1]),
        )

    TextbookAtom.query.filter_by(
        lesson_id=les.id, page_index=page_index
    ).delete()
    db.session.flush()

    for snap in snapshots:
        db.session.add(
            TextbookAtom(
                lesson_id=les.id,
                atom_code=snap["atom_code"],
                page_index=page_index,
                atom_type=snap["atom_type"],
                bbox_json=snap["bbox_json"],
                content=snap["content"],
                ocr_text=snap["ocr_text"],
                metadata_json=snap.get("metadata_json"),
            )
        )

    db.session.commit()
    return {
        "ok": True,
        "page_index": page_index,
        "saved": len(snapshots),
        "removed": removed_count,
        "mapping": mapping,
    }


# 兼容旧调用名
renumber_atoms_for_page = save_page_atoms


def delete_atoms(*, lesson_uid: str, atom_codes: list[str], book_type: str = "old") -> dict:
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    codes = [str(c).strip() for c in atom_codes if str(c).strip()]
    if not codes:
        raise ValueError("请选择要删除的原子")

    deleted: list[str] = []
    for code in codes:
        row = TextbookAtom.query.filter_by(
            lesson_id=les.id, atom_code=code
        ).first()
        if not row:
            raise ValueError(f"未找到原子 {code}")
        db.session.delete(row)
        deleted.append(code)

    _strip_atoms_from_blocks(les.id, set(deleted))
    for lp in LessonPage.query.filter_by(lesson_id=les.id).all():
        if lp.atom_lineage_json:
            lp.atom_lineage_json = remove_lineage_codes(lp.atom_lineage_json, deleted)
    db.session.commit()
    return {"ok": True, "deleted": deleted}


def split_mixed_dialogue_scene_spans(
    *,
    lesson_uid: str,
    split_specs: list[dict[str, Any]],
    book_type: str = "old",
) -> dict:
    """将气泡+标牌粘连 OCR 大框拆成独立原子（含补建主插图 image）。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    if not split_specs:
        return {"ok": True, "split": []}

    split_out: list[dict[str, Any]] = []
    for spec in split_specs:
        source = str(spec.get("source_code") or "").strip()
        parts = list(spec.get("parts") or [])
        if not source or len(parts) < 2:
            continue
        row = TextbookAtom.query.filter_by(lesson_id=les.id, atom_code=source).first()
        if not row:
            raise ValueError(f"未找到原子 {source}")
        page_index = row.page_index
        new_codes: list[str] = []
        for part in parts:
            new_code = _next_unmerge_atom_code(les.id, page_index)
            bbox = part.get("bbox") or {}
            db.session.add(
                TextbookAtom(
                    lesson_id=les.id,
                    atom_code=new_code,
                    page_index=page_index,
                    atom_type=str(part.get("atom_type") or "text"),
                    bbox_json={
                        "x_start": float(bbox.get("x_start", 0)),
                        "y_start": float(bbox.get("y_start", 0)),
                        "x_end": float(bbox.get("x_end", 1)),
                        "y_end": float(bbox.get("y_end", 1)),
                    },
                    content=(part.get("content") or "")[:2000] or None,
                    ocr_text=(part.get("ocr_text") or part.get("content") or "")[:8000]
                    or None,
                )
            )
            new_codes.append(new_code)
        db.session.delete(row)
        _replace_atom_in_blocks(les.id, source, new_codes)
        split_out.append({"source": source, "created": new_codes})

    db.session.commit()
    return {"ok": True, "split": split_out}


def unmerge_atom_to_ocr(
    *,
    lesson_uid: str,
    atom_code: str,
    renumber: bool = True,
    book_type: str = "old",
) -> dict:
    """将误合并的原子拆回 OCR 基准中的多条碎块。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    code = str(atom_code).strip()
    if not code:
        raise ValueError("需要 atom_code")

    row = TextbookAtom.query.filter_by(lesson_id=les.id, atom_code=code).first()
    if not row:
        raise ValueError(f"未找到原子 {code}")

    lp = _lesson_page_row(les.id, row.page_index)
    baseline = list(lp.ocr_atoms_json or []) if lp else []
    if not baseline:
        raise ValueError("本页没有 OCR 基准快照，请先执行「OCR 本页」")

    lineage = dict(lp.atom_lineage_json or {})
    indices = resolve_snapshot_indices(
        lineage,
        code,
        baseline,
        row.bbox_json or {},
    )
    if len(indices) < 2:
        raise ValueError(
            "该原子无法拆回多条 OCR 结果（可能从未合并，或 OCR 快照已过期需重新 OCR）"
        )

    new_codes: list[str] = []
    lineage_out = dict(lineage)
    lineage_out.pop(code, None)

    for idx in indices:
        snap = baseline[idx]
        new_code = _next_unmerge_atom_code(les.id, row.page_index)
        bbox = snap.get("bbox") or {}
        db.session.add(
            TextbookAtom(
                lesson_id=les.id,
                atom_code=new_code,
                page_index=row.page_index,
                atom_type=str(snap.get("atom_type") or "text"),
                bbox_json={
                    "x_start": float(bbox.get("x_start", 0)),
                    "y_start": float(bbox.get("y_start", 0)),
                    "x_end": float(bbox.get("x_end", 1)),
                    "y_end": float(bbox.get("y_end", 1)),
                },
                content=(snap.get("content") or "")[:2000] or None,
                ocr_text=(snap.get("ocr_text") or snap.get("content") or "")[:8000]
                or None,
            )
        )
        new_codes.append(new_code)
        lineage_out[new_code] = [idx]

    db.session.delete(row)
    _replace_atom_in_blocks(les.id, code, new_codes)
    lp.atom_lineage_json = lineage_out
    db.session.commit()

    page_index = row.page_index
    saved = 0
    mapping: dict[str, str] = {}
    if renumber:
        save_out = save_page_atoms(
            lesson_uid=lesson_uid,
            page_index=page_index,
            book_type=book_type,
        )
        saved = int(save_out.get("saved") or 0)
        mapping = dict(save_out.get("mapping") or {})
        new_codes = [mapping.get(c, c) for c in new_codes]

    return {
        "ok": True,
        "removed": code,
        "split_into": new_codes,
        "page_index": page_index,
        "ocr_piece_count": len(new_codes),
        "renumbered": bool(renumber),
        "saved": saved,
        "mapping": mapping,
    }


def delete_atom(*, lesson_uid: str, atom_code: str, book_type: str = "old") -> dict:
    return delete_atoms(lesson_uid=lesson_uid, atom_codes=[atom_code], book_type=book_type)


def restore_page_snapshot(
    *,
    lesson_uid: str,
    page_index: int,
    atoms: list[dict],
    blocks: list[dict] | None = None,
    book_type: str = "old",
) -> dict:
    """恢复本页原子快照（撤销误删/误合并）；可选同步恢复区块 atom_codes。"""
    les = get_lesson_by_uid(lesson_uid, book_type=book_type)
    page_index = int(page_index)
    if page_index < 1:
        raise ValueError("page_index 无效")
    if not atoms:
        raise ValueError("快照为空，无法恢复")

    TextbookAtom.query.filter_by(
        lesson_id=les.id, page_index=page_index
    ).delete()
    db.session.flush()

    written = 0
    for item in atoms:
        code = str(item.get("atom_code") or "").strip()
        if not code:
            continue
        bbox = item.get("bbox") or item.get("bbox_json") or {}
        db.session.add(
            TextbookAtom(
                lesson_id=les.id,
                atom_code=code[:16],
                page_index=page_index,
                atom_type=str(item.get("atom_type") or "text"),
                bbox_json={
                    "x_start": float(bbox.get("x_start", 0)),
                    "y_start": float(bbox.get("y_start", 0)),
                    "x_end": float(bbox.get("x_end", 1)),
                    "y_end": float(bbox.get("y_end", 1)),
                },
                content=(item.get("content") or "")[:2000] or None,
                ocr_text=(item.get("ocr_text") or item.get("content") or "")[:8000]
                or None,
            )
        )
        written += 1

    if blocks:
        for row in blocks:
            code = str(row.get("block_code") or "").strip()
            if not code:
                continue
            block = Block.query.filter_by(
                lesson_id=les.id, block_code=code
            ).first()
            if block:
                block.atom_codes = list(row.get("atom_codes") or [])

    db.session.commit()
    return {"ok": True, "restored": written, "page_index": page_index}
