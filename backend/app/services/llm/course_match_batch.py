"""粗分：整册新课目录 ↔ 旧课标全册目录，一次 LLM 对照。"""
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
    llm_course_match_batch_timeout,
    llm_course_match_model,
)
from .course_match_suggest import COURSE_MATCH_SYSTEM

logger = logging.getLogger(__name__)

BATCH_EXTRA = """

【批量模式】
- 下方【新课目录】为本册全部待对照课时，编号 N0、N1…；行末「正文：…」为 OCR 摘要（最多约 1000 字）
- 下方【旧课目录】为同版本旧课标全年级全册课时池，编号 O0、O1…；有正文时同样附带摘要
- 配对照时须结合正文主题、活动深度与年级，勿只看目录标题
- 必须为每条新课各输出一条结果；primary_old_index / traceability_old_index 使用 O 编号（整数），不选填 null

【输出 JSON 格式】
{
  "matches": [
    {
      "new_index": 0,
      "primary_tier": "high_similarity",
      "primary_old_index": 42,
      "traceability_old_index": null,
      "reason": "同主题同深度"
    }
  ]
}
"""


class BatchMatchRow(BaseModel):
    new_index: int
    primary_tier: Literal["high_similarity", "none"] = "none"
    primary_old_index: int | None = None
    traceability_old_index: int | None = None
    reason: str = ""

    @field_validator("primary_tier", mode="before")
    @classmethod
    def _norm_tier(cls, v: Any) -> str:
        text = str(v or "").strip().lower()
        if text in ("high_similarity", "high", "similar", "match", "高相似"):
            return "high_similarity"
        return "none"

    @field_validator("primary_old_index", "traceability_old_index", mode="before")
    @classmethod
    def _norm_index(cls, v: Any) -> int | None:
        if v is None or v == "" or str(v).lower() in ("null", "none", "-1"):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None


class BatchMatchResponse(BaseModel):
    matches: list[BatchMatchRow] = Field(default_factory=list)


def _format_new_line(idx: int, cand: LessonCandidate) -> str:
    line = f"N{idx}｜{cand.unit_title}｜{cand.lesson_text}"
    excerpt = body_excerpt_for_llm(cand.body_text, max_len=BODY_EXCERPT_FOR_LLM)
    if excerpt:
        line += f"｜正文：{excerpt}"
    return line


def _format_old_line(idx: int, cand: LessonCandidate) -> str:
    line = f"O{idx}｜{cand.grade}年级{cand.semester}｜{cand.unit_title}｜{cand.lesson_text}"
    excerpt = body_excerpt_for_llm(cand.body_text, max_len=BODY_EXCERPT_FOR_LLM)
    if excerpt:
        line += f"｜正文：{excerpt}"
    return line


def build_batch_user_prompt(
    *,
    edition: str,
    grade: int,
    semester: str,
    new_lessons: list[LessonCandidate],
    old_pool: list[LessonCandidate],
) -> str:
    new_lines = "\n".join(_format_new_line(i, c) for i, c in enumerate(new_lessons))
    old_lines = "\n".join(_format_old_line(i, c) for i, c in enumerate(old_pool))
    return (
        f"【版本】{edition}\n"
        f"【本册新课】{grade}年级{semester}，共 {len(new_lessons)} 课\n\n"
        f"【新课目录】\n{new_lines}\n\n"
        f"【旧课目录】同版本全年级全册，共 {len(old_pool)} 课\n{old_lines}\n\n"
        "请按 JSON 格式输出全部 matches。"
    )


def _parse_json_fallback(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    return json.loads(raw)


def suggest_course_match_batch(
    *,
    edition: str,
    grade: int,
    semester: str,
    new_lessons: list[LessonCandidate],
    old_pool: list[LessonCandidate],
) -> list[BatchMatchRow]:
    from langchain_core.messages import HumanMessage, SystemMessage

    if not new_lessons:
        return []

    user_prompt = build_batch_user_prompt(
        edition=edition,
        grade=grade,
        semester=semester,
        new_lessons=new_lessons,
        old_pool=old_pool,
    )
    llm = build_chat_openai(
        max_tokens=8192,
        model=llm_course_match_model(),
        include_reasoning=False,
        timeout=llm_course_match_batch_timeout(),
    )
    system = COURSE_MATCH_SYSTEM + BATCH_EXTRA
    msg = llm.invoke(
        [
            SystemMessage(content=system),
            HumanMessage(content=user_prompt),
        ]
    )
    content = getattr(msg, "content", str(msg))
    data = _parse_json_fallback(content)
    parsed = BatchMatchResponse.model_validate(data)
    logger.info(
        "course match batch: %d new lessons, %d old pool, %d rows returned",
        len(new_lessons),
        len(old_pool),
        len(parsed.matches),
    )
    return parsed.matches
