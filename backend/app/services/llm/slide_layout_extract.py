"""课件页插图/图表区域识别：Doubao 视觉版面（与教材 page_layout 同款缓存模式）。"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .config import build_chat_openai, llm_enabled, llm_vision_model
from .page_layout_extract import LlmPageLayout, atom_layout_llm_enabled
from .slide_text_extract import _blob_to_data_url, _slide_fields

logger = logging.getLogger(__name__)

def _cache_dir() -> Path:
    from ...repo_paths import app_root
    return app_root() / "data" / "base_data" / "cache" / "slide_layout"

_CACHE_DIR = None  # 懒加载

SLIDE_LAYOUT_SYSTEM = """你是 K12 课件页（PPT/幻灯片）版面分析专家。根据附带的课件页截图，识别插图与图表区域。

规则：
1. illustration：照片、示意图、实验图、图标组合、流程图、表格截图等**非纯文字**画面区域。
2. 不要把标题栏、正文段落、项目符号列表文字框进 illustration（文字由文字 OCR 处理）。
3. 每个 illustration 必须填写 label：5~15 个汉字，描述画面主题（如「蜡烛加热实验图」「四格制陶步骤图」）。
4. bbox 为相对整页宽高的 0~1 小数，须满足 0≤x_start<x_end≤1、0≤y_start<y_end≤1。
5. 页上若无插图/图表，regions 返回空数组。
6. 不要返回 stamp_noise / page_number（课件无教材页码）。"""


def _lesson_cache_path(lesson_uid: str) -> Path:
    safe = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", lesson_uid)
    return _cache_dir() / f"{safe}.json"


def load_lesson_slide_layout_cache(lesson_uid: str) -> dict[str, Any]:
    path = _lesson_cache_path(lesson_uid)
    if not path.is_file():
        return {"slides": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("slides"), dict):
            return data
    except Exception:
        pass
    return {"slides": {}}


def save_lesson_slide_layout_cache(lesson_uid: str, cache: dict[str, Any]) -> None:
    _cache_dir().mkdir(parents=True, exist_ok=True)
    cache["updated_at"] = datetime.now(timezone.utc).isoformat()
    _lesson_cache_path(lesson_uid).write_text(
        json.dumps(cache, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


def slide_layout_for_index(
    lesson_uid: str,
    slide_index: int,
    *,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    data = cache if cache is not None else load_lesson_slide_layout_cache(lesson_uid)
    entry = (data.get("slides") or {}).get(str(int(slide_index)))
    return entry if isinstance(entry, dict) else None


def _parse_json_fallback(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    data = json.loads(raw)
    return LlmPageLayout.model_validate(data).model_dump()


def _invoke_slide_layout_llm(image_url: str, *, slide_index: int) -> LlmPageLayout:
    llm = build_chat_openai(max_tokens=2048, model=llm_vision_model(), timeout=90.0)
    messages = [
        SystemMessage(content=SLIDE_LAYOUT_SYSTEM),
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        f"请分析【课件 P{int(slide_index)}】，仅输出 illustration 区域 bbox 与 label；"
                        "无插图则 regions 为空数组。"
                    ),
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ]
        ),
    ]
    try:
        structured = llm.with_structured_output(LlmPageLayout)
        return structured.invoke(messages)
    except Exception as exc:
        logger.warning("LLM structured slide layout failed, fallback JSON: %s", exc)
        msg = llm.invoke(messages)
        raw = getattr(msg, "content", str(msg))
        data = _parse_json_fallback(raw)
        return LlmPageLayout.model_validate(data)


def extract_slide_layout(
    *,
    lesson_uid: str,
    slide_index: int,
    blob_id: str | None,
    force_refresh: bool = False,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """识别单张课件页插图区域；结果写入 slide_layout 缓存。"""
    idx = int(slide_index)
    blob_id = str(blob_id).strip() if blob_id else ""
    store = cache if cache is not None else load_lesson_slide_layout_cache(lesson_uid)
    slides_map = store.setdefault("slides", {})
    key = str(idx)

    if not force_refresh and key in slides_map:
        prev = slides_map[key]
        if isinstance(prev, dict) and prev.get("blob_id") == blob_id and prev.get("layout_scanned"):
            return dict(prev)

    if not atom_layout_llm_enabled():
        return {
            "slide_index": idx,
            "blob_id": blob_id,
            "source": "disabled",
            "layout_scanned": True,
            "regions": [],
            "warning": "豆包版面未启用（ATOM_LAYOUT_LLM=0 或 LLM 密钥缺失）",
        }

    image_url = _blob_to_data_url(blob_id)
    if not image_url:
        return {
            "slide_index": idx,
            "blob_id": blob_id,
            "source": "missing_blob",
            "layout_scanned": False,
            "regions": [],
            "warning": "课件图 blob 不可用",
        }

    try:
        layout = _invoke_slide_layout_llm(image_url, slide_index=idx)
        regions = [
            {
                "role": r.role,
                "label": (r.label or "").strip(),
                "x_start": float(r.x_start),
                "y_start": float(r.y_start),
                "x_end": float(r.x_end),
                "y_end": float(r.y_end),
            }
            for r in layout.regions
            if r.role == "illustration"
        ]
        entry = {
            "slide_index": idx,
            "blob_id": blob_id,
            "source": "doubao",
            "layout_scanned": True,
            "regions": regions,
            "region_count": len(regions),
        }
        slides_map[key] = entry
        if cache is None:
            save_lesson_slide_layout_cache(lesson_uid, store)
        return entry
    except Exception as exc:
        logger.warning("slide layout OCR P%s failed: %s", idx, exc)
        return {
            "slide_index": idx,
            "blob_id": blob_id,
            "source": "error",
            "layout_scanned": False,
            "regions": [],
            "warning": str(exc)[:200],
        }


def ocr_lesson_slides_layout(
    *,
    lesson_uid: str,
    slides: list[Any] | None = None,
    lesson_id: str | None = None,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """对本课全部课件页跑插图/图表版面 OCR（写 slide_layout 缓存）。"""
    if slides is None or not all(isinstance(s, dict) for s in slides):
        if not lesson_id:
            raise ValueError("需要 lesson_id 或 dict 格式的 slides 快照")
        from .slide_text_extract import load_lesson_slide_jobs

        slides = load_lesson_slide_jobs(str(lesson_id))
    cache = load_lesson_slide_layout_cache(lesson_uid)
    warnings: list[str] = []
    done = 0
    for slide in slides:
        idx, blob_id, _ocr = _slide_fields(slide)
        entry = extract_slide_layout(
            lesson_uid=lesson_uid,
            slide_index=idx,
            blob_id=blob_id,
            force_refresh=force_refresh,
            cache=cache,
        )
        if entry.get("layout_scanned"):
            done += 1
        if entry.get("warning"):
            warnings.append(f"课件P{idx}：{entry['warning']}")
    save_lesson_slide_layout_cache(lesson_uid, cache)
    return cache, warnings


def slide_layout_cache_complete(lesson_uid: str, slides: list[Any]) -> bool:
    if not slides:
        return True
    cache = load_lesson_slide_layout_cache(lesson_uid)
    for slide in slides:
        idx, blob_id, _ocr = _slide_fields(slide)
        entry = slide_layout_for_index(lesson_uid, idx, cache=cache)
        if not entry or not entry.get("layout_scanned") or entry.get("blob_id") != str(blob_id or "").strip():
            return False
    return True
