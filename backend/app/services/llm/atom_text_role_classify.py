# -*- coding: utf-8 -*-
"""教材库：OCR 原子 text 语义角色标注（绑定前）。"""
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
    lib_figure_part_role_llm_enabled,
    llm_enabled,
    llm_vision_model,
    reload_llm_env,
)

logger = logging.getLogger(__name__)

ATOM_TEXT_ROLE_PROMPT_VERSION = 1

def _cache_dir() -> Path:
    from ...repo_paths import app_root
    return app_root() / "data" / "base_data" / "cache" / "atom_text_role"

_CACHE_DIR = None  # 懒加载

VALID_TEXT_ROLES = frozenset(
    {
        "body",
        "title",
        "column_label",
        "exercise_label",
        "figure_caption",
        "figure_interior_label",
        "figure_interior_body",
        "page_footer",
        "page_chrome",
    }
)

BINDABLE_TEXT_ROLES = frozenset({"figure_interior_label", "figure_interior_body"})

_PAGE_CHROME_RE = re.compile(r"^\(\s*第\s*\d+\s*题\s*\)$|^\d{1,3}$")
_COLUMN_LABEL_RE = re.compile(
    r"^(观察与思考|想一想|读一读|做一做|[A-Z]\s*组|第[一二三四五六七八九十\d]+[节章课])$",
    re.I,
)

ATOM_TEXT_ROLE_SYSTEM = """你是 K12 教材页面 OCR 原子语义标注专家。根据整页扫描图与原子列表，为每个 text/title 原子标注 text_role。

**目标**：区分「普通正文/栏目标」与「应挂接到插图内的图内标注」，供后续几何/过程图绑定使用。

text_role 取值（必选其一）：
- body：正文段落、定义、说明文字
- title：篇/章/节标题、大标题
- column_label：栏目标、活动组名（如「B 组」「观察与思考」栏头、活动标识）
- exercise_label：题号与题干（如「3.」「4.」开头习题）
- figure_caption：插图外的图题/图注（不在插图框内）
- figure_interior_label：图内短标注（几何边长/角度如 2b、3a；箭头旁短说明如「分解因式」「约去公因式」）
- figure_interior_body：过程示意图/流程图内 OCR 合并的长段（含公式链、多步说明，视觉上在流程图框内）
- page_footer：页脚章节名、页码行
- page_chrome：页眉页脚题号等版式元素

规则：
1. **栏目标/习题/正文** 即使很短，也不要标 figure_interior_*（例：「B 组」「观察与思考」→ column_label）。
2. **figure_interior_*** 仅当语义上属于某幅 illustration 内部或紧贴流程图/几何图内部标注。
3. image 原子不要输出；只标注 text/title。
4. 结合 bbox 位置与整页视觉判断，不要只看字数长短。

输出严格 JSON：
{
  "atoms": [{"atom_code": "...", "text_role": "body"}],
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


def _atoms_have_text_roles(atoms: list[dict[str, Any]]) -> bool:
    return any(_is_text_like(a) and _plain_text(a) and a.get("text_role") for a in atoms)


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
    parts = [subject or "", f"v{ATOM_TEXT_ROLE_PROMPT_VERSION}"]
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
        raise ValueError("atom text role LLM response must be a JSON object")
    return data


def _build_classify_payload(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if not _is_text_like(atom):
            continue
        code = _atom_code(atom)
        if not code:
            continue
        bb = atom.get("bbox") or atom.get("bbox_json") or atom
        out.append(
            {
                "atom_code": code,
                "atom_type": str(atom.get("atom_type") or "text"),
                "content": _plain_text(atom)[:240],
                "bbox": {
                    "x_start": float(bb.get("x_start", 0)),
                    "y_start": float(bb.get("y_start", 0)),
                    "x_end": float(bb.get("x_end", 1)),
                    "y_end": float(bb.get("y_end", 1)),
                },
            }
        )
    return out


def _heuristic_text_role(atom: dict[str, Any]) -> str:
    text = _plain_text(atom)
    if _PAGE_CHROME_RE.match(text):
        return "page_chrome"
    if str(atom.get("atom_type") or "") == "title":
        return "title"
    t = text.strip()
    if re.match(r"^第[一二三四五六七八九十\d]+章", t) or "分式和分式方程" in t:
        return "page_footer"
    if re.match(r"^[34]\.\s", t) or re.match(r"^\d+[\.．、]", t):
        return "exercise_label"
    if _COLUMN_LABEL_RE.match(t) or t in ("B 组", "A 组", "C 组"):
        return "column_label"
    if t.startswith("[插图]") or t.startswith("插图"):
        return "figure_caption"
    return "body"


def _heuristic_classify_atom_text_roles(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(a) for a in atoms]
    for atom in out:
        if not _is_text_like(atom) or not _plain_text(atom):
            continue
        atom["text_role"] = _heuristic_text_role(atom)
    return out


def _apply_role_map(atoms: list[dict[str, Any]], role_map: dict[str, str]) -> list[dict[str, Any]]:
    out = [dict(a) for a in atoms]
    for atom in out:
        if not _is_text_like(atom):
            continue
        code = _atom_code(atom)
        role = role_map.get(code)
        if role in VALID_TEXT_ROLES:
            atom["text_role"] = role
        elif not atom.get("text_role"):
            atom["text_role"] = _heuristic_text_role(atom)
    return out


def _llm_classify_atom_text_roles(
    *,
    atoms: list[dict[str, Any]],
    subject: str,
    lesson_name: str,
    page_image_bytes: bytes | None,
    invoke_fn: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    payload = _build_classify_payload(atoms)
    if not payload:
        return _heuristic_classify_atom_text_roles(atoms)

    cache_path = _cache_dir() / f"{_cache_key(page_image_bytes=page_image_bytes, atoms=atoms, subject=subject)}.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            role_map = {
                str(x.get("atom_code") or ""): str(x.get("text_role") or "")
                for x in (cached.get("atoms") or [])
                if isinstance(x, dict)
            }
            if role_map:
                return _apply_role_map(atoms, role_map)
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    from langchain_core.messages import HumanMessage, SystemMessage

    user_text = (
        f"学科：{subject or '未知'}\n"
        f"课名：{lesson_name or ''}\n\n"
        f"【待标注 text/title 原子】\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请输出 JSON：atoms（atom_code, text_role）, warnings。"
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if page_image_bytes:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _page_jpeg_data_url(page_image_bytes)},
            }
        )

    if invoke_fn is not None:
        raw_text = invoke_fn(content)
    else:
        reload_llm_env()
        model = llm_vision_model()
        llm = build_chat_openai(model=model)
        resp = llm.invoke(
            [
                SystemMessage(content=ATOM_TEXT_ROLE_SYSTEM),
                HumanMessage(content=content),
            ]
        )
        raw_text = str(getattr(resp, "content", "") or "")

    data = _parse_json_response(raw_text)
    role_map = {
        str(x.get("atom_code") or ""): str(x.get("text_role") or "")
        for x in (data.get("atoms") or [])
        if isinstance(x, dict)
    }
    try:
        _cache_dir().mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"atoms": data.get("atoms") or []}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return _apply_role_map(atoms, role_map)


def classify_atom_text_roles(
    atoms: list[dict[str, Any]],
    *,
    subject: str = "",
    lesson_name: str = "",
    page_image_bytes: bytes | None = None,
    invoke_fn: Callable[..., Any] | None = None,
    force_heuristic: bool = False,
) -> list[dict[str, Any]]:
    """为 text/title 原子写入 text_role；LLM 不可用时走启发式。"""
    if not atoms:
        return []
    if force_heuristic or not lib_figure_part_role_llm_enabled() or not llm_enabled():
        return _heuristic_classify_atom_text_roles(atoms)
    try:
        return _llm_classify_atom_text_roles(
            atoms=atoms,
            subject=subject,
            lesson_name=lesson_name,
            page_image_bytes=page_image_bytes,
            invoke_fn=invoke_fn,
        )
    except Exception as exc:
        logger.warning("atom text role LLM failed, heuristic fallback: %s", exc)
        return _heuristic_classify_atom_text_roles(atoms)


def atoms_have_text_roles(atoms: list[dict[str, Any]]) -> bool:
    return _atoms_have_text_roles(atoms)
