"""LangChain + 公司 ai-service：区块配对 AI 建议。"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .config import build_chat_openai, llm_enabled, llm_model

logger = logging.getLogger(__name__)

COMPARE_SUGGEST_SYSTEM = """你是一位专业的科学教材与课件复用分析专家。

【重要】复用判定分两层：
  1. 单区块执行动作（5种，仅单个区块可用）
  2. 全课时顶层分类由教研综合判定，此处只输出单区块动作

单区块执行动作（reuse_action 字段必须使用下列英文 code）：
  - reuse_as_is：直接沿用，旧课件该区块无需修改
  - optimize：优化调整，核心可用，需改非核心内容
  - reference：参考素材重制，可参考视觉/方法，核心需重录
  - new_build：全新制作
  - remove：完全删除，新教材已无此模块

变化类型 change_type 使用 L001-L009：
  L001 素养类新增, L002 名称措辞变化, L003 媒体素材更新,
  L004 结构流程重组, L005 实验参数方案变化, L006 实验器材删减,
  L007 内容删除, L008 内容新增, L009 整体重构

teacher_note 写清：旧课件哪页→新教材哪块→怎么改（中文，可供教研直接参考）。
change_detail 写具体变化描述（中文，可并入教研说明的摘要）。

【固定课件页】名称含「课件例题」或「课件尾页」的区块，属课件固定页（常无新教材对应块）：
  - 一律 reuse_as_is（直接沿用），旧课件该页无需修改
  - change_type 优先 L002（无实质变化）；若仅为课件页无教材原子，可用 L006
  - teacher_note 示例：
    · 旧区块「课件例题：选择利用拉力的情形；拔河比赛用的是什么力」→
      「旧课件P17-18为课件固定例题页，无新教材对应块，直接沿用」
    · 旧区块「课件尾页：一起来做练习吧」→
      「旧课件P20为课件固定尾页，无新教材对应块，直接沿用」

【输出格式】直接输出 JSON 对象，不要加 Markdown 代码块，不要加任何前缀说明：
{{"change_type": "...", "reuse_action": "...", "teacher_note": "...", "change_detail": "..."}}"""


class ComparePairSuggestion(BaseModel):
    change_type: str = Field(description="L001-L009")
    reuse_action: str = Field(
        description="reuse_as_is|optimize|reference|new_build|remove"
    )
    teacher_note: str = Field(description="教研说明草稿")
    change_detail: str = Field(default="", description="变化明细")

    @field_validator("change_type")
    @classmethod
    def _norm_change(cls, v: str) -> str:
        code = str(v or "").strip().upper()
        if re.fullmatch(r"L0\d\d", code):
            return code
        m = re.search(r"L0\d\d", code)
        return m.group(0) if m else "L009"

    @field_validator("reuse_action")
    @classmethod
    def _norm_reuse(cls, v: str) -> str:
        text = str(v or "").strip()
        mapping = {
            "直接沿用": "reuse_as_is",
            "优化调整": "optimize",
            "参考素材重制": "reference",
            "全新制作": "new_build",
            "完全删除": "remove",
        }
        if text in mapping:
            return mapping[text]
        allowed = {
            "reuse_as_is",
            "optimize",
            "reference",
            "new_build",
            "remove",
        }
        if text in allowed:
            return text
        return "optimize"


def _format_pages(pgs: list | None, prefix: str = "P") -> str:
    if not pgs:
        return "无"
    return prefix + "/".join(str(int(p)) for p in pgs)


def build_pair_user_prompt(
    old_block: dict | None,
    new_block: dict | None,
    *,
    old_text: str,
    new_text: str,
) -> str:
    if old_block:
        old_part = (
            f"【旧区块 {old_block.get('block_id') or ''}】"
            f"{old_block.get('block_name') or ''}\n"
            f"教材页：{_format_pages(old_block.get('old_tb_pgs'))}\n"
            f"课件页：{_format_pages(old_block.get('cw_pgs'), 'cw')}\n"
            f"教材原文：{old_text or '无'}\n"
        )
    else:
        old_part = "【旧区块】无\n"

    if new_block:
        new_part = (
            f"【新区块 {new_block.get('block_id') or ''}】"
            f"{new_block.get('block_name') or ''}\n"
            f"新教材页：{_format_pages(new_block.get('new_tb_pgs'))}\n"
            f"教材原文：{new_text or '无'}\n"
        )
    else:
        new_part = "【新区块】无\n"

    return (
        f"{old_part}\n{new_part}\n"
        "请根据以上内容输出复用判定建议。"
    )


def _parse_json_fallback(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    raw = raw.strip()
    data = json.loads(raw)
    if isinstance(data, str):
        data = json.loads(data)
    return ComparePairSuggestion.model_validate(data).model_dump()


def _invoke_langchain(user_prompt: str) -> ComparePairSuggestion:
    from langchain_core.prompts import ChatPromptTemplate

    llm = build_chat_openai(max_tokens=768)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", COMPARE_SUGGEST_SYSTEM),
            ("human", "{user_input}"),
        ]
    )
    try:
        structured = llm.with_structured_output(ComparePairSuggestion)
        chain = prompt | structured
        return chain.invoke({"user_input": user_prompt})
    except Exception as exc:
        logger.warning("structured output failed, fallback to raw JSON: %s", exc)
        chain = prompt | llm
        msg = chain.invoke({"user_input": user_prompt})
        content = getattr(msg, "content", str(msg))
        try:
            data = _parse_json_fallback(content)
            return ComparePairSuggestion.model_validate(data if isinstance(data, dict) else json.loads(data))
        except Exception as exc2:
            logger.warning("fallback parse also failed: %s", exc2)
            raise exc2


def suggest_block_pair_with_llm(
    old_block: dict | None,
    new_block: dict | None,
    *,
    old_text: str,
    new_text: str,
) -> dict:
    if not llm_enabled():
        raise RuntimeError("LLM 未启用或未配置 LLM_APP_ID/LLM_API_KEY / LLM_MODEL")

    user_prompt = build_pair_user_prompt(
        old_block,
        new_block,
        old_text=old_text,
        new_text=new_text,
    )
    result = _invoke_langchain(user_prompt)
    note = (result.teacher_note or "").strip()
    detail = (result.change_detail or "").strip()
    if detail and detail not in note:
        note = f"{detail}；{note}" if note else detail

    return {
        "reuse_action": result.reuse_action,
        "change_type": result.change_type,
        "teacher_note": note or "请补充：旧课件哪页→新教材哪块→怎么改",
        "change_detail": detail,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source": "llm_api",
        "model": llm_model(),
    }
