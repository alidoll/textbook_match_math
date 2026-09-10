# -*- coding: utf-8 -*-
"""教材库：页面表格结构化抽取（vision LLM）。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from .config import (
    build_chat_openai,
    lib_table_grid_llm_enabled,
    llm_enabled,
    llm_vision_model,
    reload_llm_env,
)

logger = logging.getLogger(__name__)

TABLE_GRID_PROMPT_VERSION = 1

def _cache_dir() -> Path:
    from ...repo_paths import app_root
    return app_root() / "data" / "base_data" / "cache" / "table_grid"

_CACHE_DIR = None  # 懒加载

TABLE_GRID_SYSTEM = """你是 K12 教材表格结构化专家。根据整页扫描图与 OCR 原子列表，识别表格区域并输出完整行列结构。

**目标**：把 OCR 压扁的多列表格还原为 columns + rows，**空单元格必须保留**（用空字符串 ""）。

常见类型：
- 化学实验记录表：编号、(1)(2)…、变化前/变化后/现象/备注 等列
- 科学记录表、自评表：表头 + 多行评分或记录

规则：
1. 同一页可有多张表，每张表一个 table_id（如 tbl_exp_record）
2. columns：表头列名数组，从左到右
3. rows：二维数组，每行列数与 columns 长度一致；无内容单元格写 ""
4. bbox：表格整体外接框，归一化 0~1（x_start, y_start, x_end, y_end）
5. title：表标题（如「实验记录」），无则 ""
6. 结合整页视觉读网格线，不要只拼接 OCR 碎句顺序
7. 非表格区域不要输出

输出严格 JSON：
{
  "tables": [{
    "table_id": "tbl_exp_record",
    "title": "实验记录",
    "bbox": {"x_start": 0.05, "y_start": 0.15, "x_end": 0.95, "y_end": 0.55},
    "columns": ["编号", "变化前的物质", "变化前的状态", "变化后的物质", "变化后的状态"],
    "rows": [
      ["(1)", "液态的水", "液态", "气态的水（水蒸气）", "气态"],
      ["(2)", "红色固体", "固态", "银白色固体", "固态"]
    ]
  }],
  "warnings": []
}"""


def _atom_code(atom: dict[str, Any]) -> str:
    return str(atom.get("atom_id") or atom.get("atom_code") or "").strip()


def _plain_text(atom: dict[str, Any]) -> str:
    for key in ("content", "ocr_text", "display_text", "text"):
        s = str(atom.get(key) or "").strip()
        if s:
            return s
    return ""


def _is_text_like(atom: dict[str, Any]) -> bool:
    return str(atom.get("atom_type") or "").strip() in ("text", "title")


def _bbox_of(atom: dict[str, Any]) -> dict[str, float]:
    bb = atom.get("bbox") or atom.get("bbox_json") or atom
    return {
        "x_start": float(bb.get("x_start", 0)),
        "y_start": float(bb.get("y_start", 0)),
        "x_end": float(bb.get("x_end", 1)),
        "y_end": float(bb.get("y_end", 1)),
    }


def _page_jpeg_data_url(page_bytes: bytes, *, max_side: int = 1600) -> str:
    from PIL import Image

    im = Image.open(io.BytesIO(page_bytes)).convert("RGB")
    w, h = im.size
    side = max(w, h)
    if side > max_side:
        scale = max_side / side
        im = im.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=82)
    b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _cache_key(
    *,
    page_image_bytes: bytes | None,
    atoms: list[dict[str, Any]],
    subject: str,
) -> str:
    parts = [subject or "", f"v{TABLE_GRID_PROMPT_VERSION}"]
    if page_image_bytes:
        parts.append(hashlib.sha256(page_image_bytes).hexdigest()[:16])
    slim = []
    for a in atoms:
        if not _is_text_like(a):
            continue
        code = _atom_code(a)
        if not code:
            continue
        slim.append({"c": code, "t": _plain_text(a)[:120]})
    parts.append(hashlib.sha256(json.dumps(slim, ensure_ascii=False).encode()).hexdigest()[:16])
    return "_".join(parts)


def _parse_json_response(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("table grid LLM response must be a JSON object")
    return data


def _normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_table(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    table_id = str(raw.get("table_id") or "").strip() or "tbl_1"
    title = str(raw.get("title") or "").strip()
    bbox = raw.get("bbox") if isinstance(raw.get("bbox"), dict) else {}
    columns_raw = raw.get("columns") or []
    if not isinstance(columns_raw, list) or not columns_raw:
        return None
    columns = [_normalize_cell(c) for c in columns_raw]
    col_n = len(columns)
    rows_out: list[list[str]] = []
    for row in raw.get("rows") or []:
        if not isinstance(row, list):
            continue
        cells = [_normalize_cell(c) for c in row[:col_n]]
        while len(cells) < col_n:
            cells.append("")
        rows_out.append(cells)
    if not rows_out:
        return None
    try:
        bbox_norm = {
            "x_start": max(0.0, min(1.0, float(bbox.get("x_start", 0)))),
            "y_start": max(0.0, min(1.0, float(bbox.get("y_start", 0)))),
            "x_end": max(0.0, min(1.0, float(bbox.get("x_end", 1)))),
            "y_end": max(0.0, min(1.0, float(bbox.get("y_end", 1)))),
        }
    except (TypeError, ValueError):
        bbox_norm = {"x_start": 0.0, "y_start": 0.0, "x_end": 1.0, "y_end": 1.0}
    return {
        "table_id": table_id,
        "title": title,
        "bbox": bbox_norm,
        "columns": columns,
        "rows": rows_out,
    }


def _build_payload(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if not _is_text_like(atom):
            continue
        code = _atom_code(atom)
        if not code:
            continue
        bb = _bbox_of(atom)
        out.append(
            {
                "atom_code": code,
                "content": _plain_text(atom)[:240],
                "bbox": bb,
            }
        )
    return out


def _llm_extract_page_table_grids(
    *,
    atoms: list[dict[str, Any]],
    subject: str,
    lesson_name: str,
    page_image_bytes: bytes | None,
    invoke_fn: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    payload = _build_payload(atoms)
    if not payload or not page_image_bytes:
        return []

    cache_path = _cache_dir() / f"{_cache_key(page_image_bytes=page_image_bytes, atoms=atoms, subject=subject)}.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            tables = [_normalize_table(t) for t in (cached.get("tables") or [])]
            return [t for t in tables if t]
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    from langchain_core.messages import HumanMessage, SystemMessage

    user_text = (
        f"学科：{subject or '未知'}\n"
        f"课名：{lesson_name or ''}\n\n"
        f"【OCR text 原子】\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请识别页面中的表格并输出 JSON：tables, warnings。"
    )
    content: list[dict[str, Any]] = [
        {"type": "text", "text": user_text},
        {
            "type": "image_url",
            "image_url": {"url": _page_jpeg_data_url(page_image_bytes)},
        },
    ]

    if invoke_fn is not None:
        raw_text = invoke_fn(content)
    else:
        reload_llm_env()
        model = llm_vision_model()
        llm = build_chat_openai(model=model)
        resp = llm.invoke(
            [
                SystemMessage(content=TABLE_GRID_SYSTEM),
                HumanMessage(content=content),
            ]
        )
        raw_text = str(getattr(resp, "content", "") or "")

    data = _parse_json_response(raw_text)
    tables = [_normalize_table(t) for t in (data.get("tables") or [])]
    tables = [t for t in tables if t]
    try:
        _cache_dir().mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"tables": tables}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return tables


def extract_page_table_grids(
    atoms: list[dict[str, Any]],
    *,
    subject: str = "",
    lesson_name: str = "",
    page_image_bytes: bytes | None = None,
    invoke_fn: Callable[..., Any] | None = None,
    force_heuristic: bool = False,
) -> list[dict[str, Any]]:
    """从整页图 + OCR 原子抽取表格网格；LLM 不可用时返回 []。"""
    if not atoms:
        return []
    if force_heuristic or not lib_table_grid_llm_enabled() or not llm_enabled():
        return []
    if not page_image_bytes:
        return []
    try:
        return _llm_extract_page_table_grids(
            atoms=atoms,
            subject=subject,
            lesson_name=lesson_name,
            page_image_bytes=page_image_bytes,
            invoke_fn=invoke_fn,
        )
    except Exception as exc:
        logger.warning("table grid LLM failed: %s", exc)
        return []
