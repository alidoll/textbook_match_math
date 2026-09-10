"""OCR 基准快照与原子合并血缘（拆回 OCR）。"""
from __future__ import annotations

from typing import Any

IMAGE_OCR_SCANNED_KEY = "__image_ocr_scanned__"


def _bbox_dict(item: dict[str, Any]) -> dict[str, float]:
    if "x_start" in item:
        return {
            "x_start": float(item.get("x_start", 0)),
            "y_start": float(item.get("y_start", 0)),
            "x_end": float(item.get("x_end", 1)),
            "y_end": float(item.get("y_end", 1)),
        }
    bbox = item.get("bbox") or item.get("bbox_json") or {}
    return {
        "x_start": float(bbox.get("x_start", 0)),
        "y_start": float(bbox.get("y_start", 0)),
        "x_end": float(bbox.get("x_end", 1)),
        "y_end": float(bbox.get("y_end", 1)),
    }


def _bbox_area(bbox: dict[str, float]) -> float:
    w = max(0.0, bbox["x_end"] - bbox["x_start"])
    h = max(0.0, bbox["y_end"] - bbox["y_start"])
    return w * h


def _bbox_center(bbox: dict[str, float]) -> tuple[float, float]:
    return (
        (bbox["x_start"] + bbox["x_end"]) / 2,
        (bbox["y_start"] + bbox["y_end"]) / 2,
    )


def _containment_ratio(inner: dict[str, float], outer: dict[str, float]) -> float:
    ix0 = max(inner["x_start"], outer["x_start"])
    iy0 = max(inner["y_start"], outer["y_start"])
    ix1 = min(inner["x_end"], outer["x_end"])
    iy1 = min(inner["y_end"], outer["y_end"])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    inner_area = _bbox_area(inner)
    if inner_area <= 0:
        return 0.0
    return inter / inner_area


def build_ocr_snapshot_from_raw(raw_atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 OCR 提取结果存为不可变基准（按索引引用）。"""
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw_atoms):
        bbox = _bbox_dict(item)
        content = (item.get("content") or "")[:2000] or None
        ocr_text = (item.get("ocr_text") or item.get("content") or "")[:8000] or None
        out.append(
            {
                "snapshot_id": i,
                "ocr_code": str(item.get("atom_id") or "")[:16] or None,
                "atom_type": str(item.get("atom_type") or "text"),
                "bbox": bbox,
                "content": content,
                "ocr_text": ocr_text,
            }
        )
    return out


def init_lineage_from_extract(
    raw_atoms: list[dict[str, Any]],
) -> dict[str, list[int]]:
    """OCR 后每个当前 atom_code 对应一个 snapshot 索引。"""
    lineage: dict[str, list[int]] = {}
    for i, item in enumerate(raw_atoms):
        code = str(item.get("atom_id") or "").strip()[:16]
        if code:
            lineage[code] = [i]
    return lineage


def record_merge_lineage(
    lineage: dict[str, list[int]],
    source_codes: list[str],
    merged_code: str,
) -> dict[str, list[int]]:
    """合并后：合并码 → 全部来源 snapshot 索引；来源码从血缘表移除。"""
    out = dict(lineage)
    indices: list[int] = []
    for code in source_codes:
        indices.extend(out.pop(code, []))
    if not indices:
        return out
    out[merged_code] = sorted(set(indices))
    return out


def remove_lineage_codes(
    lineage: dict[str, list[int]],
    codes: list[str],
) -> dict[str, list[int]]:
    out = dict(lineage)
    for code in codes:
        out.pop(code, None)
    return out


def remap_lineage_keys(
    lineage: dict[str, list[int]],
    mapping: dict[str, str],
    *,
    dropped_codes: set[str] | None = None,
) -> dict[str, list[int]]:
    """重编号后同步血缘键。"""
    dropped = dropped_codes or set()
    out: dict[str, list[int]] = {}
    for old_code, indices in lineage.items():
        if old_code in dropped:
            continue
        new_code = mapping.get(old_code, old_code)
        out[new_code] = list(indices)
    return out


def match_baseline_indices_to_bbox(
    baseline: list[dict[str, Any]],
    bbox: dict[str, Any],
    *,
    min_count: int = 2,
) -> list[int]:
    """
    无血缘记录时：找中心落在合并框内、且面积不过大的 OCR 碎块。
    用于 AI 整理后仍能拆回。
    """
    outer = _bbox_dict(bbox)
    outer_area = max(_bbox_area(outer), 1e-9)
    hits: list[int] = []
    for snap in baseline:
        inner = _bbox_dict(snap)
        inner_area = _bbox_area(inner)
        if inner_area <= 0:
            continue
        cx, cy = _bbox_center(inner)
        if cx < outer["x_start"] or cx > outer["x_end"]:
            continue
        if cy < outer["y_start"] or cy > outer["y_end"]:
            continue
        if inner_area > outer_area * 0.92:
            continue
        cr = _containment_ratio(inner, outer)
        if cr >= 0.55 or inner_area <= outer_area * 0.45:
            hits.append(int(snap["snapshot_id"]))
    if len(hits) < min_count:
        return []
    return sorted(set(hits))


def resolve_snapshot_indices(
    lineage: dict[str, list[int]],
    atom_code: str,
    baseline: list[dict[str, Any]],
    bbox: dict[str, Any],
) -> list[int]:
    indices = list(lineage.get(atom_code) or [])
    if len(indices) >= 2:
        return sorted(set(indices))
    return match_baseline_indices_to_bbox(baseline, bbox)


def ocr_source_count(
    lineage: dict[str, list[int]],
    atom_code: str,
    baseline: list[dict[str, Any]],
    bbox: dict[str, Any],
) -> int:
    return len(resolve_snapshot_indices(lineage, atom_code, baseline, bbox))
