"""课件页文字识别：Doubao 视觉模型提取 slide 可见文字（双轨专用缓存，不写旧库）。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import build_chat_openai, llm_enabled, llm_vision_model

logger = logging.getLogger(__name__)

def _cache_dir() -> Path:
    from ...repo_paths import app_root
    return app_root() / "data" / "base_data" / "cache" / "slide_text"

_CACHE_DIR = None  # 懒加载

SLIDE_TEXT_SYSTEM = """你是 K12 课件页文字识别专家。根据附带的课件页截图，提取页面上所有可见中文文字。

规则：
1. 按从上到下、从左到右的阅读顺序输出 full_text（保留标点，段落之间用换行分隔）。
2. 包含标题、正文、标注、图内说明文字；忽略纯装饰图标与无文字图形。
3. 若页面几乎无文字，full_text 留空字符串。
4. line_count 为 full_text 中非空行数。"""


class LlmSlideText(BaseModel):
    full_text: str = ""
    line_count: int = Field(default=0, ge=0)


def slide_text_llm_enabled() -> bool:
    import os

    from dotenv import load_dotenv

    from ...repo_paths import app_root
    load_dotenv(app_root() / ".env", encoding="utf-8-sig", override=True)
    raw = os.getenv("DUAL_TRACK_SLIDE_OCR", "1").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    return llm_enabled()


def _lesson_cache_path(lesson_uid: str) -> Path:
    safe = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", lesson_uid)
    return _cache_dir() / f"{safe}.json"


def load_lesson_slide_text_cache(lesson_uid: str) -> dict[str, Any]:
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


def save_lesson_slide_text_cache(lesson_uid: str, cache: dict[str, Any]) -> None:
    _cache_dir().mkdir(parents=True, exist_ok=True)
    cache["updated_at"] = datetime.now(timezone.utc).isoformat()
    _lesson_cache_path(lesson_uid).write_text(
        json.dumps(cache, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


def slide_text_for_index(
    lesson_uid: str,
    slide_index: int,
    *,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    data = cache if cache is not None else load_lesson_slide_text_cache(lesson_uid)
    entry = (data.get("slides") or {}).get(str(int(slide_index)))
    return entry if isinstance(entry, dict) else None


def _blob_to_data_url(blob_id: str | None) -> str | None:
    if not blob_id:
        return None
    try:
        from ...models import FileBlob
        from ..blobs import read_blob_bytes

        blob = FileBlob.query.get(str(blob_id).strip())
        if not blob:
            return None
        data = read_blob_bytes(blob)
        mime = (blob.mime_type or "image/png").lower()
        prefix = (
            "data:image/jpeg;base64,"
            if "jpeg" in mime or "jpg" in mime
            else "data:image/png;base64,"
        )
        return prefix + base64.standard_b64encode(data).decode("ascii")
    except Exception as exc:
        logger.debug("slide text blob %s unreadable: %s", blob_id, exc)
        return None


def _parse_json_fallback(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    data = json.loads(raw)
    return LlmSlideText.model_validate(data).model_dump()


def _invoke_slide_text_llm(image_url: str, *, slide_index: int) -> LlmSlideText:
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = build_chat_openai(max_tokens=2048, model=llm_vision_model(), timeout=90.0)
    messages = [
        SystemMessage(content=SLIDE_TEXT_SYSTEM),
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": f"请识别【课件 P{int(slide_index)}】上的全部可见文字，输出 JSON。",
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ]
        ),
    ]
    try:
        structured = llm.with_structured_output(LlmSlideText)
        return structured.invoke(messages)
    except Exception as exc:
        logger.warning("LLM structured slide text failed, fallback JSON: %s", exc)
        msg = llm.invoke(messages)
        raw = getattr(msg, "content", str(msg))
        data = _parse_json_fallback(raw)
        return LlmSlideText.model_validate(data)


def extract_slide_text(
    *,
    lesson_uid: str,
    slide_index: int,
    blob_id: str | None,
    force_refresh: bool = False,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """识别单张课件页文字；结果写入双轨专用缓存。"""
    idx = int(slide_index)
    blob_id = str(blob_id).strip() if blob_id else ""
    store = cache if cache is not None else load_lesson_slide_text_cache(lesson_uid)
    slides_map = store.setdefault("slides", {})
    key = str(idx)

    if not force_refresh and key in slides_map:
        prev = slides_map[key]
        if isinstance(prev, dict) and prev.get("blob_id") == blob_id and prev.get("text"):
            return dict(prev)

    if not slide_text_llm_enabled():
        return {
            "slide_index": idx,
            "text": "",
            "source": "disabled",
            "char_count": 0,
            "line_count": 0,
            "blob_id": blob_id,
            "warning": "豆包未启用（DUAL_TRACK_SLIDE_OCR=0 或 LLM 密钥缺失）",
        }

    image_url = _blob_to_data_url(blob_id)
    if not image_url:
        return {
            "slide_index": idx,
            "text": "",
            "source": "missing_blob",
            "char_count": 0,
            "line_count": 0,
            "blob_id": blob_id,
            "warning": "课件图 blob 不可用",
        }

    try:
        result = _invoke_slide_text_llm(image_url, slide_index=idx)
        text = (result.full_text or "").strip()
        entry = {
            "slide_index": idx,
            "text": text,
            "source": "doubao",
            "char_count": len(text),
            "line_count": int(result.line_count or 0) or len([ln for ln in text.splitlines() if ln.strip()]),
            "blob_id": blob_id,
        }
        slides_map[key] = entry
        if cache is None:
            save_lesson_slide_text_cache(lesson_uid, store)
        return entry
    except Exception as exc:
        logger.warning("slide text OCR P%s failed: %s", idx, exc)
        return {
            "slide_index": idx,
            "text": "",
            "source": "error",
            "char_count": 0,
            "line_count": 0,
            "blob_id": blob_id,
            "warning": str(exc)[:200],
        }


def ocr_lesson_slides_text(
    *,
    lesson_uid: str,
    slides: list[Any] | None = None,
    lesson_id: str | None = None,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """对本课全部课件页跑豆包文字 OCR（仅写双轨缓存）。"""
    if slides is None or not all(isinstance(s, dict) for s in slides):
        if not lesson_id:
            raise ValueError("需要 lesson_id 或 dict 格式的 slides 快照")
        slides = load_lesson_slide_jobs(str(lesson_id))
    cache = load_lesson_slide_text_cache(lesson_uid)
    warnings: list[str] = []
    done = 0
    for slide in slides:
        idx, blob_id, _ocr = _slide_fields(slide)
        entry = extract_slide_text(
            lesson_uid=lesson_uid,
            slide_index=idx,
            blob_id=blob_id,
            force_refresh=force_refresh,
            cache=cache,
        )
        if entry.get("text"):
            done += 1
        if entry.get("warning"):
            warnings.append(f"P{idx}：{entry['warning']}")
    save_lesson_slide_text_cache(lesson_uid, cache)
    return cache, warnings


def _slide_fields(slide: Any) -> tuple[int, str | None, str]:
    """ORM 或 dict 课件行 → (slide_index, blob_id, ocr_text)。"""
    if isinstance(slide, dict):
        return (
            int(slide.get("slide_index") or 0),
            slide.get("blob_id"),
            str(slide.get("ocr_text") or "").strip(),
        )
    return (
        int(slide.slide_index),
        getattr(slide, "blob_id", None),
        (getattr(slide, "ocr_text", None) or "").strip(),
    )


def slide_text_entry_done(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    return entry.get("source") in ("doubao", "disabled")


def slide_text_cache_complete(lesson_uid: str, slides: list[Any]) -> bool:
    if not slides:
        return True
    cache = load_lesson_slide_text_cache(lesson_uid)
    for slide in slides:
        idx, blob_id, db_text = _slide_fields(slide)
        if db_text:
            continue
        entry = slide_text_for_index(lesson_uid, idx, cache=cache)
        if not slide_text_entry_done(entry):
            return False
        if entry and entry.get("blob_id") != str(blob_id or "").strip():
            return False
    return True


def persist_slide_text_to_db(
    *,
    lesson_id: str,
    lesson_uid: str,
    cache: dict[str, Any] | None = None,
) -> int:
    """将豆包课件识字结果写入 CoursewareSlide.ocr_text（旧库建块直接读库）。"""
    from ...extensions import db
    from ...models import CoursewareSlide

    store = cache if cache is not None else load_lesson_slide_text_cache(lesson_uid)
    updated = 0
    rows = (
        db.session.query(CoursewareSlide.id, CoursewareSlide.slide_index)
        .filter_by(lesson_id=lesson_id)
        .order_by(CoursewareSlide.slide_index)
        .all()
    )
    for row_id, slide_index in rows:
        entry = slide_text_for_index(lesson_uid, int(slide_index), cache=store)
        text = ((entry or {}).get("text") or "").strip()
        if not text:
            continue
        CoursewareSlide.query.filter_by(id=row_id).update(
            {"ocr_text": text[:8000]},
            synchronize_session=False,
        )
        updated += 1
    return updated


def load_lesson_slide_jobs(lesson_id: str) -> list[dict[str, Any]]:
    """从 DB 读取课件页快照（dict），避免跨 session.remove 后 ORM 游离。"""
    from ...models import CoursewareSlide

    return [
        {
            "slide_index": int(s.slide_index),
            "blob_id": s.blob_id,
            "ocr_text": (s.ocr_text or "").strip(),
        }
        for s in (
            CoursewareSlide.query.filter_by(lesson_id=lesson_id)
            .order_by(CoursewareSlide.slide_index)
            .all()
        )
    ]
