"""版面 bbox 归一化：豆包偶发返回像素坐标而非 0~1。"""
from __future__ import annotations

import re
from typing import Any

_ROLE_ALIASES = {
    "illustration": "illustration",
    "image": "illustration",
    "figure": "illustration",
    "stamp_noise": "stamp_noise",
    "stamp": "stamp_noise",
    "page_number": "page_number",
    "page_num": "page_number",
}


def _coerce_coord(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        num, den = text.split("/", 1)
        return float(num.strip()) / float(den.strip())
    return float(text)


def _region_role(raw: Any) -> str:
    key = str(raw or "illustration").strip().lower()
    return _ROLE_ALIASES.get(key, "illustration")


def coerce_layout_response(data: Any) -> dict[str, Any]:
    """把豆包多种 JSON 形状统一为 {regions: [{role,label,x_start,...}]}。"""
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        nested = data.get("regions") or data.get("items") or data.get("boxes")
        if isinstance(nested, list):
            items = nested
        elif "bbox" in data or "x_start" in data:
            items = [data]
        else:
            items = []
    else:
        items = []

    regions: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = _region_role(item.get("role") or item.get("type"))
        label = str(item.get("label") or item.get("name") or "").strip()
        bbox = item.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            xs, ys, xe, ye = (_coerce_coord(v) for v in bbox[:4])
            if xe <= xs or ye <= ys:
                xs, ys, xe, ye = xs, ys, xs + max(xe, 0.0), ys + max(ye, 0.0)
        else:
            xs = _coerce_coord(item.get("x_start", 0))
            ys = _coerce_coord(item.get("y_start", 0))
            xe = _coerce_coord(item.get("x_end", 1))
            ye = _coerce_coord(item.get("y_end", 1))
        regions.append(
            {
                "role": role,
                "label": label,
                "x_start": xs,
                "y_start": ys,
                "x_end": xe,
                "y_end": ye,
            }
        )
    return {"regions": regions}


def sanitize_layout_json_text(text: str) -> str:
    """把 388/1200 这类分数写法替换成小数，便于 json.loads。"""
    return re.sub(
        r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)",
        lambda m: str(float(m.group(1)) / float(m.group(2))),
        text or "",
    )


def _normalize_axis(value: float, *, span: float | None) -> float:
    v = float(value)
    if v <= 1.01:
        return v
    if span and span > 1.0:
        return v / span
    return v


def _bbox_values_to_xyxy(
    xs: float,
    ys: float,
    xe: float,
    ye: float,
    *,
    img_w: float | None = None,
    img_h: float | None = None,
) -> tuple[float, float, float, float]:
    """判断 bbox 四元组是 xyxy 还是 xywh，并逐轴归一化到 0~1。"""
    xs = _normalize_axis(xs, span=img_w)
    ys = _normalize_axis(ys, span=img_h)
    xe = _normalize_axis(xe, span=img_w)
    ye = _normalize_axis(ye, span=img_h)

    if xe <= xs or ye <= ys:
        w = float(img_w) if img_w and img_w > 1 else 1.0
        h = float(img_h) if img_h and img_h > 1 else 1.0
        xe_raw = _coerce_coord(xe)
        ye_raw = _coerce_coord(ye)
        if xe <= xs and 0 < xe_raw <= 1.0:
            xe = min(1.0, xs + xe_raw)
        elif xe <= xs and xe_raw > 1.0:
            xe = min(1.0, xs + xe_raw / w)
        if ye <= ys and 0 < ye_raw <= 1.0:
            ye = min(1.0, ys + ye_raw)
        elif ye <= ys and ye_raw > 1.0:
            ye = min(1.0, ys + ye_raw / h)
    return xs, ys, xe, ye


def normalize_bbox_01(
    x_start: float,
    y_start: float,
    x_end: float,
    y_end: float,
    *,
    img_w: float | None = None,
    img_h: float | None = None,
) -> tuple[float, float, float, float]:
    xs, ys, xe, ye = _bbox_values_to_xyxy(
        x_start, y_start, x_end, y_end, img_w=img_w, img_h=img_h
    )
    xs = max(0.0, min(1.0, xs))
    ys = max(0.0, min(1.0, ys))
    xe = max(0.0, min(1.0, xe))
    ye = max(0.0, min(1.0, ye))
    if xe <= xs:
        xe = min(1.0, xs + 0.02)
    if ye <= ys:
        ye = min(1.0, ys + 0.02)
    return round(xs, 4), round(ys, 4), round(xe, 4), round(ye, 4)


def normalize_region_dict(
    region: dict[str, Any],
    *,
    img_w: float | None = None,
    img_h: float | None = None,
) -> dict[str, Any]:
    out = dict(region)
    xs, ys, xe, ye = normalize_bbox_01(
        out.get("x_start", 0),
        out.get("y_start", 0),
        out.get("x_end", 1),
        out.get("y_end", 1),
        img_w=img_w,
        img_h=img_h,
    )
    out["x_start"] = xs
    out["y_start"] = ys
    out["x_end"] = xe
    out["y_end"] = ye
    return out


def normalize_layout_payload(
    data: dict[str, Any],
    *,
    img_w: float | None = None,
    img_h: float | None = None,
) -> dict[str, Any]:
    out = dict(data)
    regions: list[dict[str, Any]] = []
    for item in out.get("regions") or []:
        if isinstance(item, dict):
            regions.append(normalize_region_dict(item, img_w=img_w, img_h=img_h))
    out["regions"] = regions
    return out
