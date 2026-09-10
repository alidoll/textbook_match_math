"""新库对照旧块建块：豆包视觉分配新原子 → 旧 block_code。"""
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

MIRROR_SYSTEM = """你是小学科学教研专家，负责「新课标教材 ↔ 旧课标教学区块」对照建块。

任务：把【新教材本页】的 OCR 原子（atom_code）分配到【旧教材对照块】（old_block_code）。

你将看到旧教材页、新教材页截图，以及两侧原子/区块文字摘要。请结合画面布局与教学语义分配，不要仅靠 OCR 字面相似度。

硬性规则：
1. 每个 new atom_code 只能归属一个 old_block_code；必须覆盖输入中列出的全部新原子，不得遗漏或重复。
2. old_block_code 只能来自给定的旧块列表；已标注 skip 的旧块不要分配。
3. 图片类原子须与相邻栏目文字、区块语义同组：问题情境/导入区的插图不要划入科学探究/实验操作块。
4. 同一页上教学目的不同的区域须拆到不同旧块；勿把整页所有原子绑到单一旧块。
5. 旧块名称含「导入/情境」→ 优先分配问题情境区；含「实验/探究/活动」→ 分配探究活动区。

输出严格 JSON：
{
  "assignments": [
    {"old_block_code": "B01", "new_atom_codes": ["A001-001"], "reason": "…"}
  ]
}
"""


class MirrorAssignmentItem(BaseModel):
    old_block_code: str = Field(description="旧教材区块 code，如 B01")
    new_atom_codes: list[str] = Field(default_factory=list)
    reason: str = Field(default="")


class NewBlockMirrorPlan(BaseModel):
    assignments: list[MirrorAssignmentItem] = Field(default_factory=list)


def _build_user_prompt(
    *,
    lesson_name: str,
    unit_title: str,
    old_page_index: int,
    new_page_index: int,
    old_blocks: list[dict[str, Any]],
    new_atoms: list[dict[str, Any]],
    prepare_hint: str = "",
) -> str:
    lines = [
        f"单元：{unit_title or '—'}",
        f"课时：{lesson_name or '—'}",
        f"对照页：旧 p{old_page_index} ↔ 新 p{new_page_index}",
        "",
        "═══ 旧教材对照块（old_block_code）═══",
    ]
    for ob in old_blocks:
        code = ob.get("block_code", "")
        name = ob.get("block_name", "")
        skip = ob.get("skip_reason") or ""
        head = f"- {code}「{name}」"
        if skip:
            lines.append(f"{head} 【跳过：{skip}】")
            continue
        excerpt = (ob.get("query_excerpt") or "")[:280]
        lines.append(f"{head}")
        if excerpt:
            lines.append(f"  摘录：{excerpt}")
        for atom in ob.get("atoms") or []:
            ac = atom.get("atom_code", "")
            txt = re.sub(r"\s+", " ", (atom.get("text") or "")[:48])
            lines.append(f"  · {ac}: {txt or '…'}")

    lines.append("")
    lines.append("═══ 待分配的新教材原子（须全部分配）═══")
    for atom in new_atoms:
        ac = atom.get("atom_code", "")
        kind = atom.get("atom_type") or "text"
        label = atom.get("image_label") or ""
        txt = re.sub(r"\s+", " ", (atom.get("text") or "")[:80])
        extra = f" label={label}" if label else ""
        lines.append(f"- {ac} ({kind}){extra}: {txt or '…'}")

    lines.append("")
    if prepare_hint:
        lines.append(prepare_hint)
        lines.append("")
    lines.append("请输出 assignments JSON。")
    return "\n".join(lines)


def _build_vision_content(
    user_text: str,
    *,
    old_page: dict[str, Any] | None,
    new_page: dict[str, Any] | None,
    closing_text: str = "请结合截图与文字，输出 assignments JSON。",
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]

    if old_page:
        pi = int(old_page.get("page_index") or 0)
        content.append(
            {
                "type": "text",
                "text": f"═══ 旧教材页截图 p{pi} ═══",
            }
        )
        url = _blob_to_data_url(old_page.get("blob_id"))
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})

    if new_page:
        pi = int(new_page.get("page_index") or 0)
        content.append(
            {
                "type": "text",
                "text": f"═══ 新教材页截图 p{pi} ═══",
            }
        )
        url = _blob_to_data_url(new_page.get("blob_id"))
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})

    content.append(
        {
            "type": "text",
            "text": closing_text,
        }
    )
    return content


def _plan_from_llm_text(text: str, plan_type: type = NewBlockMirrorPlan) -> Any:
    raw = _strip_json_fence(_message_content_to_text(text))
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(_repair_llm_json(raw))
    return plan_type.model_validate(data)


def _invoke_vision_llm(
    user_prompt: str,
    *,
    system_prompt: str,
    plan_type: type,
    old_page: dict[str, Any] | None,
    new_page: dict[str, Any] | None,
    empty_error: str,
) -> Any:
    from langchain_core.messages import HumanMessage, SystemMessage

    model = llm_new_block_mirror_model()
    timeout = new_block_mirror_llm_timeout()
    llm = build_chat_openai(
        max_tokens=4096,
        model=model,
        include_reasoning=True,
        timeout=timeout,
    )
    content = _build_vision_content(
        user_prompt,
        old_page=old_page,
        new_page=new_page,
        closing_text="请结合截图与文字，输出 JSON。",
    )
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=content),
    ]
    try:
        structured = llm.with_structured_output(plan_type)
        return structured.invoke(messages)
    except Exception as exc:
        logger.warning("vision structured output failed (%s): %s", plan_type.__name__, exc)
    msg = llm.invoke(messages)
    raw = _message_content_to_text(getattr(msg, "content", msg))
    if not raw.strip():
        raise ValueError(empty_error)
    return _plan_from_llm_text(raw, plan_type)


def _invoke_mirror_llm(
    user_prompt: str,
    *,
    old_page: dict[str, Any] | None,
    new_page: dict[str, Any] | None,
) -> NewBlockMirrorPlan:
    return _invoke_vision_llm(
        user_prompt,
        system_prompt=MIRROR_SYSTEM,
        plan_type=NewBlockMirrorPlan,
        old_page=old_page,
        new_page=new_page,
        empty_error="豆包对照建块返回为空",
    )


def validate_mirror_plan(
    plan: NewBlockMirrorPlan,
    *,
    allowed_old_codes: set[str],
    required_new_codes: set[str],
    skipped_old_codes: set[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """将 LLM 计划转为 assignments；返回 (map, warnings)。"""
    warnings: list[str] = []
    seen_new: set[str] = set()
    out: dict[str, list[str]] = {c: [] for c in allowed_old_codes}

    for item in plan.assignments or []:
        ob = (item.old_block_code or "").strip()
        if not ob:
            continue
        if ob in skipped_old_codes:
            warnings.append(f"忽略已跳过旧块 {ob} 的分配")
            continue
        if ob not in allowed_old_codes:
            warnings.append(f"未知旧块 {ob}，已忽略")
            continue
        for code in item.new_atom_codes or []:
            c = str(code).strip()
            if not c:
                continue
            if c not in required_new_codes:
                warnings.append(f"未知新原子 {c}，已忽略")
                continue
            if c in seen_new:
                warnings.append(f"重复分配 {c}，保留首次")
                continue
            seen_new.add(c)
            out.setdefault(ob, []).append(c)

    missing = required_new_codes - seen_new
    if missing:
        warnings.append(f"LLM 遗漏 {len(missing)} 个原子")
        raise ValueError(
            f"豆包分配不完整，遗漏 {len(missing)} 个原子：{sorted(missing)[:8]}"
        )

    extra = seen_new - required_new_codes
    if extra:
        warnings.append(f"多余原子 {len(extra)} 个已忽略")

    return out, warnings


def suggest_mirror_assignments_with_llm(
    *,
    lesson_name: str,
    unit_title: str,
    old_page_index: int,
    new_page_index: int,
    old_blocks: list[dict[str, Any]],
    new_atoms: list[dict[str, Any]],
    old_page: dict[str, Any] | None = None,
    new_page: dict[str, Any] | None = None,
    prepare_hint: str = "",
) -> dict[str, Any]:
    if not new_block_mirror_llm_enabled():
        raise RuntimeError("NEW_BLOCK_MIRROR_LLM 未启用或未配置 LLM")

    user_prompt = _build_user_prompt(
        lesson_name=lesson_name,
        unit_title=unit_title,
        old_page_index=old_page_index,
        new_page_index=new_page_index,
        old_blocks=old_blocks,
        new_atoms=new_atoms,
        prepare_hint=prepare_hint,
    )
    plan = _invoke_mirror_llm(
        user_prompt,
        old_page=old_page,
        new_page=new_page,
    )
    allowed = {
        str(b["block_code"]).strip()
        for b in old_blocks
        if b.get("block_code") and not b.get("skip_reason")
    }
    skipped = {
        str(b["block_code"]).strip()
        for b in old_blocks
        if b.get("skip_reason") and b.get("block_code")
    }
    required = {str(a["atom_code"]).strip() for a in new_atoms if a.get("atom_code")}
    assignments, warnings = validate_mirror_plan(
        plan,
        allowed_old_codes=allowed,
        required_new_codes=required,
        skipped_old_codes=skipped,
    )
    return {
        "assignments": assignments,
        "warnings": warnings,
        "model": llm_new_block_mirror_model(),
    }


ANCHOR_SYSTEM = """你是小学科学教研专家，负责「新课标教学区块 ↔ 旧课标教学区块」锚定对照。

任务：为【新教材已有区块】（new_block_code）找到最对应的【旧教材区块】（old_block_code）。

你将看到旧教材页、新教材页截图，以及两侧区块名称、所含原子文字摘要。请结合画面布局与教学语义匹配，不要仅靠块名字面相似或纵向位置。

硬性规则：
1. 每个 new_block_code 最多对应一个 old_block_code；每个 old_block_code 最多被一个 new_block_code 使用。
2. 必须为本页待锚定的全部新区块给出匹配；old_block_code 只能来自给定列表，已标注跳过的旧块勿用。
3. 依据区块内原子内容、栏目语义、插图归属判断对应关系，而非仅比较块名。
4. 问题情境/导入类新区块应对应旧课导入/情境块；科学探究/实验应对应旧课实验/活动块。
5. 若新区块是旧课某块的改版（内容重组但目的一致），仍应锚定到该旧块。

输出严格 JSON：
{
  "matches": [
    {"new_block_code": "N01", "old_block_code": "B01", "reason": "…"}
  ]
}
"""


class AnchorMatchItem(BaseModel):
    new_block_code: str = Field(description="新教材区块 code，如 N01")
    old_block_code: str = Field(description="旧教材区块 code，如 B01")
    reason: str = Field(default="")


class NewBlockAnchorPlan(BaseModel):
    matches: list[AnchorMatchItem] = Field(default_factory=list)


def _build_anchor_user_prompt(
    *,
    lesson_name: str,
    unit_title: str,
    old_page_index: int,
    new_page_index: int,
    old_blocks: list[dict[str, Any]],
    new_blocks: list[dict[str, Any]],
    prepare_hint: str = "",
) -> str:
    lines = [
        f"单元：{unit_title or '—'}",
        f"课时：{lesson_name or '—'}",
        f"对照页：旧 p{old_page_index} ↔ 新 p{new_page_index}",
        "",
        "═══ 旧教材对照块（old_block_code）═══",
    ]
    for ob in old_blocks:
        code = ob.get("block_code", "")
        name = ob.get("block_name", "")
        skip = ob.get("skip_reason") or ""
        head = f"- {code}「{name}」"
        if skip:
            lines.append(f"{head} 【跳过：{skip}】")
            continue
        excerpt = (ob.get("query_excerpt") or "")[:280]
        lines.append(f"{head}")
        if excerpt:
            lines.append(f"  摘录：{excerpt}")
        for atom in ob.get("atoms") or []:
            ac = atom.get("atom_code", "")
            txt = re.sub(r"\s+", " ", (atom.get("text") or "")[:48])
            lines.append(f"  · {ac}: {txt or '…'}")

    lines.append("")
    lines.append("═══ 待锚定的新教材区块（须全部匹配）═══")
    for nb in new_blocks:
        code = nb.get("block_code", "")
        name = nb.get("block_name", "")
        excerpt = (nb.get("query_excerpt") or "")[:280]
        lines.append(f"- {code}「{name}」")
        if excerpt:
            lines.append(f"  摘录：{excerpt}")
        for atom in nb.get("atoms") or []:
            ac = atom.get("atom_code", "")
            txt = re.sub(r"\s+", " ", (atom.get("text") or "")[:48])
            lines.append(f"  · {ac}: {txt or '…'}")

    lines.append("")
    if prepare_hint:
        lines.append(prepare_hint)
        lines.append("")
    lines.append("请输出 matches JSON。")
    return "\n".join(lines)


def validate_anchor_plan(
    plan: NewBlockAnchorPlan,
    *,
    allowed_old_codes: set[str],
    required_new_codes: set[str],
    skipped_old_codes: set[str],
    require_complete: bool = True,
) -> tuple[dict[str, str], list[str]]:
    """将 LLM 锚定计划转为 new_block_code → old_block_code。"""
    warnings: list[str] = []
    seen_new: set[str] = set()
    used_old: set[str] = set()
    out: dict[str, str] = {}

    for item in plan.matches or []:
        nb = (item.new_block_code or "").strip()
        ob = (item.old_block_code or "").strip()
        if not nb or not ob:
            continue
        if nb not in required_new_codes:
            warnings.append(f"未知新区块 {nb}，已忽略")
            continue
        if nb in seen_new:
            warnings.append(f"重复新区块 {nb}，保留首次")
            continue
        if ob in skipped_old_codes:
            warnings.append(f"忽略已跳过旧块 {ob}")
            continue
        if ob not in allowed_old_codes:
            warnings.append(f"未知旧块 {ob}，已忽略")
            continue
        if ob in used_old:
            warnings.append(f"旧块 {ob} 重复匹配，保留首次")
            continue
        seen_new.add(nb)
        used_old.add(ob)
        out[nb] = ob

    missing = required_new_codes - seen_new
    if missing and require_complete:
        raise ValueError(
            f"豆包锚定不完整，遗漏 {len(missing)} 个新区块：{sorted(missing)[:8]}"
        )
    if missing:
        warnings.append(f"LLM 遗漏 {len(missing)} 个新区块")

    return out, warnings


def suggest_anchor_matches_with_llm(
    *,
    lesson_name: str,
    unit_title: str,
    old_page_index: int,
    new_page_index: int,
    old_blocks: list[dict[str, Any]],
    new_blocks: list[dict[str, Any]],
    old_page: dict[str, Any] | None = None,
    new_page: dict[str, Any] | None = None,
    require_complete: bool = True,
    prepare_hint: str = "",
) -> dict[str, Any]:
    if not new_block_mirror_llm_enabled():
        raise RuntimeError("NEW_BLOCK_MIRROR_LLM 未启用或未配置 LLM")

    user_prompt = _build_anchor_user_prompt(
        lesson_name=lesson_name,
        unit_title=unit_title,
        old_page_index=old_page_index,
        new_page_index=new_page_index,
        old_blocks=old_blocks,
        new_blocks=new_blocks,
        prepare_hint=prepare_hint,
    )
    plan = _invoke_vision_llm(
        user_prompt,
        system_prompt=ANCHOR_SYSTEM,
        plan_type=NewBlockAnchorPlan,
        old_page=old_page,
        new_page=new_page,
        empty_error="豆包锚定返回为空",
    )
    allowed = {
        str(b["block_code"]).strip()
        for b in old_blocks
        if b.get("block_code") and not b.get("skip_reason")
    }
    skipped = {
        str(b["block_code"]).strip()
        for b in old_blocks
        if b.get("skip_reason") and b.get("block_code")
    }
    required = {
        str(b["block_code"]).strip() for b in new_blocks if b.get("block_code")
    }
    matches, warnings = validate_anchor_plan(
        plan,
        allowed_old_codes=allowed,
        required_new_codes=required,
        skipped_old_codes=skipped,
        require_complete=require_complete,
    )
    return {
        "matches": matches,
        "warnings": warnings,
        "model": llm_new_block_mirror_model(),
    }
