"""教材库页级语义建块 LLM suggest。"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .config import build_chat_openai, llm_enabled, llm_vision_model, reload_llm_env

logger = logging.getLogger(__name__)

SEMANTIC_BLOCK_SYSTEM = """你是 K12 教材页面语义建块专家。根据页面扫描图（若有）与 OCR 原子列表，将本页原子划分为语义单元块。

规则：
1. **全覆盖**：本页每个原子恰好属于一个块。
2. **语义同组**：图与图注、栏目标与说明、同一活动/对话单元等同块。
3. **勿整页糊一块**：除非本页确实只有一个教学单元。
4. **块名**：短、可读（优先图注首行 / 栏目标 / 段首）。
5. kind 取值：legend|figure|text|activity|other
6. **插图散件绑定**：若原子含 `parent_figure_code`，必须与对应父图同块；父图 `part_atom_codes` 所列散件不得拆到其他块。
7. **figure 块**：几何/过程示意图与其图内散件优先 `kind=figure` 同块。

输出严格 JSON：
{
  "page_index": <int>,
  "blocks": [{"block_name": "...", "kind": "...", "atom_codes": ["..."], "note": "可选"}],
  "warnings": []
}"""

CROSS_PAGE_BRIDGE_SYSTEM = """你是 K12 教材跨页语义桥接专家。根据各页已建块的摘要，判断相邻页块之间是否需要跨页处理。

规则：
1. **同一教学单元**：语句/段落/活动跨页未完结 → 可选 merge（合成一块）或 link（保留两块并设续页关系）。
2. **无关块禁止合并**：不同栏目、不同图、不同活动不得 merge。
3. **不作为**：无跨页延续关系时返回空 actions。
4. merge 时 block_ids 按页序排列；link 时 relation 固定为 continued_by（from 在前页，to 在后页）。

输出严格 JSON：
{
  "actions": [
    {"type": "merge", "block_ids": ["tmp_p1_i0", "tmp_p2_i0"], "block_name": "可选覆盖名"},
    {"type": "link", "from": "tmp_p1_i0", "to": "tmp_p2_i0", "relation": "continued_by"}
  ]
}"""


def _semantic_block_model() -> str:
    reload_llm_env()
    return os.getenv("LLM_SEMANTIC_BLOCK_MODEL", "").strip() or llm_vision_model()


def _parse_json_response(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"semantic block LLM returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("semantic block LLM response must be a JSON object")
    return data


def _build_user_text(
    *,
    page_index: int,
    atoms: list[dict],
    subject: str,
    lesson_name: str,
) -> str:
    atoms_json = json.dumps(atoms, ensure_ascii=False, indent=2)
    return (
        f"学科：{subject}\n"
        f"课名：{lesson_name}\n"
        f"page_index：{page_index}\n\n"
        f"【本页原子列表】\n{atoms_json}\n\n"
        "请输出 JSON：page_index, blocks（block_name, kind, atom_codes, 可选 note）, warnings。"
    )


def suggest_page_semantic_blocks(
    *,
    page_index: int,
    atoms: list[dict],
    image_data_url: str | None,
    subject: str,
    lesson_name: str,
) -> dict:
    """调用 LLM 生成单页语义建块计划（raw dict，未 normalize）。"""
    if not llm_enabled():
        raise RuntimeError("semantic block LLM disabled")

    from langchain_core.messages import HumanMessage, SystemMessage

    user_text = _build_user_text(
        page_index=page_index,
        atoms=atoms,
        subject=subject,
        lesson_name=lesson_name,
    )

    if image_data_url:
        human_content: str | list[dict[str, Any]] = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]
    else:
        human_content = user_text

    llm = build_chat_openai(
        max_tokens=2048,
        model=_semantic_block_model(),
        timeout=90.0,
    )
    messages = [
        SystemMessage(content=SEMANTIC_BLOCK_SYSTEM),
        HumanMessage(content=human_content),
    ]
    msg = llm.invoke(messages)
    raw = getattr(msg, "content", str(msg))
    return _parse_json_response(raw)


def _build_cross_page_user_text(*, summaries: list[dict]) -> str:
    summaries_json = json.dumps(summaries, ensure_ascii=False, indent=2)
    return (
        "【各页块摘要】\n"
        f"{summaries_json}\n\n"
        "请输出 JSON：actions（type=merge|link 或空数组）。禁止把无关块合并。"
    )


def suggest_cross_page_bridge(*, summaries: list[dict]) -> dict:
    """调用 LLM 生成课级跨页桥接动作（raw dict，未 apply）。"""
    if not llm_enabled():
        raise RuntimeError("semantic block LLM disabled")

    from langchain_core.messages import HumanMessage, SystemMessage

    llm = build_chat_openai(
        max_tokens=2048,
        model=_semantic_block_model(),
        timeout=90.0,
    )
    messages = [
        SystemMessage(content=CROSS_PAGE_BRIDGE_SYSTEM),
        HumanMessage(content=_build_cross_page_user_text(summaries=summaries)),
    ]
    msg = llm.invoke(messages)
    raw = getattr(msg, "content", str(msg))
    return _parse_json_response(raw)
