"""课时对照预判断：规则分 + 豆包语义判定（为后续 AI 建块铺垫）。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..course_match.body_text_prep import BODY_EXCERPT_FOR_LLM, body_excerpt_for_llm
from .config import build_chat_openai, llm_prescan_model, llm_prescan_timeout

logger = logging.getLogger(__name__)

PRESCAN_SYSTEM = """你是小学科学教材教研专家，负责新课标单课与旧课标对照的「预判断」。

【背景】粗分已给出 rank1–4 旧课候选；规则引擎的正文/块名相似度对「换词不改课」（如泥巴→陶泥）往往偏低，你需要做语义判断。

【任务】
1. 判断新课与粗分主参照旧课是否为「同课换名/微调」还是「改动大/配错」
2. 简要说明理由（1–3 句）
3. 预估旧块→新页的大致对应关系（供下一步 AI 建块参考，不必逐块精确）

【pair_status 只能选一个】
- confirmed：同课，主题与活动结构一致，可课内对照建块
- heavy_change：仍算同一课但活动/页序/深度改动大，宜手动建块
- no_old：粗分配错或无对应旧课
- swap_primary：正文更贴近 rank2–4 中某一课，建议换主参照（填 recommended_lesson_index）

【change_level】minor=换词/插图微调；moderate=部分活动重组；major=主题或流程明显不同

【输出 JSON】pair_status, change_level, recommended_lesson_index（可选）, reason, block_hints（数组，每项 old_block_code 或旧块名摘要 + new_page_hint 页码或活动描述）"""


class PrescanBlockHint(BaseModel):
    old_block_label: str = Field(default="", description="旧块 code 或块名摘要")
    new_page_hint: str = Field(default="", description="对应新教材页或活动描述")
    confidence: Literal["high", "medium", "low"] = "medium"


class PrescanDecision(BaseModel):
    pair_status: Literal["confirmed", "heavy_change", "no_old", "swap_primary"] = Field(
        description="对照确认建议",
    )
    change_level: Literal["minor", "moderate", "major"] = "moderate"
    recommended_lesson_index: int | None = Field(
        default=None,
        description="候选旧课列表 index；仅 swap_primary 时填写",
    )
    reason: str = Field(default="", description="中文理由，供教研确认")
    block_hints: list[PrescanBlockHint] = Field(default_factory=list)

    @field_validator("recommended_lesson_index", mode="before")
    @classmethod
    def _norm_index(cls, v: Any) -> int | None:
        if v is None or v == "" or str(v).lower() in ("null", "none", "-1"):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None


def _parse_json_fallback(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    return json.loads(raw)


def build_prescan_user_prompt(
    *,
    new_lesson: dict,
    primary_old: dict | None,
    candidates: list[dict],
    rule_summary: str,
    rule_lesson_scores: list[dict],
    rule_block_hits: list[dict],
) -> str:
    new_line = (
        f"【新课】{new_lesson.get('lesson_no', '')} {new_lesson.get('lesson_name', '')}"
        f"｜{new_lesson.get('unit_title', '')}"
    )
    new_excerpt = body_excerpt_for_llm(new_lesson.get("body_text") or "", max_len=BODY_EXCERPT_FOR_LLM)
    if new_excerpt:
        new_line += f"｜正文：{new_excerpt}"

    cand_lines = []
    for i, c in enumerate(candidates):
        tag = "主参照" if c.get("annotate_primary") else f"rank{c.get('match_rank', '?')}"
        line = f"[{i}] {tag} {c.get('lesson_no', '')} {c.get('lesson_name', '')}（{c.get('match_label', '')}）"
        excerpt = body_excerpt_for_llm(c.get("body_text") or "", max_len=400)
        if excerpt:
            line += f"｜{excerpt[:200]}"
        cand_lines.append(line)

    score_lines = [
        f"- {s.get('lesson_no')} {s.get('lesson_name')}: 规则正文分 {int((s.get('score') or 0) * 100)}%"
        for s in rule_lesson_scores[:6]
    ]
    hit_lines = [
        f"- {h.get('old_block_code', '?')} @ {h.get('lesson_name', '')} 分 {int((h.get('match_score') or 0) * 100)}%"
        for h in rule_block_hits[:8]
    ]

    primary_line = ""
    if primary_old:
        primary_line = (
            f"\n【粗分主参照】{primary_old.get('lesson_no', '')} {primary_old.get('lesson_name', '')}\n"
        )

    return (
        f"{new_line}\n"
        f"{primary_line}"
        f"\n【规则引擎摘要】{rule_summary or '无'}\n"
        f"\n【规则课时相似度】\n" + ("\n".join(score_lines) or "无") + "\n"
        f"\n【规则块命中 Top】\n" + ("\n".join(hit_lines) or "无") + "\n"
        f"\n【候选旧课】\n" + ("\n".join(cand_lines) or "无") + "\n\n"
        "请输出 JSON：pair_status, change_level, recommended_lesson_index, reason, block_hints。"
    )


def suggest_lesson_prescan_with_llm(
    *,
    new_lesson: dict,
    primary_old: dict | None,
    candidates: list[dict],
    rule_summary: str,
    rule_lesson_scores: list[dict],
    rule_block_hits: list[dict],
) -> PrescanDecision:
    from langchain_core.messages import HumanMessage, SystemMessage

    user_prompt = build_prescan_user_prompt(
        new_lesson=new_lesson,
        primary_old=primary_old,
        candidates=candidates,
        rule_summary=rule_summary,
        rule_lesson_scores=rule_lesson_scores,
        rule_block_hits=rule_block_hits,
    )
    llm = build_chat_openai(
        max_tokens=1024,
        model=llm_prescan_model(),
        include_reasoning=False,
        timeout=llm_prescan_timeout(),
    )
    msg = llm.invoke(
        [
            SystemMessage(content=PRESCAN_SYSTEM),
            HumanMessage(content=user_prompt),
        ]
    )
    content = getattr(msg, "content", str(msg))
    data = _parse_json_fallback(content)
    return PrescanDecision.model_validate(data)
