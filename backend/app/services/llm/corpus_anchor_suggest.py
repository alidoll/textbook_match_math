"""全库旧块候选：豆包视觉重排 / 选定最佳锚定。"""
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

CORPUS_ANCHOR_SYSTEM = """你是小学科学教研专家，负责为新教材内容从「候选旧块列表」中选出最佳锚定对象。

你将看到新教材页截图、待锚定文字摘要，以及若干旧块候选（含块名、课时、摘录）。

规则：
1. 只能从候选列表中选一条；输出其 corpus_key（格式 lesson_id:block_code）。
2. 优先教学语义、模块类型一致（问题情境↔导入，科学探究↔实验活动）；跨课允许但须有充分依据。
3. 已被占用（anchor_locked）的候选不可选。
4. 若无一合适，corpus_key 留空并在 reason 说明。

输出严格 JSON：
{"corpus_key": "uuid:B05", "reason": "…"}
"""


class CorpusAnchorPick(BaseModel):
    corpus_key: str = Field(default="")
    reason: str = Field(default="")


def _plan_from_text(text: str) -> CorpusAnchorPick:
    raw = _strip_json_fence(_message_content_to_text(text))
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(_repair_llm_json(raw))
    return CorpusAnchorPick.model_validate(data)


def llm_pick_anchor_candidate(
    *,
    lesson_name: str,
    query_preview: str,
    block_name_hint: str,
    candidates: list[dict[str, Any]],
    new_page: dict[str, Any] | None = None,
) -> CorpusAnchorPick | None:
    if not new_block_mirror_llm_enabled() or not candidates:
        return None

    unlocked = [c for c in candidates if not c.get("anchor_locked")]
    if not unlocked:
        return None

    lines = [
        f"课时：{lesson_name or '—'}",
        f"新区块名提示：{block_name_hint or '—'}",
        f"待锚定摘录：{query_preview[:400]}",
        "",
        "═══ 候选旧块（仅可选其一）═══",
    ]
    for c in unlocked[:15]:
        key = c.get("corpus_key") or ""
        lines.append(
            f"- {key}「{c.get('block_name', '')}」"
            f" · {c.get('source_lesson_label') or c.get('lesson_name') or ''}"
        )
        excerpt = (c.get("excerpt") or "")[:100]
        if excerpt:
            lines.append(f"  摘录：{excerpt}")

    from langchain_core.messages import HumanMessage, SystemMessage

    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}]
    if new_page:
        pi = int(new_page.get("page_index") or 0)
        content.append({"type": "text", "text": f"═══ 新教材页 p{pi} ═══"})
        url = _blob_to_data_url(new_page.get("blob_id"))
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": "请输出 JSON。"})

    llm = build_chat_openai(
        max_tokens=2048,
        model=llm_new_block_mirror_model(),
        include_reasoning=True,
        timeout=new_block_mirror_llm_timeout(),
    )
    messages = [
        SystemMessage(content=CORPUS_ANCHOR_SYSTEM),
        HumanMessage(content=content),
    ]
    try:
        structured = llm.with_structured_output(CorpusAnchorPick)
        return structured.invoke(messages)
    except Exception as exc:
        logger.warning("corpus anchor LLM structured failed: %s", exc)
    try:
        msg = llm.invoke(messages)
        raw = _message_content_to_text(getattr(msg, "content", msg))
        if raw.strip():
            return _plan_from_text(raw)
    except Exception as exc:
        logger.warning("corpus anchor LLM invoke failed: %s", exc)
    return None


def rerank_suggestions_with_llm(
    suggestions: list[dict[str, Any]],
    *,
    lesson_name: str,
    query_preview: str,
    block_name_hint: str,
    new_page: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], str]:
    """豆包重排；返回 (suggestions, ranker)。"""
    if len(suggestions) < 1:
        return suggestions, "rules"

    pick = llm_pick_anchor_candidate(
        lesson_name=lesson_name,
        query_preview=query_preview,
        block_name_hint=block_name_hint,
        candidates=suggestions,
        new_page=new_page,
    )
    if not pick or not (pick.corpus_key or "").strip():
        return suggestions, "rules"

    key = pick.corpus_key.strip()
    idx = next(
        (i for i, s in enumerate(suggestions) if s.get("corpus_key") == key),
        -1,
    )
    if idx < 0:
        return suggestions, "rules"

    chosen = dict(suggestions[idx])
    chosen["score"] = min(1.0, float(chosen.get("score") or 0) + 0.25)
    chosen["reason"] = (pick.reason or chosen.get("reason") or "豆包选定").strip()
    chosen["ranker"] = "anchor_llm"
    rest = [s for i, s in enumerate(suggestions) if i != idx]
    return [chosen] + rest, "anchor_llm"
