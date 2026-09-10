"""粗分：规则预筛候选 + 大模型语义判定（豆包 / 公司 ai-service）。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..course_match.engine import LessonCandidate
from ..course_match.body_text_prep import BODY_EXCERPT_FOR_LLM, body_excerpt_for_llm
from .config import (
    build_chat_openai,
    llm_course_match_model,
    llm_course_match_timeout,
)

logger = logging.getLogger(__name__)

COURSE_MATCH_SYSTEM = """你是小学科学教材教研专家，负责新课标课时与旧课标课时的粗分对照。

【任务】从候选旧课列表中，判断新课时应如何对照（若有）。

【判定档位 primary_tier】（只能选一个主档）：
- high_similarity：核心对照旧课——同主题、同深度，可用于课件对比与修改
- none：无精准对应旧课——新课为全新内容，或候选均不相关

【可选溯源 traceability_index】：
- 仅当 primary_tier=none 且存在跨年级基础知识点来源时填写
- 低年级入门课只能作溯源参考，不能作为核心对照
- 无合适溯源则填 null

【严禁】
- 只因标题共有一两个字（如「水」「奥秘」「桥梁」）就配对
- 跨 3 个年级以上且单元主题无关的配对
- 同单元但流程/环节不同（如「验收展评」≠「招标」）强行配对
- 无匹配时随意选一个候选

【月亮/地月主题（湘科等）】标题不像也要按教学主题对照：
- 「月有阴晴圆缺」↔「在地球上看月球」（月相）；低年级「变化的月亮」仅作次选
- 「月球——地球的卫星」↔ 同年级「地月系」（勿优先配「探索月球的秘密」）
- 「人类探月史」↔「探索月球的秘密」（探月/登月史）

【优先级】同年级同册 > 同年级 > 跨年级同单元主题 > 跨年级基础溯源

【输出】primary_index / traceability_index 为候选列表中的 index（从 0 起）；不选则 null。"""


class CourseMatchDecision(BaseModel):
    primary_tier: Literal["high_similarity", "none"] = Field(
        description="核心对照档位：high_similarity 或 none"
    )
    primary_index: int | None = Field(
        default=None,
        description="核心对照候选 index；none 时为 null",
    )
    traceability_index: int | None = Field(
        default=None,
        description="溯源候选 index；仅 none 时可选",
    )
    reason: str = Field(default="", description="简短中文理由，供教研参考")

    @field_validator("primary_tier", mode="before")
    @classmethod
    def _norm_tier(cls, v: Any) -> str:
        text = str(v or "").strip().lower()
        if text in ("high_similarity", "high", "similar", "match", "高相似"):
            return "high_similarity"
        return "none"

    @field_validator("primary_index", "traceability_index", mode="before")
    @classmethod
    def _norm_index(cls, v: Any) -> int | None:
        if v is None or v == "" or str(v).lower() in ("null", "none", "-1"):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None


def _format_candidate(idx: int, item: Any) -> str:
    old: LessonCandidate = item.old
    line = (
        f"[{idx}] {old.grade}年级{old.semester}｜{old.unit_title}｜{old.lesson_text}"
        f"（规则分 {item.hybrid:.2f}）"
    )
    excerpt = body_excerpt_for_llm(old.body_text, max_len=BODY_EXCERPT_FOR_LLM)
    if excerpt:
        line += f"｜正文：{excerpt}"
    return line


def build_course_match_user_prompt(
    new: LessonCandidate,
    candidates: list[Any],
    *,
    rule_primary_tier: str | None = None,
    rule_primary_hint: str | None = None,
) -> str:
    cand_lines = "\n".join(_format_candidate(i, c) for i, c in enumerate(candidates))
    rule_line = ""
    if rule_primary_tier and rule_primary_hint:
        rule_line = (
            f"\n规则引擎参考：{rule_primary_tier} → {rule_primary_hint}"
            "（可采纳或推翻，须给出理由）\n"
        )
    new_excerpt = body_excerpt_for_llm(new.body_text, max_len=BODY_EXCERPT_FOR_LLM)
    new_line = f"【新课】{new.grade}年级{new.semester}｜{new.unit_title}｜{new.lesson_text}"
    if new_excerpt:
        new_line += f"｜正文：{new_excerpt}"
    return (
        f"{new_line}\n"
        f"{rule_line}\n"
        f"【候选旧课】共 {len(candidates)} 条：\n{cand_lines}\n\n"
        "请输出 JSON：primary_tier、primary_index、traceability_index、reason。"
    )


def _parse_json_fallback(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    return json.loads(raw)


def suggest_course_match_with_llm(
    new: LessonCandidate,
    candidates: list[Any],
    *,
    rule_primary_tier: str | None = None,
    rule_primary_hint: str | None = None,
) -> CourseMatchDecision:
    from langchain_core.messages import HumanMessage, SystemMessage

    user_prompt = build_course_match_user_prompt(
        new,
        candidates,
        rule_primary_tier=rule_primary_tier,
        rule_primary_hint=rule_primary_hint,
    )
    llm = build_chat_openai(
        max_tokens=512,
        model=llm_course_match_model(),
        include_reasoning=False,
        timeout=llm_course_match_timeout(),
    )
    msg = llm.invoke(
        [
            SystemMessage(content=COURSE_MATCH_SYSTEM),
            HumanMessage(content=user_prompt),
        ]
    )
    content = getattr(msg, "content", str(msg))
    data = _parse_json_fallback(content)
    return CourseMatchDecision.model_validate(data)
