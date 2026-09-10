"""教材页教学栏目识别（豆包视觉，栏目不足时补充规则结果）。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from .block_seed_suggest import (
    _blob_to_data_url,
    _message_content_to_text,
    _repair_llm_json,
    _strip_json_fence,
)
from .config import (
    build_chat_openai,
    llm_new_block_mirror_model,
    new_block_mirror_llm_enabled,
    new_block_mirror_llm_timeout,
)

logger = logging.getLogger(__name__)

SECTION_SYSTEM = """你是 K12 科学教材版面分析专家。根据整页扫描图识别本页主要「教学栏目」的纵向分区。

栏目示例：问题情境、科学探究、拓展迁移、归纳总结、安全警示、核心概念、应用迁移、读一读、活动、实验 等。

规则：
1. 只识别本页可见的主要教学栏目，按从上到下排序。
2. name：5~12 个汉字，概括栏目用途。
3. y_start、y_end 为相对整页高度的 0~1 小数，须满足 0≤y_start<y_end≤1，相邻栏目 y 连续不重叠。
4. 忽略页眉页脚、纯页码、印章。
5. 若整页只有一个栏目，也须输出一条覆盖主要正文区的 section。

输出严格 JSON：
{"sections": [{"name": "问题情境", "y_start": 0.05, "y_end": 0.32, "reason": "…"}]}
"""


class PageSectionItem(BaseModel):
    name: str = Field(default="")
    y_start: float = Field(ge=0.0, le=1.0)
    y_end: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default="")


class PageSectionPlan(BaseModel):
    sections: list[PageSectionItem] = Field(default_factory=list)


def _plan_from_text(text: str) -> PageSectionPlan:
    raw = _strip_json_fence(_message_content_to_text(text))
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(_repair_llm_json(raw))
    return PageSectionPlan.model_validate(data)


def detect_page_sections_with_llm(
    *,
    page_index: int,
    blob_id: str | None,
    lesson_name: str = "",
) -> list[dict[str, Any]]:
    if not new_block_mirror_llm_enabled() or not blob_id:
        return []
    from langchain_core.messages import HumanMessage, SystemMessage

    user = (
        f"课时：{lesson_name or '—'}\n"
        f"教材页：p{page_index}\n"
        "请识别本页教学栏目分区，输出 sections JSON。"
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": user}]
    url = _blob_to_data_url(blob_id)
    if url:
        content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": "请输出 sections JSON。"})

    llm = build_chat_openai(
        max_tokens=2048,
        model=llm_new_block_mirror_model(),
        include_reasoning=True,
        timeout=new_block_mirror_llm_timeout(),
    )
    messages = [
        SystemMessage(content=SECTION_SYSTEM),
        HumanMessage(content=content),
    ]
    try:
        structured = llm.with_structured_output(PageSectionPlan)
        plan = structured.invoke(messages)
    except Exception as exc:
        logger.warning("page section LLM structured failed p%s: %s", page_index, exc)
        msg = llm.invoke(messages)
        raw = _message_content_to_text(getattr(msg, "content", msg))
        if not raw.strip():
            return []
        plan = _plan_from_text(raw)

    out: list[dict[str, Any]] = []
    for sec in plan.sections or []:
        name = (sec.name or "").strip()
        if not name:
            continue
        y0 = float(sec.y_start)
        y1 = float(sec.y_end)
        if y1 <= y0:
            y1 = min(1.0, y0 + 0.08)
        out.append(
            {
                "section_name": name,
                "y_start": y0,
                "y_end": y1,
                "source": "llm",
                "header_atom_code": None,
            }
        )
    return out
