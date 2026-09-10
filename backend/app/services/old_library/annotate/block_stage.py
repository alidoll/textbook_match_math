"""区块环节参考（stage_ref）与知识点名称（block_name）分离存储。"""
from __future__ import annotations

from ....models import Block
from ....services.dictionary import list_dictionary_entries

STAGE_REF_META_KEY = "stage_ref"


def stage_labels_for_book(*, book_type: str = "old") -> set[str]:
    category = "block_stage_new" if book_type == "new" else "block_stage"
    return {
        str(x["label"]).strip()
        for x in list_dictionary_entries(category=category)
        if x.get("label")
    }


def read_block_fields(block: Block, *, book_type: str = "old") -> dict[str, str]:
    """解析环节参考与区块名称；兼容旧数据将环节名写在 block_name 的情况。"""
    labels = stage_labels_for_book(book_type=book_type)
    meta = block.metadata_json or {}
    stage_ref = str(meta.get(STAGE_REF_META_KEY) or "").strip()
    block_name = (block.block_name or "").strip()
    if not stage_ref and block_name in labels:
        stage_ref = block_name
        block_name = ""
    return {"stage_ref": stage_ref, "block_name": block_name}


def apply_stage_ref(block: Block, stage_ref: str) -> None:
    meta = dict(block.metadata_json or {})
    ref = (stage_ref or "").strip()
    if ref:
        meta[STAGE_REF_META_KEY] = ref
    else:
        meta.pop(STAGE_REF_META_KEY, None)
    block.metadata_json = meta or None


def validate_block_labels(*, block_name: str, stage_ref: str) -> tuple[str, str]:
    name = (block_name or "").strip()
    ref = (stage_ref or "").strip()
    if not name and not ref:
        raise ValueError("请选择环节参考或填写区块名称")
    return name, ref
